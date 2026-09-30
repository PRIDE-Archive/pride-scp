from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import sdrf_field_fit_adjudicator as mod

SEMANTICS = ROOT / "resources" / "sdrf_field_semantics_v1.json"


def evidence(ref: str, text: str, row_scope: str = "unknown") -> dict[str, str]:
    return {
        "evidence_ref": ref,
        "kind": "fixture",
        "source_identity": "doi:test",
        "parent_artifact_sha256": "1" * 64,
        "text": text,
        "row_scope": row_scope,
    }


def test_transvaginal_puncture_fits_but_requires_vocabulary_review() -> None:
    contract = mod.load_field_contract(
        SEMANTICS,
        "characteristics[single cell isolation protocol]",
    )
    items = [
        evidence(
            "claim:one",
            "The oocytes were obtained by transvaginal puncture with an 18-gauge needle.",
            "all_rows",
        )
    ]
    result = mod.adjudicate(
        accession="PXD023366",
        contract=contract,
        current_values=[],
        evidence_items=items,
        model="fixture",
        model_response={
            "decision": "fits",
            "proposed_value": "transvaginal puncture",
            "application_scope": "all_rows",
            "evidence_refs": ["claim:one"],
            "rationale": "Explicit isolation/collection method.",
        },
    )
    assert result["decision"] == "fits"
    assert result["vocabulary_status"] == "unsupported"
    assert result["canonical_value"] == "transvaginal puncture"
    assert result["policy_status"] == "field_fit_requires_vocabulary_review"


def test_hyaluronidase_does_not_fit_msms_dissociation() -> None:
    contract = mod.load_field_contract(SEMANTICS, "comment[dissociation method]")
    result = mod.adjudicate(
        accession="PXD023366",
        contract=contract,
        current_values=[],
        evidence_items=[
            evidence(
                "publication:one",
                "Hyaluronidase was used to partially remove cumulus cells.",
            )
        ],
        model="fixture",
        model_response={
            "decision": "does_not_fit",
            "proposed_value": "",
            "application_scope": "unknown",
            "evidence_refs": ["publication:one"],
            "rationale": "This is biological cell handling, not MS/MS fragmentation.",
        },
    )
    assert result["policy_status"] == "field_mismatch"
    assert result["canonical_value"] == ""


def test_explicit_hcd_alias_is_supported() -> None:
    contract = mod.load_field_contract(SEMANTICS, "comment[dissociation method]")
    result = mod.adjudicate(
        accession="PXDTEST",
        contract=contract,
        current_values=[],
        evidence_items=[
            evidence(
                "claim:hcd",
                "Peptides were fragmented by higher-energy collisional dissociation.",
                "all_rows",
            )
        ],
        model="fixture",
        model_response={
            "decision": "fits",
            "proposed_value": "higher-energy collisional dissociation",
            "application_scope": "all_rows",
            "evidence_refs": ["claim:hcd"],
            "rationale": "Explicit fragmentation method.",
        },
    )
    assert result["canonical_value"] == "HCD"
    assert result["vocabulary_status"] == "supported"
    assert result["policy_status"] == "supported_field_fit"


def test_invalid_evidence_reference_fails_closed() -> None:
    contract = mod.load_field_contract(SEMANTICS, "comment[dissociation method]")
    result = mod.adjudicate(
        accession="PXDTEST",
        contract=contract,
        current_values=[],
        evidence_items=[evidence("claim:valid", "HCD was used.")],
        model="fixture",
        model_response={
            "decision": "fits",
            "proposed_value": "HCD",
            "application_scope": "all_rows",
            "evidence_refs": ["claim:invented"],
            "rationale": "fixture",
        },
    )
    assert result["decision"] == "insufficient_evidence"
    assert result["policy_status"] == "invalid_evidence_reference"


def test_context_limit_is_explicit_escalation_status() -> None:
    contract = mod.load_field_contract(SEMANTICS, "comment[dissociation method]")
    result = mod.adjudicate(
        accession="PXDTEST",
        contract=contract,
        current_values=[],
        evidence_items=[evidence("publication:long", "Truncated complex methods section.")],
        model="fixture",
        model_response={
            "decision": "context_limit",
            "proposed_value": "",
            "application_scope": "unknown",
            "evidence_refs": ["publication:long"],
            "rationale": "The evidence window is insufficient for a reliable judgment.",
        },
    )
    assert result["policy_status"] == "context_limit_requires_escalation"
    assert result["requires_human_review"] is True


def test_conflicting_evidence_is_explicit_escalation_status() -> None:
    contract = mod.load_field_contract(SEMANTICS, "comment[dissociation method]")
    result = mod.adjudicate(
        accession="PXDTEST",
        contract=contract,
        current_values=[],
        evidence_items=[
            evidence("publication:a", "HCD was used."),
            evidence("publication:b", "CID was used."),
        ],
        model="fixture",
        model_response={
            "decision": "conflicting_evidence",
            "proposed_value": "",
            "application_scope": "unknown",
            "evidence_refs": ["publication:a", "publication:b"],
            "rationale": "Two incompatible methods are explicitly stated.",
        },
    )
    assert result["policy_status"] == "conflicting_evidence_requires_escalation"


def test_claim_to_evidence_binds_exact_artifact_sha(tmp_path: Path) -> None:
    claim = {
        "accession": "PXDTEST",
        "source_identity": "doi:test",
        "parent_artifact_sha256": "2" * 64,
        "claim_text": "Explicit HCD evidence.",
    }
    path = tmp_path / "claim.json"
    path.write_text(json.dumps(claim), encoding="utf-8")
    item = mod.claim_to_evidence(claim, path)
    assert item["evidence_ref"] == f"claim:{mod.sha256_bytes(path.read_bytes())}"
    assert item["source_identity"] == "doi:test"
    assert item["parent_artifact_sha256"] == "2" * 64


def test_prompt_keeps_semantic_fit_independent_from_vocabulary() -> None:
    contract = mod.load_field_contract(
        SEMANTICS,
        "characteristics[single cell isolation protocol]",
    )
    prompt = mod.build_prompt(
        accession="PXD023366",
        contract=contract,
        current_values=["not applicable"],
        evidence_items=[
            evidence(
                "claim:iso",
                "The oocytes were obtained by transvaginal puncture with an 18-gauge needle.",
            )
        ],
    )
    assert "accepted_values" not in prompt
    assert "known_explicit_but_unsupported" not in prompt
    assert "accepted SDRF vocabulary is intentionally withheld" in prompt
    assert "transvaginal puncture" in prompt


def test_current_value_evidence_reference_is_validated() -> None:
    contract = mod.load_field_contract(SEMANTICS, "comment[dissociation method]")
    result = mod.adjudicate(
        accession="PXDTEST",
        contract=contract,
        current_values=["CID"],
        evidence_items=[evidence("claim:hcd", "HCD was used.")],
        model="fixture",
        model_response={
            "decision": "fits",
            "proposed_value": "HCD",
            "application_scope": "all_rows",
            "evidence_refs": ["claim:hcd"],
            "current_value_assessment": "conflicts",
            "current_value_evidence_refs": ["claim:not-present"],
            "rationale": "The supplied evidence supports HCD, not CID.",
        },
    )
    assert result["policy_status"] == "invalid_evidence_reference"
    assert result["invalid_evidence_refs"] == ["claim:not-present"]
