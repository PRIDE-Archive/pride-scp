#!/usr/bin/env python3
"""Local-LLM SDRF completion with fail-closed escalation bundles.

The local model is allowed to try every unresolved blocker field. Deterministic policy
produces two separate SDRF artifacts:

* confident_candidate.sdrf.tsv: only patches that pass semantic, vocabulary, evidence,
  and deterministic row-scope gates;
* local_best_effort.sdrf.tsv: review-only draft containing additional local-model
  proposals where the model explicitly judged one value study-wide.

Ambiguous, unsupported, conflicting, context-limited, or otherwise unaccepted fields are
packaged into a compact escalation bundle for a stronger external model. No output from
this tool is submission-ready by itself.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import sdrf_field_fit_adjudicator as field_fit

try:
    import sdrf_annotation_state as annotation_state
except ImportError:  # pragma: no cover - only for standalone source-only bundles
    annotation_state = None

VERSION = "pride-scp-sdrf-llm-completion-v0.2.0"
BUNDLE_SCHEMA_VERSION = "pride-scp-sdrf-escalation-bundle-v2"
PATCH_SCHEMA_VERSION = "pride-scp-sdrf-external-patch-v2"


@dataclass(frozen=True)
class CompletionDecision:
    accession: str
    blocker_family: str
    field: str
    current_values: tuple[str, ...]
    current_value_model_assessment: str
    current_value_audit: str
    current_value_evidence_refs: tuple[str, ...]
    model_decision: str
    proposed_value: str
    canonical_value: str
    vocabulary_status: str
    policy_status: str
    application_scope: str
    deterministic_scope_supported: bool
    confident_patch_status: str
    local_draft_status: str
    escalation_reason: str
    evidence_refs: tuple[str, ...]
    rationale: str
    prompt_sha256: str
    field_contract_sha256: str


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def normalize_text(value: str) -> str:
    return " ".join(value.strip().split())


def read_tsv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        return list(reader.fieldnames or []), [dict(row) for row in reader]


def write_tsv(path: Path, headers: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in headers})


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def parse_blocker_context(
    blocker_manifest: Path,
    accession: str,
) -> tuple[str, list[str], str, str]:
    _, rows = read_tsv(blocker_manifest)
    matches = [row for row in rows if str(row.get("accession") or "") == accession]
    if not matches:
        raise KeyError(f"accession missing from blocker manifest: {accession}")
    row = matches[0]
    raw = str(row.get("blocker_fields") or row.get("blocker_field") or "")
    fields = [normalize_text(item) for item in raw.split(";") if normalize_text(item)]
    state = str(
        row.get("state")
        or row.get("baseline_terminal_state")
        or row.get("final_state")
        or ""
    )
    reason = str(row.get("reason_code") or row.get("baseline_reason_code") or "")
    blocker_family = str(row.get("blocker_family") or "").strip()
    if not blocker_family and annotation_state is not None:
        blocker_family = annotation_state.normalize_blocker_family(state, reason, fields)
    if not blocker_family:
        blocker_family = "unknown"
    return blocker_family, fields, state, reason


def parse_blocker_fields(blocker_manifest: Path, accession: str) -> tuple[str, list[str]]:
    blocker_family, fields, _, _ = parse_blocker_context(blocker_manifest, accession)
    return blocker_family, fields


NON_SUBSTANTIVE_VALUES = {
    "",
    "na",
    "n/a",
    "not applicable",
    "not available",
    "unknown",
}

ROW_CONTEXT_COLUMNS = (
    "source name",
    "assay name",
    "characteristics[cell identifier]",
    "characteristics[sample type]",
    "comment[data file]",
    "comment[label]",
)


def distinct_current_values(rows: list[dict[str, str]], field: str) -> list[str]:
    values = {
        normalize_text(str(row.get(field) or ""))
        for row in rows
        if normalize_text(str(row.get(field) or ""))
    }
    return sorted(values)


def is_non_substantive_value(value: str) -> bool:
    return normalize_text(value).casefold() in NON_SUBSTANTIVE_VALUES


def extract_nt_value(value: str) -> str:
    match = re.search(r"(?:^|;)\s*NT=([^;]+)", value, flags=re.IGNORECASE)
    if match:
        return normalize_text(match.group(1))
    return normalize_text(value)


def canonicalize_existing_value(
    contract: field_fit.FieldContract,
    value: str,
) -> tuple[str, str]:
    if is_non_substantive_value(value):
        return "", "non_substantive"
    return field_fit.canonicalize_value(contract, extract_nt_value(value))


def current_value_search_terms(
    contract: field_fit.FieldContract,
    current_values: list[str],
) -> tuple[str, ...]:
    terms: list[str] = []
    for value in current_values:
        token = extract_nt_value(value)
        if not token or is_non_substantive_value(token):
            continue
        canonical, _ = field_fit.canonicalize_value(contract, token)
        if canonical:
            terms.append(canonical)
            for alias, target in contract.aliases.items():
                if normalize_text(target).casefold() == normalize_text(canonical).casefold():
                    terms.append(alias)
        terms.append(token)
    terms.extend(contract.evidence_search_terms)
    seen: set[str] = set()
    ordered: list[str] = []
    for term in terms:
        normalized = normalize_text(term)
        key = normalized.casefold()
        if normalized and key not in seen:
            seen.add(key)
            ordered.append(normalized)
    return tuple(ordered)


def representative_row_context(
    rows: list[dict[str, str]],
    field: str,
    *,
    max_rows: int = 8,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for index, row in enumerate(rows, start=1):
        payload: dict[str, str] = {}
        for column in (*ROW_CONTEXT_COLUMNS, field):
            if column in row:
                payload[column] = str(row.get(column) or "")
        signature = tuple(sorted(payload.items()))
        if signature in seen:
            continue
        seen.add(signature)
        selected.append({"row_index": index, "values": payload})
        if len(selected) >= max_rows:
            break
    return selected


def find_text_windows(
    *,
    text: str,
    terms: tuple[str, ...],
    window_chars: int,
    max_windows: int,
) -> list[tuple[int, int, str]]:
    lowered = text.casefold()
    candidates: list[tuple[int, int, int]] = []
    half = max(100, window_chars // 2)
    for priority, term in enumerate(terms):
        needle = term.casefold()
        if not needle:
            continue
        positions: list[int] = []
        if re.fullmatch(r"[A-Za-z0-9]+", term) and len(term) <= 6:
            pattern = re.compile(
                rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])",
                flags=re.IGNORECASE,
            )
            positions = [match.start() for match in pattern.finditer(text)]
        else:
            start = 0
            while True:
                idx = lowered.find(needle, start)
                if idx < 0:
                    break
                positions.append(idx)
                start = idx + max(1, len(needle))

        for idx in positions[:max_windows]:
            left = max(0, idx - half)
            right = min(len(text), idx + len(term) + half)
            candidates.append((priority, left, right))

    # Preserve field-specific term priority instead of allowing early generic text to crowd out
    # a later decisive HCD/CID/etc. occurrence. Position breaks ties deterministically.
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    chosen: list[tuple[int, int]] = []
    for _, left, right in candidates:
        overlap = any(
            not (right <= prev_left or left >= prev_right)
            for prev_left, prev_right in chosen
        )
        if overlap:
            continue
        chosen.append((left, right))
        if len(chosen) >= max_windows:
            break
    chosen.sort()
    return [(left, right, text[left:right]) for left, right in chosen]


def registry_evidence_for_field(
    *,
    registry_rows: list[dict[str, str]],
    accession: str,
    field: str,
    contract: field_fit.FieldContract,
    current_values: list[str],
    window_chars: int,
    max_publication_windows: int,
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    accession_rows = [row for row in registry_rows if row.get("accession") == accession]

    for row in accession_rows:
        if row.get("source_kind") != "publication_field_claim":
            continue
        if normalize_text(str(row.get("blocker_field") or "")) != field:
            continue
        artifact_sha = str(row.get("artifact_sha256") or "")
        if not artifact_sha:
            continue
        evidence.append(
            {
                "evidence_ref": f"claim:{artifact_sha}",
                "kind": "publication_field_claim",
                "source_identity": str(row.get("source_identity") or ""),
                "parent_artifact_sha256": str(row.get("parent_artifact_sha256") or ""),
                "text": str(row.get("claim_text") or ""),
                "row_scope": str(row.get("row_scope") or "unknown"),
                "claim_value": str(row.get("claim_value") or ""),
                "claim_status": str(row.get("claim_status") or ""),
            }
        )

    for row in accession_rows:
        if row.get("source_kind") != "publication_fulltext":
            continue
        local_path = Path(str(row.get("local_path") or ""))
        if not local_path.is_file():
            continue
        text = local_path.read_text(encoding="utf-8", errors="replace")
        artifact_sha = str(row.get("artifact_sha256") or sha256_file(local_path))
        for left, right, snippet in find_text_windows(
            text=text,
            terms=current_value_search_terms(contract, current_values),
            window_chars=window_chars,
            max_windows=max_publication_windows,
        ):
            evidence.append(
                {
                    "evidence_ref": f"publication:{artifact_sha}:{left}-{right}",
                    "kind": "publication_text_window",
                    "source_identity": str(row.get("source_identity") or ""),
                    "parent_artifact_sha256": artifact_sha,
                    "text": snippet,
                    "row_scope": "unknown",
                    "source_start": left,
                    "source_end": right,
                }
            )

    # Claims are strongest and appear first. Keep exact evidence refs unique.
    unique: dict[str, dict[str, Any]] = {}
    for item in evidence:
        ref = str(item.get("evidence_ref") or "")
        if ref and ref not in unique:
            unique[ref] = item
    return list(unique.values())


def load_response_fixtures(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    responses: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            field = str(obj.get("field") or "")
            response = obj.get("response")
            if field and isinstance(response, dict):
                responses[field] = response
    return responses


def deterministic_scope_supported(
    *,
    adjudication: dict[str, Any],
    evidence_items: list[dict[str, Any]],
) -> bool:
    if adjudication.get("application_scope") != "all_rows":
        return False
    cited = set(adjudication.get("evidence_refs") or [])
    for item in evidence_items:
        if item.get("evidence_ref") in cited and item.get("row_scope") == "all_rows":
            return True
    return False


def substantive_current_values(current_values: list[str]) -> list[str]:
    return [value for value in current_values if not is_non_substantive_value(value)]


def normalized_existing_values(
    contract: field_fit.FieldContract,
    current_values: list[str],
) -> list[str]:
    normalized: list[str] = []
    for value in substantive_current_values(current_values):
        canonical, _ = canonicalize_existing_value(contract, value)
        normalized.append(canonical or extract_nt_value(value))
    return normalized


def current_values_compatible(
    contract: field_fit.FieldContract,
    current_values: list[str],
    value: str,
) -> bool:
    substantive = normalized_existing_values(contract, current_values)
    if not substantive:
        return True
    canonical, _ = field_fit.canonicalize_value(contract, value)
    target = normalize_text(canonical or value).casefold()
    return all(normalize_text(item).casefold() == target for item in substantive)


def current_value_audit_status(
    *,
    contract: field_fit.FieldContract,
    current_values: list[str],
    adjudication: dict[str, Any],
) -> str:
    substantive = normalized_existing_values(contract, current_values)
    if not substantive:
        return "no_substantive_current_value"

    decision = str(adjudication.get("decision") or "")
    proposed = str(adjudication.get("canonical_value") or adjudication.get("proposed_value") or "")
    if decision == "fits" and proposed:
        proposed_canonical, _ = field_fit.canonicalize_value(contract, proposed)
        target = normalize_text(proposed_canonical or proposed).casefold()
        if all(normalize_text(value).casefold() == target for value in substantive):
            return "current_value_supported_by_supplied_evidence"
        return "current_value_conflicts_with_supplied_evidence"

    model_assessment = str(adjudication.get("current_value_assessment") or "not_assessed")
    if model_assessment == "supported":
        return "current_value_model_supported_but_semantic_value_unresolved"
    if model_assessment == "conflicts":
        return "current_value_conflicts_with_supplied_evidence"
    if model_assessment == "unsupported":
        return "current_value_not_supported_by_supplied_evidence"
    return "current_value_unverified_by_supplied_evidence"


def ensure_field(headers: list[str], rows: list[dict[str, str]], field: str) -> None:
    if field in headers:
        return
    headers.append(field)
    for row in rows:
        row[field] = ""


def fill_blank_or_placeholder_cells(
    rows: list[dict[str, str]],
    field: str,
    value: str,
) -> int:
    changed = 0
    for row in rows:
        current = normalize_text(str(row.get(field) or ""))
        if current and not is_non_substantive_value(current):
            continue
        row[field] = value
        changed += 1
    return changed


def escalation_reason_for(
    *,
    adjudication: dict[str, Any],
    scope_supported: bool,
    values_compatible: bool,
    current_value_audit: str,
) -> str:
    if current_value_audit == "current_value_conflicts_with_supplied_evidence":
        return "current_value_conflicts_with_evidence"
    policy = str(adjudication.get("policy_status") or "")
    if policy == "supported_field_fit" and not values_compatible:
        return "conflicts_with_existing_candidate_values"
    if policy == "supported_field_fit" and not scope_supported:
        return "requires_row_scope_review"
    if policy == "field_fit_requires_vocabulary_review":
        return "requires_vocabulary_review"
    if policy == "context_limit_requires_escalation":
        return "local_model_context_limit"
    if policy == "conflicting_evidence_requires_escalation":
        return "conflicting_evidence"
    if policy == "ambiguous_field_fit":
        return "ambiguous_field_fit"
    if policy == "field_mismatch":
        return "no_matching_evidence_for_target_field"
    if policy == "invalid_evidence_reference":
        return "invalid_local_evidence_reference"
    if policy == "missing_proposed_value":
        return "missing_local_proposed_value"
    if current_value_audit in {
        "current_value_not_supported_by_supplied_evidence",
        "current_value_unverified_by_supplied_evidence",
        "current_value_model_supported_but_semantic_value_unresolved",
    }:
        return "current_value_unverified_by_supplied_evidence"
    return "insufficient_local_evidence"


def external_patch_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": PATCH_SCHEMA_VERSION,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "accession",
            "field",
            "original_value_sha256",
            "action",
            "proposed_value",
            "evidence_refs",
            "semantic_fit",
            "current_value_assessment",
            "vocabulary_status",
            "rationale",
            "abstain",
        ],
        "properties": {
            "accession": {"type": "string"},
            "field": {"type": "string"},
            "original_value_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "action": {
                "type": "string",
                "enum": ["keep", "set", "replace", "clear", "abstain"],
            },
            "proposed_value": {"type": "string"},
            "evidence_refs": {"type": "array", "items": {"type": "string"}},
            "semantic_fit": {
                "type": "string",
                "enum": sorted(field_fit.ALLOWED_DECISIONS),
            },
            "current_value_assessment": {
                "type": "string",
                "enum": [
                    "supported",
                    "unsupported",
                    "conflicts",
                    "insufficient_evidence",
                    "not_present",
                ],
            },
            "vocabulary_status": {
                "type": "string",
                "enum": ["supported", "unsupported", "unknown", "missing", "not_applicable"],
            },
            "rationale": {"type": "string"},
            "abstain": {"type": "boolean"},
        },
    }


def candidate_value_sha256(values: list[str]) -> str:
    return sha256_bytes(canonical_json_bytes(values))


def run_completion(
    *,
    accession: str,
    candidate_sdrf: Path,
    blocker_manifest: Path,
    evidence_registry: Path,
    semantics: Path,
    output: Path,
    model: str,
    ollama_url: str,
    timeout_seconds: float,
    response_fixtures: dict[str, dict[str, Any]],
    window_chars: int,
    max_publication_windows: int,
) -> dict[str, Any]:
    blocker_family, blocker_fields, blocker_state, blocker_reason = parse_blocker_context(
        blocker_manifest, accession
    )
    headers, candidate_rows = read_tsv(candidate_sdrf)
    registry_headers, registry_rows = read_tsv(evidence_registry)
    del registry_headers

    confident_headers = list(headers)
    confident_rows = [dict(row) for row in candidate_rows]
    draft_headers = list(headers)
    draft_rows = [dict(row) for row in candidate_rows]

    accepted_patches: list[dict[str, Any]] = []
    review_rows: list[dict[str, Any]] = []
    escalation_packets: list[dict[str, Any]] = []
    adjudication_records: list[dict[str, Any]] = []
    decisions: list[CompletionDecision] = []

    for field in blocker_fields:
        try:
            contract = field_fit.load_field_contract(semantics, field)
        except KeyError:
            current_values = distinct_current_values(candidate_rows, field)
            decision = CompletionDecision(
                accession=accession,
                blocker_family=blocker_family,
                field=field,
                current_values=tuple(current_values),
                current_value_model_assessment="not_assessed",
                current_value_audit=(
                    "no_substantive_current_value"
                    if not substantive_current_values(current_values)
                    else "current_value_unverified_by_supplied_evidence"
                ),
                current_value_evidence_refs=(),
                model_decision="insufficient_evidence",
                proposed_value="",
                canonical_value="",
                vocabulary_status="missing",
                policy_status="unsupported_field_contract",
                application_scope="unknown",
                deterministic_scope_supported=False,
                confident_patch_status="not_applied",
                local_draft_status="not_applied",
                escalation_reason="unsupported_field_contract",
                evidence_refs=(),
                rationale="No deterministic field-semantics contract is registered.",
                prompt_sha256="",
                field_contract_sha256="",
            )
            decisions.append(decision)
            review_rows.append(asdict(decision))
            escalation_packets.append(
                {
                    "schema_version": BUNDLE_SCHEMA_VERSION,
                    "accession": accession,
                    "blocker_family": blocker_family,
                    "blocker_state": blocker_state,
                    "blocker_reason": blocker_reason,
                    "field": field,
                    "current_distinct_values": current_values,
                    "candidate_row_count": len(candidate_rows),
                    "representative_row_context": representative_row_context(
                        candidate_rows, field
                    ),
                    "escalation_reason": decision.escalation_reason,
                    "field_contract": None,
                    "evidence": [],
                    "local_adjudication": asdict(decision),
                }
            )
            continue

        current_values = distinct_current_values(candidate_rows, field)
        evidence_items = registry_evidence_for_field(
            registry_rows=registry_rows,
            accession=accession,
            field=field,
            contract=contract,
            current_values=current_values,
            window_chars=window_chars,
            max_publication_windows=max_publication_windows,
        )
        prompt = field_fit.build_prompt(
            accession=accession,
            contract=contract,
            current_values=current_values,
            evidence_items=evidence_items,
        )
        if field in response_fixtures:
            model_response = response_fixtures[field]
        elif evidence_items:
            model_response = field_fit.request_ollama(
                base_url=ollama_url,
                model=model,
                prompt=prompt,
                timeout_seconds=timeout_seconds,
            )
        else:
            model_response = {
                "decision": "insufficient_evidence",
                "proposed_value": "",
                "application_scope": "unknown",
                "evidence_refs": [],
                "current_value_assessment": "not_assessed",
                "current_value_evidence_refs": [],
                "rationale": "No field-scoped or publication evidence was available.",
            }

        if not evidence_items:
            adjudication = {
                "schema_version": field_fit.OUTPUT_SCHEMA_VERSION,
                "adjudicator_version": field_fit.VERSION,
                "accession": accession,
                "target_field": field,
                "field_contract_version": contract.resource_version,
                "field_contract_sha256": contract.contract_sha256,
                "prompt_sha256": field_fit.sha256_text(prompt),
                "model": model,
                "structured_json": True,
                "non_editing": True,
                "think": False,
                "decision": "insufficient_evidence",
                "proposed_value": "",
                "application_scope": "unknown",
                "evidence_refs": [],
                "current_value_assessment": "not_assessed",
                "current_value_evidence_refs": [],
                "rationale": "No field-scoped or publication evidence was available.",
                "canonical_value": "",
                "vocabulary_status": "missing",
                "policy_status": "insufficient_evidence",
                "requires_human_review": True,
                "invalid_evidence_refs": [],
            }
        else:
            adjudication = field_fit.adjudicate(
                accession=accession,
                contract=contract,
                current_values=current_values,
                evidence_items=evidence_items,
                model=model,
                model_response=model_response,
            )

        scope_supported = deterministic_scope_supported(
            adjudication=adjudication,
            evidence_items=evidence_items,
        )
        canonical = str(adjudication.get("canonical_value") or "")
        proposed = str(adjudication.get("proposed_value") or "")
        values_compatible = current_values_compatible(
            contract, current_values, canonical or proposed
        )
        current_audit = current_value_audit_status(
            contract=contract,
            current_values=current_values,
            adjudication=adjudication,
        )
        adjudication = {
            **adjudication,
            "deterministic_current_value_audit": current_audit,
        }
        adjudication_records.append(
            {
                "field": field,
                "response": model_response,
                "adjudication": adjudication,
            }
        )

        accepted = (
            adjudication.get("policy_status") == "supported_field_fit"
            and scope_supported
            and values_compatible
            and bool(canonical)
        )
        draft_applicable = (
            adjudication.get("decision") == "fits"
            and adjudication.get("application_scope") == "all_rows"
            and bool(proposed)
        )

        confident_status = "not_applied"
        draft_status = "not_applied"
        escalation_reason = ""

        if accepted:
            ensure_field(confident_headers, confident_rows, field)
            changed = fill_blank_or_placeholder_cells(confident_rows, field, canonical)
            confident_status = f"applied_to_{changed}_blank_rows"
            accepted_patches.append(
                {
                    "schema_version": "pride-scp-sdrf-accepted-patch-v1",
                    "accession": accession,
                    "field": field,
                    "row_selector": {"type": "all_blank_or_placeholder_rows"},
                    "original_value_sha256": candidate_value_sha256(current_values),
                    "proposed_value": canonical,
                    "evidence_refs": list(adjudication.get("evidence_refs") or []),
                    "field_contract_sha256": contract.contract_sha256,
                    "prompt_sha256": str(adjudication.get("prompt_sha256") or ""),
                    "model": model,
                    "policy_status": str(adjudication.get("policy_status") or ""),
                }
            )

        if draft_applicable:
            draft_value = canonical if adjudication.get("vocabulary_status") == "supported" else proposed
            ensure_field(draft_headers, draft_rows, field)
            changed = fill_blank_or_placeholder_cells(draft_rows, field, draft_value)
            draft_status = f"review_only_applied_to_{changed}_blank_rows"

        if not accepted:
            escalation_reason = escalation_reason_for(
                adjudication=adjudication,
                scope_supported=scope_supported,
                values_compatible=values_compatible,
                current_value_audit=current_audit,
            )

        decision = CompletionDecision(
            accession=accession,
            blocker_family=blocker_family,
            field=field,
            current_values=tuple(current_values),
            current_value_model_assessment=str(
                adjudication.get("current_value_assessment") or "not_assessed"
            ),
            current_value_audit=current_audit,
            current_value_evidence_refs=tuple(
                str(ref)
                for ref in adjudication.get("current_value_evidence_refs") or []
            ),
            model_decision=str(adjudication.get("decision") or ""),
            proposed_value=proposed,
            canonical_value=canonical,
            vocabulary_status=str(adjudication.get("vocabulary_status") or ""),
            policy_status=str(adjudication.get("policy_status") or ""),
            application_scope=str(adjudication.get("application_scope") or "unknown"),
            deterministic_scope_supported=scope_supported,
            confident_patch_status=confident_status,
            local_draft_status=draft_status,
            escalation_reason=escalation_reason,
            evidence_refs=tuple(str(ref) for ref in adjudication.get("evidence_refs") or []),
            rationale=str(adjudication.get("rationale") or ""),
            prompt_sha256=str(adjudication.get("prompt_sha256") or ""),
            field_contract_sha256=contract.contract_sha256,
        )
        decisions.append(decision)
        review_rows.append(asdict(decision))

        if escalation_reason:
            escalation_packets.append(
                {
                    "schema_version": BUNDLE_SCHEMA_VERSION,
                    "accession": accession,
                    "blocker_family": blocker_family,
                    "blocker_state": blocker_state,
                    "blocker_reason": blocker_reason,
                    "field": field,
                    "current_distinct_values": current_values,
                    "current_value_audit": current_audit,
                    "candidate_row_count": len(candidate_rows),
                    "representative_row_context": representative_row_context(
                        candidate_rows, field
                    ),
                    "original_value_sha256": candidate_value_sha256(current_values),
                    "escalation_reason": escalation_reason,
                    "field_contract": {
                        "field": field,
                        "semantic_definition": contract.semantic_definition,
                        "accepted_values": list(contract.accepted_values),
                        "aliases": contract.aliases,
                        "known_explicit_but_unsupported": list(
                            contract.known_explicit_but_unsupported
                        ),
                        "explicit_exclusions": list(contract.explicit_exclusions),
                        "targeted_evidence_search_terms": list(
                            current_value_search_terms(contract, current_values)
                        ),
                        "contract_sha256": contract.contract_sha256,
                        "version": contract.resource_version,
                    },
                    "evidence": evidence_items,
                    "local_adjudication": adjudication,
                    "external_model_instruction": (
                        "Resolve only this field from the supplied evidence and candidate row context. "
                        "Audit the existing value as well as any proposed replacement. Return a structured "
                        "patch matching escalation_patch.schema.json. Abstain rather than inventing a value."
                    ),
                }
            )

    output.mkdir(parents=True, exist_ok=True)
    confident_path = output / "confident_candidate.sdrf.tsv"
    draft_path = output / "local_best_effort.sdrf.tsv"
    write_tsv(confident_path, confident_headers, confident_rows)
    write_tsv(draft_path, draft_headers, draft_rows)

    patch_path = output / "accepted_patch.jsonl"
    review_path = output / "review_overlay.tsv"
    evidence_packet_path = output / "evidence_packet.jsonl"
    schema_path = output / "escalation_patch.schema.json"
    adjudication_path = output / "local_adjudications.jsonl"
    provenance_path = output / "provenance.json"
    summary_path = output / "completion_summary.json"
    readme_path = output / "README_REVIEW.txt"

    write_jsonl(patch_path, accepted_patches)
    review_headers = [
        "accession",
        "blocker_family",
        "field",
        "current_values",
        "current_value_model_assessment",
        "current_value_audit",
        "current_value_evidence_refs",
        "model_decision",
        "proposed_value",
        "canonical_value",
        "vocabulary_status",
        "policy_status",
        "application_scope",
        "deterministic_scope_supported",
        "confident_patch_status",
        "local_draft_status",
        "escalation_reason",
        "evidence_refs",
        "rationale",
        "prompt_sha256",
        "field_contract_sha256",
    ]
    review_serialized: list[dict[str, Any]] = []
    for row in review_rows:
        review_serialized.append(
            {
                **row,
                "current_values": ";".join(row.get("current_values") or []),
                "evidence_refs": ";".join(row.get("evidence_refs") or []),
                "current_value_evidence_refs": ";".join(
                    row.get("current_value_evidence_refs") or []
                ),
                "deterministic_scope_supported": str(
                    bool(row.get("deterministic_scope_supported"))
                ).lower(),
            }
        )
    write_tsv(review_path, review_headers, review_serialized)
    write_jsonl(evidence_packet_path, escalation_packets)
    write_json(schema_path, external_patch_schema())
    write_jsonl(adjudication_path, adjudication_records)

    provenance = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "completion_version": VERSION,
        "field_fit_adjudicator_version": field_fit.VERSION,
        "accession": accession,
        "blocker_family": blocker_family,
        "blocker_state": blocker_state,
        "blocker_reason": blocker_reason,
        "blocker_fields": blocker_fields,
        "model": model,
        "candidate_sdrf": str(candidate_sdrf),
        "candidate_sdrf_sha256": sha256_file(candidate_sdrf),
        "blocker_manifest": str(blocker_manifest),
        "blocker_manifest_sha256": sha256_file(blocker_manifest),
        "evidence_registry": str(evidence_registry),
        "evidence_registry_sha256": sha256_file(evidence_registry),
        "field_semantics": str(semantics),
        "field_semantics_sha256": sha256_file(semantics),
        "outputs": {
            "confident_candidate": str(confident_path),
            "confident_candidate_sha256": sha256_file(confident_path),
            "local_best_effort": str(draft_path),
            "local_best_effort_sha256": sha256_file(draft_path),
            "accepted_patch": str(patch_path),
            "review_overlay": str(review_path),
            "evidence_packet": str(evidence_packet_path),
            "escalation_patch_schema": str(schema_path),
            "local_adjudications": str(adjudication_path),
        },
    }
    write_json(provenance_path, provenance)

    summary = {
        "version": VERSION,
        "accession": accession,
        "blocker_family": blocker_family,
        "blocker_state": blocker_state,
        "blocker_reason": blocker_reason,
        "blocker_fields": len(blocker_fields),
        "local_fields_attempted": len(decisions),
        "accepted_confident_patches": len(accepted_patches),
        "local_review_proposals": sum(
            1 for decision in decisions if decision.local_draft_status != "not_applied"
        ),
        "escalation_items": len(escalation_packets),
        "context_limit_items": sum(
            1 for decision in decisions if decision.model_decision == "context_limit"
        ),
        "ambiguous_items": sum(
            1
            for decision in decisions
            if decision.model_decision in {"ambiguous", "conflicting_evidence"}
        ),
        "existing_value_audit_items": sum(
            1
            for decision in decisions
            if decision.current_value_audit != "no_substantive_current_value"
        ),
        "existing_value_conflicts": sum(
            1
            for decision in decisions
            if decision.current_value_audit
            == "current_value_conflicts_with_supplied_evidence"
        ),
        "outputs": provenance["outputs"],
        "provenance": str(provenance_path),
    }
    write_json(summary_path, summary)

    readme_path.write_text(
        "PRIDE-SCP local LLM completion review bundle\n\n"
        "confident_candidate.sdrf.tsv contains only deterministically accepted patches.\n"
        "local_best_effort.sdrf.tsv is REVIEW-ONLY and may contain unsupported or unvalidated "
        "local-model proposals. Never submit or promote it directly.\n"
        "evidence_packet.jsonl contains only unresolved/escalated fields and is intended for a "
        "larger external adjudicator.\n"
        "Any external patch must be deterministically verified before being applied.\n",
        encoding="utf-8",
    )
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accession")
    parser.add_argument("--candidate-sdrf", type=Path)
    parser.add_argument("--blocker-manifest", type=Path)
    parser.add_argument("--evidence-registry", type=Path)
    parser.add_argument(
        "--semantics",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "resources"
        / "sdrf_field_semantics_v1.json",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model", default=field_fit.DEFAULT_MODEL)
    parser.add_argument("--ollama-url", default=field_fit.DEFAULT_OLLAMA_URL)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--responses-jsonl", type=Path)
    parser.add_argument("--window-chars", type=int, default=1200)
    parser.add_argument("--max-publication-windows", type=int, default=8)
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args()


def self_test() -> None:
    contract = field_fit.load_field_contract(
        Path(__file__).resolve().parents[1] / "resources" / "sdrf_field_semantics_v1.json",
        "comment[dissociation method]",
    )
    assert current_values_compatible(contract, [], "HCD")
    assert current_values_compatible(contract, ["HCD"], "HCD")
    assert current_values_compatible(contract, ["NT=HCD;AC=MS:1000422"], "HCD")
    assert not current_values_compatible(contract, ["CID"], "HCD")
    assert find_text_windows(
        text="abc HCD def",
        terms=("HCD",),
        window_chars=100,
        max_windows=2,
    )
    schema = external_patch_schema()
    assert schema["title"] == PATCH_SCHEMA_VERSION
    print("sdrf_llm_completion self-test: PASS")


def main() -> None:
    args = parse_args()
    if args.self_test:
        self_test()
        return
    required = {
        "--accession": args.accession,
        "--candidate-sdrf": args.candidate_sdrf,
        "--blocker-manifest": args.blocker_manifest,
        "--evidence-registry": args.evidence_registry,
        "--output": args.output,
    }
    missing = [flag for flag, value in required.items() if not value]
    if missing:
        raise SystemExit(f"missing required arguments: {', '.join(missing)}")

    response_fixtures = load_response_fixtures(args.responses_jsonl)
    summary = run_completion(
        accession=str(args.accession),
        candidate_sdrf=args.candidate_sdrf,
        blocker_manifest=args.blocker_manifest,
        evidence_registry=args.evidence_registry,
        semantics=args.semantics,
        output=args.output,
        model=args.model,
        ollama_url=args.ollama_url,
        timeout_seconds=args.timeout_seconds,
        response_fixtures=response_fixtures,
        window_chars=args.window_chars,
        max_publication_windows=args.max_publication_windows,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
