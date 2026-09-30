#!/usr/bin/env python3
"""Audit observed single-cell isolation/collection vocabulary across PRIDE-SCP artifacts.

This tool is intentionally non-editing. It inventories observed values and evidence-backed
phrases for ``characteristics[single cell isolation protocol]`` so the local PRIDE-SCP
representation policy and the upstream single-cell SDRF template can be improved from corpus
evidence rather than accession-specific exceptions.

Evidence hierarchy used by the audit:

* ``publication_field_claim`` records are evidence-backed observations;
* claims embedded in escalation bundles are evidence-backed observations;
* current SDRF values are observations of the candidate artifact, not independent evidence;
* optional full-text exact-term hits are discovery-only and remain ambiguous until promoted by
  a claim/review step.

The audit never changes an SDRF and never promotes readiness.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

VERSION = "pride-scp-sdrf-isolation-vocabulary-audit-v0.1.0"
SCHEMA_VERSION = "pride-scp-sdrf-isolation-vocabulary-observation-v1"
TARGET_FIELD = "characteristics[single cell isolation protocol]"
ACCESSION_RE = re.compile(r"\b(PXD\d{6})\b", re.IGNORECASE)
NON_SUBSTANTIVE_VALUES = {
    "",
    "na",
    "n/a",
    "not applicable",
    "not available",
    "unknown",
}
EVIDENCE_BACKED_KINDS = {
    "publication_field_claim",
    "escalation_publication_field_claim",
}
SEMANTIC_MISMATCH_STATUSES = {
    "semantic_mismatch",
    "field_mismatch",
    "does_not_fit",
    "unsupported_field",
}


@dataclass(frozen=True)
class FieldContract:
    version: str
    field: str
    preferred_values: tuple[str, ...]
    aliases: dict[str, str]
    known_extensions: tuple[str, ...]
    explicit_exclusions: tuple[str, ...]


@dataclass(frozen=True)
class Observation:
    schema_version: str
    accession: str
    exact_source_phrase: str
    normalized_phrase: str
    mapping_class: str
    source_kind: str
    evidence_backed: bool
    source_identity: str
    evidence_ref: str
    artifact_sha256: str
    row_scope: str
    current_sdrf_value: str
    value_row_count: int
    candidate_row_count: int
    claim_status: str
    representative_evidence_text: str
    source_path: str
    review_note: str


def normalize_text(value: str) -> str:
    return " ".join(str(value).strip().split())


def case_key(value: str) -> str:
    return normalize_text(value).casefold()


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


def extract_nt_value(value: str) -> str:
    value = normalize_text(value)
    match = re.search(r"(?:^|;)\s*NT=([^;]+)", value, flags=re.IGNORECASE)
    if match:
        return normalize_text(match.group(1))
    return value


def is_non_substantive(value: str) -> bool:
    return case_key(extract_nt_value(value)) in NON_SUBSTANTIVE_VALUES


def read_tsv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        return list(reader.fieldnames or []), [dict(row) for row in reader]


def write_tsv(path: Path, headers: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({header: row.get(header, "") for header in headers})


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_contract(path: Path, field: str = TARGET_FIELD) -> FieldContract:
    payload = json.loads(path.read_text(encoding="utf-8"))
    fields = payload.get("fields")
    if not isinstance(fields, dict) or field not in fields:
        raise KeyError(f"field missing from semantics resource: {field}")
    raw = fields[field]
    if not isinstance(raw, dict):
        raise TypeError(f"field contract is not an object: {field}")
    preferred = raw.get("preferred_values", raw.get("accepted_values", []))
    aliases = raw.get("aliases", {})
    extensions = raw.get("known_explicit_but_unsupported", raw.get("known_extensions", []))
    exclusions = raw.get("explicit_exclusions", [])
    if not isinstance(preferred, list):
        raise TypeError("preferred/accepted values must be a list")
    if not isinstance(aliases, dict):
        raise TypeError("aliases must be an object")
    if not isinstance(extensions, list):
        raise TypeError("known extensions must be a list")
    if not isinstance(exclusions, list):
        raise TypeError("explicit exclusions must be a list")
    return FieldContract(
        version=str(payload.get("version") or payload.get("schema_version") or ""),
        field=field,
        preferred_values=tuple(normalize_text(item) for item in preferred if normalize_text(item)),
        aliases={normalize_text(key): normalize_text(value) for key, value in aliases.items()},
        known_extensions=tuple(normalize_text(item) for item in extensions if normalize_text(item)),
        explicit_exclusions=tuple(normalize_text(item) for item in exclusions if normalize_text(item)),
    )


def preferred_lookup(contract: FieldContract) -> dict[str, str]:
    return {case_key(value): value for value in contract.preferred_values}


def alias_lookup(contract: FieldContract) -> dict[str, str]:
    return {case_key(alias): target for alias, target in contract.aliases.items()}


def extension_lookup(contract: FieldContract) -> dict[str, str]:
    return {case_key(value): value for value in contract.known_extensions}


def classify_phrase(
    *,
    phrase: str,
    contract: FieldContract,
    source_kind: str,
    claim_status: str = "",
) -> tuple[str, str, bool, str]:
    """Return (normalized phrase, mapping class, evidence-backed flag, review note)."""
    raw = extract_nt_value(phrase)
    key = case_key(raw)
    evidence_backed = source_kind in EVIDENCE_BACKED_KINDS

    if not raw or key in NON_SUBSTANTIVE_VALUES:
        return raw, "ambiguous", evidence_backed, "non_substantive_placeholder"

    preferred = preferred_lookup(contract)
    if key in preferred:
        return preferred[key], "canonical_preferred", evidence_backed, ""

    aliases = alias_lookup(contract)
    if key in aliases:
        return aliases[key], "canonical_alias", evidence_backed, ""

    if case_key(claim_status) in SEMANTIC_MISMATCH_STATUSES:
        return raw, "semantic_mismatch", evidence_backed, f"claim_status={claim_status}"

    for exclusion in contract.explicit_exclusions:
        if key == case_key(exclusion):
            return raw, "semantic_mismatch", evidence_backed, "matches_explicit_exclusion"

    if evidence_backed:
        known = extension_lookup(contract)
        if key in known:
            return known[key], "evidence_backed_extension", True, "known_noncanonical_extension"
        return raw, "evidence_backed_extension", True, "evidence_backed_noncanonical_value"

    if source_kind == "publication_text_hit":
        return raw, "ambiguous", False, "fulltext_hit_requires_claim_or_review"

    return raw, "ambiguous", False, "candidate_value_requires_independent_evidence"


def accession_from_path(path: Path) -> str:
    for part in reversed(path.parts):
        match = ACCESSION_RE.search(part)
        if match:
            return match.group(1).upper()
    match = ACCESSION_RE.search(str(path))
    return match.group(1).upper() if match else ""


def observation_from_claim(
    *,
    row: dict[str, Any],
    source_path: Path,
    contract: FieldContract,
    source_kind: str = "publication_field_claim",
) -> Observation | None:
    accession = str(row.get("accession") or "").upper()
    field = normalize_text(str(row.get("blocker_field") or row.get("field") or ""))
    phrase = normalize_text(str(row.get("claim_value") or row.get("proposed_value") or ""))
    if not accession or field != contract.field or not phrase:
        return None
    claim_status = normalize_text(str(row.get("claim_status") or ""))
    normalized, mapping_class, evidence_backed, review_note = classify_phrase(
        phrase=phrase,
        contract=contract,
        source_kind=source_kind,
        claim_status=claim_status,
    )
    artifact_sha = normalize_text(str(row.get("artifact_sha256") or ""))
    evidence_ref = normalize_text(str(row.get("evidence_ref") or ""))
    if not evidence_ref and artifact_sha:
        evidence_ref = f"claim:{artifact_sha}"
    text = normalize_text(str(row.get("claim_text") or row.get("text") or ""))
    return Observation(
        schema_version=SCHEMA_VERSION,
        accession=accession,
        exact_source_phrase=phrase,
        normalized_phrase=normalized,
        mapping_class=mapping_class,
        source_kind=source_kind,
        evidence_backed=evidence_backed,
        source_identity=normalize_text(str(row.get("source_identity") or "")),
        evidence_ref=evidence_ref,
        artifact_sha256=artifact_sha,
        row_scope=normalize_text(str(row.get("row_scope") or "unknown")) or "unknown",
        current_sdrf_value="",
        value_row_count=0,
        candidate_row_count=0,
        claim_status=claim_status,
        representative_evidence_text=text,
        source_path=str(source_path),
        review_note=review_note,
    )


def observations_from_registry(path: Path, contract: FieldContract) -> list[Observation]:
    _, rows = read_tsv(path)
    observations: list[Observation] = []
    for row in rows:
        if normalize_text(str(row.get("source_kind") or "")) != "publication_field_claim":
            continue
        observation = observation_from_claim(row=row, source_path=path, contract=contract)
        if observation is not None:
            observations.append(observation)
    return observations


def observations_from_candidate(
    path: Path,
    contract: FieldContract,
    *,
    accession_override: str = "",
) -> list[Observation]:
    headers, rows = read_tsv(path)
    if contract.field not in headers:
        return []
    accession = accession_override.upper() or accession_from_path(path)
    if not accession:
        return []
    counts: dict[str, int] = defaultdict(int)
    exact_values: dict[str, str] = {}
    for row in rows:
        exact = normalize_text(str(row.get(contract.field) or ""))
        if not exact:
            continue
        key = case_key(exact)
        counts[key] += 1
        exact_values.setdefault(key, exact)
    observations: list[Observation] = []
    total = len(rows)
    for key in sorted(counts):
        phrase = exact_values[key]
        normalized, mapping_class, evidence_backed, review_note = classify_phrase(
            phrase=phrase,
            contract=contract,
            source_kind="candidate_sdrf_value",
        )
        value_count = counts[key]
        row_scope = "all_rows" if total > 0 and value_count == total else "subset"
        observations.append(
            Observation(
                schema_version=SCHEMA_VERSION,
                accession=accession,
                exact_source_phrase=extract_nt_value(phrase),
                normalized_phrase=normalized,
                mapping_class=mapping_class,
                source_kind="candidate_sdrf_value",
                evidence_backed=evidence_backed,
                source_identity="",
                evidence_ref="",
                artifact_sha256=sha256_file(path),
                row_scope=row_scope,
                current_sdrf_value=phrase,
                value_row_count=value_count,
                candidate_row_count=total,
                claim_status="",
                representative_evidence_text="",
                source_path=str(path),
                review_note=review_note,
            )
        )
    return observations


def observations_from_escalation(path: Path, contract: FieldContract) -> list[Observation]:
    observations: list[Observation] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            packet = json.loads(line)
            if normalize_text(str(packet.get("field") or "")) != contract.field:
                continue
            accession = str(packet.get("accession") or "").upper()
            for evidence in packet.get("evidence") or []:
                if not isinstance(evidence, dict):
                    continue
                if str(evidence.get("kind") or "") != "publication_field_claim":
                    continue
                row = dict(evidence)
                row["accession"] = accession
                row["field"] = contract.field
                observation = observation_from_claim(
                    row=row,
                    source_path=path,
                    contract=contract,
                    source_kind="escalation_publication_field_claim",
                )
                if observation is not None:
                    observations.append(observation)
    return observations


def exact_term_positions(text: str, term: str) -> list[int]:
    if re.fullmatch(r"[A-Za-z0-9]+", term) and len(term) <= 6:
        pattern = re.compile(
            rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])",
            flags=re.IGNORECASE,
        )
        return [match.start() for match in pattern.finditer(text)]
    lowered = text.casefold()
    needle = term.casefold()
    positions: list[int] = []
    start = 0
    while needle:
        idx = lowered.find(needle, start)
        if idx < 0:
            break
        positions.append(idx)
        start = idx + max(1, len(needle))
    return positions


def fulltext_hit_observations(
    *,
    registry: Path,
    contract: FieldContract,
    seed_terms: tuple[str, ...],
    window_chars: int,
    max_hits_per_term: int,
) -> list[Observation]:
    _, rows = read_tsv(registry)
    terms: list[str] = []
    seen: set[str] = set()
    for term in (*contract.known_extensions, *seed_terms):
        term = normalize_text(term)
        key = case_key(term)
        if term and key not in seen:
            seen.add(key)
            terms.append(term)
    observations: list[Observation] = []
    for row in rows:
        if normalize_text(str(row.get("source_kind") or "")) != "publication_fulltext":
            continue
        accession = str(row.get("accession") or "").upper()
        local_path = Path(str(row.get("local_path") or ""))
        if not accession or not local_path.is_file():
            continue
        text = local_path.read_text(encoding="utf-8", errors="replace")
        artifact_sha = normalize_text(str(row.get("artifact_sha256") or "")) or sha256_file(local_path)
        for term in terms:
            positions = exact_term_positions(text, term)[:max_hits_per_term]
            for pos in positions:
                half = max(100, window_chars // 2)
                left = max(0, pos - half)
                right = min(len(text), pos + len(term) + half)
                snippet = normalize_text(text[left:right])
                normalized, mapping_class, evidence_backed, review_note = classify_phrase(
                    phrase=term,
                    contract=contract,
                    source_kind="publication_text_hit",
                )
                observations.append(
                    Observation(
                        schema_version=SCHEMA_VERSION,
                        accession=accession,
                        exact_source_phrase=term,
                        normalized_phrase=normalized,
                        mapping_class=mapping_class,
                        source_kind="publication_text_hit",
                        evidence_backed=evidence_backed,
                        source_identity=normalize_text(str(row.get("source_identity") or "")),
                        evidence_ref=f"publication:{artifact_sha}:{left}-{right}",
                        artifact_sha256=artifact_sha,
                        row_scope="unknown",
                        current_sdrf_value="",
                        value_row_count=0,
                        candidate_row_count=0,
                        claim_status="",
                        representative_evidence_text=snippet,
                        source_path=str(local_path),
                        review_note=review_note,
                    )
                )
    return observations


def deduplicate_observations(observations: Iterable[Observation]) -> list[Observation]:
    unique: dict[tuple[str, ...], Observation] = {}
    for observation in observations:
        key = (
            observation.accession,
            case_key(observation.exact_source_phrase),
            observation.source_kind,
            observation.evidence_ref,
            observation.source_path,
            observation.row_scope,
        )
        unique.setdefault(key, observation)
    return sorted(
        unique.values(),
        key=lambda item: (
            item.accession,
            item.mapping_class,
            case_key(item.normalized_phrase),
            item.source_kind,
            item.evidence_ref,
        ),
    )



def candidates_from_manifest(path: Path) -> list[tuple[str, Path]]:
    _, rows = read_tsv(path)
    entries: list[tuple[str, Path]] = []
    for row in rows:
        accession = str(row.get("accession") or "").strip().upper()
        raw_path = str(
            row.get("candidate_sdrf")
            or row.get("candidate_path")
            or row.get("sdrf_path")
            or row.get("local_path")
            or row.get("path")
            or ""
        ).strip()
        if not accession or not ACCESSION_RE.fullmatch(accession) or not raw_path:
            continue
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = (path.parent / candidate).resolve()
        if candidate.is_file():
            entries.append((accession, candidate.resolve()))
    return entries

def discover_candidate_files(roots: Iterable[Path], explicit: Iterable[Path]) -> list[Path]:
    found: set[Path] = {path.resolve() for path in explicit if path.is_file()}
    for root in roots:
        if root.is_file():
            found.add(root.resolve())
            continue
        if not root.is_dir():
            continue
        for path in root.rglob("*.sdrf.tsv"):
            if path.is_file() and accession_from_path(path):
                found.add(path.resolve())
    return sorted(found)


def summarize(observations: list[Observation]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[Observation]] = defaultdict(list)
    for observation in observations:
        key = (case_key(observation.normalized_phrase), observation.mapping_class)
        grouped[key].append(observation)

    rows: list[dict[str, Any]] = []
    for (_, mapping_class), items in sorted(
        grouped.items(),
        key=lambda item: (item[0][1], item[0][0]),
    ):
        accessions = sorted({item.accession for item in items if item.accession})
        exact_phrases = sorted({item.exact_source_phrase for item in items if item.exact_source_phrase})
        source_kinds = sorted({item.source_kind for item in items})
        evidence_refs = sorted({item.evidence_ref for item in items if item.evidence_ref})
        source_identities = sorted({item.source_identity for item in items if item.source_identity})
        row_scopes = sorted({item.row_scope for item in items if item.row_scope})
        rows.append(
            {
                "normalized_phrase": items[0].normalized_phrase,
                "mapping_class": mapping_class,
                "accession_count": len(accessions),
                "observation_count": len(items),
                "evidence_backed_observation_count": sum(item.evidence_backed for item in items),
                "accessions": ";".join(accessions),
                "exact_source_phrases": ";".join(exact_phrases),
                "source_kinds": ";".join(source_kinds),
                "source_identities": ";".join(source_identities),
                "row_scopes": ";".join(row_scopes),
                "evidence_refs": ";".join(evidence_refs),
            }
        )
    return rows


def build_extensions_payload(
    *,
    observations: list[Observation],
    contract: FieldContract,
) -> dict[str, Any]:
    grouped: dict[str, list[Observation]] = defaultdict(list)
    for observation in observations:
        if observation.mapping_class != "evidence_backed_extension":
            continue
        grouped[case_key(observation.normalized_phrase)].append(observation)

    extensions: list[dict[str, Any]] = []
    for key in sorted(grouped):
        items = grouped[key]
        accessions = sorted({item.accession for item in items})
        extensions.append(
            {
                "normalized_phrase": items[0].normalized_phrase,
                "accession_count": len(accessions),
                "accessions": accessions,
                "exact_source_phrases": sorted(
                    {item.exact_source_phrase for item in items if item.exact_source_phrase}
                ),
                "source_identities": sorted(
                    {item.source_identity for item in items if item.source_identity}
                ),
                "evidence_refs": sorted({item.evidence_ref for item in items if item.evidence_ref}),
                "row_scopes": sorted({item.row_scope for item in items if item.row_scope}),
                "representative_evidence_text": next(
                    (
                        item.representative_evidence_text
                        for item in items
                        if item.representative_evidence_text
                    ),
                    "",
                ),
            }
        )

    return {
        "schema_version": "pride-scp-sdrf-isolation-vocabulary-extensions-v1",
        "audit_version": VERSION,
        "field_contract_version": contract.version,
        "target_field": contract.field,
        "preferred_values": list(contract.preferred_values),
        "extensions": extensions,
    }


def render_report(
    *,
    observations: list[Observation],
    summary_rows: list[dict[str, Any]],
    extensions_payload: dict[str, Any],
    contract: FieldContract,
) -> str:
    accession_count = len({item.accession for item in observations if item.accession})
    class_counts: dict[str, int] = defaultdict(int)
    for item in observations:
        class_counts[item.mapping_class] += 1

    lines = [
        "# PRIDE-SCP single-cell isolation vocabulary gap audit",
        "",
        f"- Audit version: `{VERSION}`",
        f"- Field contract: `{contract.version}`",
        f"- Target field: `{contract.field}`",
        f"- Accessions observed: **{accession_count}**",
        f"- Observations: **{len(observations)}**",
        f"- Evidence-backed extensions: **{len(extensions_payload['extensions'])}**",
        "",
        "## Mapping classes",
        "",
    ]
    for mapping_class in (
        "canonical_preferred",
        "canonical_alias",
        "evidence_backed_extension",
        "semantic_mismatch",
        "ambiguous",
    ):
        lines.append(f"- `{mapping_class}`: {class_counts.get(mapping_class, 0)}")

    lines.extend(["", "## Evidence-backed extension candidates", ""])
    extensions = extensions_payload["extensions"]
    if not extensions:
        lines.append("No evidence-backed extension candidates were observed.")
    else:
        lines.extend(
            [
                "| Normalized phrase | Accessions | Row scopes | Evidence refs |",
                "| --- | --- | --- | --- |",
            ]
        )
        for item in extensions:
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(item["normalized_phrase"]).replace("|", "\\|"),
                        ", ".join(item["accessions"]),
                        ", ".join(item["row_scopes"]),
                        str(len(item["evidence_refs"])),
                    ]
                )
                + " |"
            )

    ambiguous = [row for row in summary_rows if row["mapping_class"] == "ambiguous"]
    lines.extend(["", "## Ambiguous / discovery-only observations", ""])
    if not ambiguous:
        lines.append("None.")
    else:
        lines.extend(
            [
                "| Phrase | Accessions | Sources |",
                "| --- | --- | --- |",
            ]
        )
        for row in ambiguous:
            lines.append(
                f"| {str(row['normalized_phrase']).replace('|', '\\|')} | "
                f"{row['accessions']} | {row['source_kinds']} |"
            )

    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            (
                "`canonical_preferred` and `canonical_alias` describe compatibility with the current "
                "preferred/template vocabulary. `evidence_backed_extension` means trusted evidence "
                "contains a field-relevant noncanonical phrase; it is a candidate for local policy and "
                "upstream-template review, not an automatic SDRF edit. Candidate-only and raw full-text "
                "hits remain `ambiguous` until independently supported."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def input_provenance(paths: Iterable[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted({item.resolve() for item in paths if item.is_file()}):
        rows.append(
            {
                "path": str(path),
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
        )
    return rows


def run_audit(
    *,
    semantics: Path,
    output_dir: Path,
    evidence_registries: tuple[Path, ...],
    candidate_roots: tuple[Path, ...],
    candidates: tuple[Path, ...],
    candidate_manifests: tuple[Path, ...],
    escalation_jsonl: tuple[Path, ...],
    scan_publication_fulltext: bool,
    seed_terms: tuple[str, ...],
    window_chars: int,
    max_hits_per_term: int,
) -> dict[str, Any]:
    contract = load_contract(semantics)
    observations: list[Observation] = []

    for registry in evidence_registries:
        observations.extend(observations_from_registry(registry, contract))
        if scan_publication_fulltext:
            observations.extend(
                fulltext_hit_observations(
                    registry=registry,
                    contract=contract,
                    seed_terms=seed_terms,
                    window_chars=window_chars,
                    max_hits_per_term=max_hits_per_term,
                )
            )

    candidate_files = discover_candidate_files(candidate_roots, candidates)
    candidate_manifest_entries: list[tuple[str, Path]] = []
    for manifest in candidate_manifests:
        candidate_manifest_entries.extend(candidates_from_manifest(manifest))

    manifest_paths = {path for _, path in candidate_manifest_entries}
    for candidate in candidate_files:
        if candidate in manifest_paths:
            continue
        observations.extend(observations_from_candidate(candidate, contract))
    for accession, candidate in candidate_manifest_entries:
        observations.extend(
            observations_from_candidate(
                candidate,
                contract,
                accession_override=accession,
            )
        )

    candidate_files = sorted(set(candidate_files) | manifest_paths)

    for packet in escalation_jsonl:
        observations.extend(observations_from_escalation(packet, contract))

    observations = deduplicate_observations(observations)
    summary_rows = summarize(observations)
    extensions_payload = build_extensions_payload(observations=observations, contract=contract)

    output_dir.mkdir(parents=True, exist_ok=True)
    observations_path = output_dir / "isolation_vocabulary_observations.tsv"
    summary_path = output_dir / "isolation_vocabulary_summary.tsv"
    extensions_path = output_dir / "isolation_vocabulary_extensions.json"
    report_path = output_dir / "isolation_vocabulary_report.md"
    provenance_path = output_dir / "provenance.json"

    observation_rows = []
    for observation in observations:
        row = asdict(observation)
        row["evidence_backed"] = "true" if observation.evidence_backed else "false"
        observation_rows.append(row)
    write_tsv(
        observations_path,
        list(asdict(observations[0]).keys()) if observations else list(Observation.__annotations__),
        observation_rows,
    )
    summary_headers = [
        "normalized_phrase",
        "mapping_class",
        "accession_count",
        "observation_count",
        "evidence_backed_observation_count",
        "accessions",
        "exact_source_phrases",
        "source_kinds",
        "source_identities",
        "row_scopes",
        "evidence_refs",
    ]
    write_tsv(summary_path, summary_headers, summary_rows)
    write_json(extensions_path, extensions_payload)
    report_path.write_text(
        render_report(
            observations=observations,
            summary_rows=summary_rows,
            extensions_payload=extensions_payload,
            contract=contract,
        ),
        encoding="utf-8",
    )

    all_input_paths = [
        semantics,
        *evidence_registries,
        *candidate_manifests,
        *candidate_files,
        *escalation_jsonl,
    ]
    provenance = {
        "version": VERSION,
        "target_field": contract.field,
        "field_contract_version": contract.version,
        "scan_publication_fulltext": scan_publication_fulltext,
        "seed_terms": list(seed_terms),
        "candidate_files": len(candidate_files),
        "observation_count": len(observations),
        "accession_count": len({item.accession for item in observations if item.accession}),
        "evidence_backed_extension_count": len(extensions_payload["extensions"]),
        "inputs": input_provenance(all_input_paths),
        "outputs": {
            "observations": str(observations_path),
            "summary": str(summary_path),
            "extensions": str(extensions_path),
            "report": str(report_path),
        },
    }
    write_json(provenance_path, provenance)

    result = {
        **provenance,
        "outputs": {**provenance["outputs"], "provenance": str(provenance_path)},
    }
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--semantics", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--evidence-registry", type=Path, action="append", default=[])
    parser.add_argument("--candidate-root", type=Path, action="append", default=[])
    parser.add_argument("--candidate", type=Path, action="append", default=[])
    parser.add_argument("--candidate-manifest", type=Path, action="append", default=[])
    parser.add_argument("--escalation-jsonl", type=Path, action="append", default=[])
    parser.add_argument("--scan-publication-fulltext", action="store_true")
    parser.add_argument("--seed-term", action="append", default=[])
    parser.add_argument("--window-chars", type=int, default=1000)
    parser.add_argument("--max-hits-per-term", type=int, default=3)
    parser.add_argument("--self-test", action="store_true")
    return parser


def self_test() -> None:
    contract = FieldContract(
        version="fixture",
        field=TARGET_FIELD,
        preferred_values=("FACS", "manual picking"),
        aliases={"fluorescence-activated cell sorting": "FACS"},
        known_extensions=("transvaginal puncture",),
        explicit_exclusions=("cell lysis",),
    )
    assert classify_phrase(
        phrase="FACS", contract=contract, source_kind="candidate_sdrf_value"
    )[1] == "canonical_preferred"
    assert classify_phrase(
        phrase="fluorescence-activated cell sorting",
        contract=contract,
        source_kind="candidate_sdrf_value",
    )[1] == "canonical_alias"
    assert classify_phrase(
        phrase="transvaginal puncture",
        contract=contract,
        source_kind="publication_field_claim",
    )[1] == "evidence_backed_extension"
    assert classify_phrase(
        phrase="transvaginal puncture",
        contract=contract,
        source_kind="candidate_sdrf_value",
    )[1] == "ambiguous"
    print("audit_scp_isolation_vocabulary self-test: PASS")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    if args.semantics is None or args.output_dir is None:
        parser.error("--semantics and --output-dir are required unless --self-test is used")
    result = run_audit(
        semantics=args.semantics,
        output_dir=args.output_dir,
        evidence_registries=tuple(args.evidence_registry),
        candidate_roots=tuple(args.candidate_root),
        candidates=tuple(args.candidate),
        candidate_manifests=tuple(args.candidate_manifest),
        escalation_jsonl=tuple(args.escalation_jsonl),
        scan_publication_fulltext=args.scan_publication_fulltext,
        seed_terms=tuple(args.seed_term),
        window_chars=args.window_chars,
        max_hits_per_term=args.max_hits_per_term,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
