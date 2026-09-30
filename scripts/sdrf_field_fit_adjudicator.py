#!/usr/bin/env python3
"""Bounded local-LLM semantic adjudication for PRIDE-SCP SDRF fields.

The model judges semantic field fit only. Deterministic policy retains authority over
controlled vocabulary, evidence references, and all SDRF edits.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VERSION = "pride-scp-sdrf-field-fit-adjudicator-v0.2.0"
OUTPUT_SCHEMA_VERSION = "pride-scp-sdrf-field-fit-adjudication-v2"
DEFAULT_MODEL = "qwen3.6:27b"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"

ALLOWED_DECISIONS = {
    "fits",
    "does_not_fit",
    "ambiguous",
    "insufficient_evidence",
    "context_limit",
    "conflicting_evidence",
}
ALLOWED_SCOPES = {"all_rows", "subset", "unknown"}


@dataclass(frozen=True)
class FieldContract:
    field: str
    semantic_definition: str
    accepted_values: tuple[str, ...]
    aliases: dict[str, str]
    known_explicit_but_unsupported: tuple[str, ...]
    explicit_exclusions: tuple[str, ...]
    evidence_search_terms: tuple[str, ...]
    resource_version: str
    contract_sha256: str


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def normalize_text(value: str) -> str:
    return " ".join(value.strip().split())


def load_field_contract(resource_path: Path, field: str) -> FieldContract:
    resource = read_json(resource_path)
    fields = resource.get("fields") or {}
    raw = fields.get(field)
    if not isinstance(raw, dict):
        raise KeyError(f"unsupported SDRF field contract: {field}")
    contract_payload = {
        "field": field,
        "resource_version": str(resource.get("version") or ""),
        **raw,
    }
    return FieldContract(
        field=field,
        semantic_definition=str(raw.get("semantic_definition") or ""),
        accepted_values=tuple(str(v) for v in raw.get("accepted_values") or []),
        aliases={str(k): str(v) for k, v in (raw.get("aliases") or {}).items()},
        known_explicit_but_unsupported=tuple(
            str(v) for v in raw.get("known_explicit_but_unsupported") or []
        ),
        explicit_exclusions=tuple(str(v) for v in raw.get("explicit_exclusions") or []),
        evidence_search_terms=tuple(str(v) for v in raw.get("evidence_search_terms") or []),
        resource_version=str(resource.get("version") or ""),
        contract_sha256=sha256_bytes(canonical_json_bytes(contract_payload)),
    )


def canonicalize_value(contract: FieldContract, proposed_value: str) -> tuple[str, str]:
    value = normalize_text(proposed_value)
    if not value:
        return "", "missing"

    accepted_casefold = {item.casefold(): item for item in contract.accepted_values}
    aliases_casefold = {key.casefold(): val for key, val in contract.aliases.items()}
    unsupported_casefold = {
        item.casefold(): item for item in contract.known_explicit_but_unsupported
    }

    if value.casefold() in accepted_casefold:
        return accepted_casefold[value.casefold()], "supported"
    if value.casefold() in aliases_casefold:
        canonical = aliases_casefold[value.casefold()]
        if canonical.casefold() in accepted_casefold:
            return accepted_casefold[canonical.casefold()], "supported"
        return canonical, "unsupported"
    if value.casefold() in unsupported_casefold:
        return unsupported_casefold[value.casefold()], "unsupported"
    return value, "unknown"


def field_fit_response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "decision",
            "proposed_value",
            "application_scope",
            "evidence_refs",
            "rationale",
        ],
        "properties": {
            "decision": {"type": "string", "enum": sorted(ALLOWED_DECISIONS)},
            "proposed_value": {"type": "string"},
            "application_scope": {"type": "string", "enum": sorted(ALLOWED_SCOPES)},
            "evidence_refs": {"type": "array", "items": {"type": "string"}},
            "rationale": {"type": "string"},
        },
    }


def build_prompt(
    *,
    accession: str,
    contract: FieldContract,
    current_values: list[str],
    evidence_items: list[dict[str, Any]],
) -> str:
    evidence_payload = []
    for item in evidence_items:
        evidence_payload.append(
            {
                "evidence_ref": str(item.get("evidence_ref") or ""),
                "kind": str(item.get("kind") or ""),
                "source_identity": str(item.get("source_identity") or ""),
                "parent_artifact_sha256": str(item.get("parent_artifact_sha256") or ""),
                "text": str(item.get("text") or ""),
                "row_scope": str(item.get("row_scope") or "unknown"),
            }
        )

    task = {
        "accession": accession,
        "target_field": contract.field,
        "field_semantics": contract.semantic_definition,
        "accepted_values": list(contract.accepted_values),
        "aliases": contract.aliases,
        "known_explicit_but_unsupported": list(contract.known_explicit_but_unsupported),
        "explicit_exclusions": list(contract.explicit_exclusions),
        "current_distinct_values": current_values,
        "evidence": evidence_payload,
    }
    return (
        "You are a bounded scientific adjudicator for proteomics SDRF metadata.\n"
        "Decide whether the supplied evidence describes the semantic concept represented by the "
        "single target SDRF field.\n"
        "Do not invent facts. Do not use general instrument knowledge to infer a fragmentation "
        "method. Do not confuse biological cell/tissue dissociation with MS/MS ion dissociation.\n"
        "Use only the supplied evidence. If evidence is missing, conflicting, truncated, or too "
        "complex for a reliable judgment, abstain with insufficient_evidence, conflicting_evidence, "
        "ambiguous, or context_limit.\n"
        "If decision=fits, proposed_value must be explicitly supported by the cited evidence. "
        "Do not coerce an unsupported explicit method into a different accepted vocabulary term.\n"
        "application_scope is a semantic judgment only: all_rows means the supplied evidence explicitly "
        "supports one study-wide value; subset means evidence only applies to some rows; unknown means "
        "scope cannot be established. Deterministic policy will independently decide whether any edit "
        "may be applied.\n"
        "Return JSON only, matching the required schema.\n\n"
        + json.dumps(task, ensure_ascii=False, indent=2, sort_keys=True)
    )


def request_ollama(
    *,
    base_url: str,
    model: str,
    prompt: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "stream": False,
        "think": False,
        "format": field_fit_response_schema(),
        "options": {"temperature": 0, "seed": 42},
        "messages": [
            {
                "role": "user",
                "content": prompt,
            }
        ],
    }
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Ollama request failed: {exc}") from exc

    message = body.get("message") or {}
    content = message.get("content")
    if isinstance(content, dict):
        result = content
    else:
        try:
            result = json.loads(str(content or ""))
        except json.JSONDecodeError as exc:
            raise RuntimeError("Ollama returned non-JSON field-fit content") from exc
    if not isinstance(result, dict):
        raise TypeError("Ollama field-fit response is not a JSON object")
    return result


def normalize_model_response(response: dict[str, Any]) -> dict[str, Any]:
    decision = str(response.get("decision") or "").strip()
    if decision not in ALLOWED_DECISIONS:
        decision = "insufficient_evidence"
    application_scope = str(response.get("application_scope") or "unknown").strip()
    if application_scope not in ALLOWED_SCOPES:
        application_scope = "unknown"
    refs_raw = response.get("evidence_refs") or []
    refs = [str(ref).strip() for ref in refs_raw if str(ref).strip()]
    return {
        "decision": decision,
        "proposed_value": normalize_text(str(response.get("proposed_value") or "")),
        "application_scope": application_scope,
        "evidence_refs": sorted(set(refs)),
        "rationale": normalize_text(str(response.get("rationale") or "")),
    }


def apply_deterministic_policy(
    *,
    contract: FieldContract,
    model_response: dict[str, Any],
    allowed_evidence_refs: set[str],
) -> dict[str, Any]:
    normalized = normalize_model_response(model_response)
    cited = set(normalized["evidence_refs"])
    invalid_refs = sorted(cited - allowed_evidence_refs)
    missing_refs = not cited

    if invalid_refs or missing_refs:
        return {
            **normalized,
            "decision": "insufficient_evidence",
            "canonical_value": "",
            "vocabulary_status": "missing",
            "policy_status": "invalid_evidence_reference",
            "requires_human_review": True,
            "invalid_evidence_refs": invalid_refs,
        }

    decision = normalized["decision"]
    proposed_value = normalized["proposed_value"]
    canonical_value, vocabulary_status = canonicalize_value(contract, proposed_value)

    if decision == "fits" and not proposed_value:
        return {
            **normalized,
            "decision": "insufficient_evidence",
            "canonical_value": "",
            "vocabulary_status": "missing",
            "policy_status": "missing_proposed_value",
            "requires_human_review": True,
            "invalid_evidence_refs": [],
        }

    if decision == "fits" and vocabulary_status == "supported":
        policy_status = "supported_field_fit"
        requires_human_review = False
    elif decision == "fits":
        policy_status = "field_fit_requires_vocabulary_review"
        requires_human_review = True
    elif decision == "does_not_fit":
        policy_status = "field_mismatch"
        requires_human_review = False
        canonical_value = ""
        vocabulary_status = "not_applicable"
    elif decision == "ambiguous":
        policy_status = "ambiguous_field_fit"
        requires_human_review = True
        canonical_value = ""
    elif decision == "context_limit":
        policy_status = "context_limit_requires_escalation"
        requires_human_review = True
        canonical_value = ""
    elif decision == "conflicting_evidence":
        policy_status = "conflicting_evidence_requires_escalation"
        requires_human_review = True
        canonical_value = ""
    else:
        policy_status = "insufficient_evidence"
        requires_human_review = True
        canonical_value = ""

    return {
        **normalized,
        "canonical_value": canonical_value,
        "vocabulary_status": vocabulary_status,
        "policy_status": policy_status,
        "requires_human_review": requires_human_review,
        "invalid_evidence_refs": [],
    }


def adjudicate(
    *,
    accession: str,
    contract: FieldContract,
    current_values: list[str],
    evidence_items: list[dict[str, Any]],
    model: str,
    model_response: dict[str, Any],
) -> dict[str, Any]:
    prompt = build_prompt(
        accession=accession,
        contract=contract,
        current_values=current_values,
        evidence_items=evidence_items,
    )
    allowed_refs = {
        str(item.get("evidence_ref") or "")
        for item in evidence_items
        if str(item.get("evidence_ref") or "")
    }
    policy = apply_deterministic_policy(
        contract=contract,
        model_response=model_response,
        allowed_evidence_refs=allowed_refs,
    )
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "adjudicator_version": VERSION,
        "accession": accession,
        "target_field": contract.field,
        "field_contract_version": contract.resource_version,
        "field_contract_sha256": contract.contract_sha256,
        "prompt_sha256": sha256_text(prompt),
        "model": model,
        "structured_json": True,
        "non_editing": True,
        "think": False,
        **policy,
    }


def claim_to_evidence(claim: dict[str, Any], claim_path: Path) -> dict[str, Any]:
    artifact_sha = sha256_bytes(claim_path.read_bytes())
    source_identity = str(
        claim.get("source_identity")
        or claim.get("canonical_source_identity")
        or ""
    )
    parent_sha = str(
        claim.get("parent_artifact_sha256")
        or claim.get("publication_artifact_sha256")
        or ""
    )
    text = str(
        claim.get("claim_text")
        or claim.get("supporting_text")
        or claim.get("evidence_text")
        or ""
    )
    row_scope = str(claim.get("row_scope") or "unknown")
    return {
        "evidence_ref": f"claim:{artifact_sha}",
        "kind": "publication_field_claim",
        "source_identity": source_identity,
        "parent_artifact_sha256": parent_sha,
        "text": text,
        "row_scope": row_scope,
        "claim_artifact_sha256": artifact_sha,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claim-artifact", type=Path)
    parser.add_argument("--field")
    parser.add_argument(
        "--semantics",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "resources"
        / "sdrf_field_semantics_v1.json",
    )
    parser.add_argument("--accession")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--response-json", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args()


def self_test() -> None:
    contract = FieldContract(
        field="comment[dissociation method]",
        semantic_definition="MS/MS fragmentation method",
        accepted_values=("HCD", "CID"),
        aliases={"higher-energy collisional dissociation": "HCD"},
        known_explicit_but_unsupported=(),
        explicit_exclusions=("biological dissociation",),
        evidence_search_terms=("HCD", "dissociation"),
        resource_version="test-v1",
        contract_sha256="0" * 64,
    )
    evidence = [
        {
            "evidence_ref": "claim:abc",
            "kind": "publication_field_claim",
            "source_identity": "doi:test",
            "parent_artifact_sha256": "1" * 64,
            "text": "Peptides were fragmented by higher-energy collisional dissociation.",
            "row_scope": "all_rows",
        }
    ]
    result = adjudicate(
        accession="PXDTEST",
        contract=contract,
        current_values=[""],
        evidence_items=evidence,
        model="fixture",
        model_response={
            "decision": "fits",
            "proposed_value": "higher-energy collisional dissociation",
            "application_scope": "all_rows",
            "evidence_refs": ["claim:abc"],
            "rationale": "Explicit MS/MS fragmentation method.",
        },
    )
    assert result["canonical_value"] == "HCD"
    assert result["policy_status"] == "supported_field_fit"
    print("sdrf_field_fit_adjudicator self-test: PASS")


def main() -> None:
    args = parse_args()
    if args.self_test:
        self_test()
        return
    if not args.claim_artifact or not args.field or not args.output:
        raise SystemExit("--claim-artifact, --field and --output are required")

    claim = read_json(args.claim_artifact)
    accession = args.accession or str(claim.get("accession") or "")
    if not accession:
        raise SystemExit("accession missing from claim and --accession not supplied")
    contract = load_field_contract(args.semantics, args.field)
    evidence = [claim_to_evidence(claim, args.claim_artifact)]
    prompt = build_prompt(
        accession=accession,
        contract=contract,
        current_values=[],
        evidence_items=evidence,
    )
    if args.response_json:
        model_response = read_json(args.response_json)
    else:
        model_response = request_ollama(
            base_url=args.ollama_url,
            model=args.model,
            prompt=prompt,
            timeout_seconds=args.timeout_seconds,
        )
    result = adjudicate(
        accession=accession,
        contract=contract,
        current_values=[],
        evidence_items=evidence,
        model=args.model,
        model_response=model_response,
    )
    result["claim_artifact_sha256"] = evidence[0]["claim_artifact_sha256"]
    result["source_identity"] = evidence[0]["source_identity"]
    result["parent_artifact_sha256"] = evidence[0]["parent_artifact_sha256"]
    write_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
