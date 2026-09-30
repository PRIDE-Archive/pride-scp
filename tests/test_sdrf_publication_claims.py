from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from sdrf_annotation_harness import extract_publication_claims_and_replan
from sdrf_annotation_state import sha256_file
from sdrf_evidence_registry import build_registry
from sdrf_publication_claims import (
    DISSOCIATION_FIELD,
    ISOLATION_FIELD,
    build_publication_claim_rows,
)


def write_tsv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=fields,
            delimiter="\t",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def publication_registry(
    root: Path,
    *,
    accession: str,
    text: str,
    identity: str = "doi:10.1016/j.mcpro.2022.100267",
) -> tuple[Path, Path, str]:
    paper = root / "paper.txt"
    paper.write_text(text, encoding="utf-8")
    sha = sha256_file(paper)
    registry = root / "evidence.tsv"
    write_tsv(
        registry,
        [
            {
                "accession": accession,
                "artifact_sha256": sha,
                "sha_verified": "true",
                "blocker_field": "",
                "source_kind": "publication_fulltext",
                "source_provider": "europe_pmc_fullTextXML",
                "source_locator": "https://doi.org/10.1016/j.mcpro.2022.100267",
                "source_identity": identity,
                "publication_doi": identity.removeprefix("doi:"),
                "publication_identity_status": "external_recovery:accepted",
                "local_path": str(paper),
                "trust_class": "trusted_independent",
                "independence_class": "independent_external",
                "is_independent": "true",
                "provenance_status": "independent_source_provenance_present",
            }
        ],
        [
            "accession",
            "artifact_sha256",
            "sha_verified",
            "blocker_field",
            "source_kind",
            "source_provider",
            "source_locator",
            "source_identity",
            "publication_doi",
            "publication_identity_status",
            "local_path",
            "trust_class",
            "independence_class",
            "is_independent",
            "provenance_status",
        ],
    )
    return registry, paper, sha


def blocker_manifest(root: Path, accession: str, fields: list[str]) -> Path:
    path = root / "blockers.tsv"
    write_tsv(
        path,
        [
            {
                "accession": accession,
                "state": "partial_human_review",
                "reason_code": "single_cell_isolation_unresolved",
                "blocker_fields": ";".join(fields),
            }
        ],
        ["accession", "state", "reason_code", "blocker_fields"],
    )
    return path


def test_publication_claim_extractor_emits_exact_unsupported_isolation_claim(
    tmp_path: Path,
) -> None:
    registry, _, parent_sha = publication_registry(
        tmp_path,
        accession="PXD023366",
        text=(
            "The oocytes were obtained by transvaginal puncture with an 18-gauge needle. "
            "Peptide analysis was performed on a Q Exactive HF-X mass spectrometer."
        ),
    )
    blockers = blocker_manifest(
        tmp_path,
        "PXD023366",
        [ISOLATION_FIELD, DISSOCIATION_FIELD],
    )
    rows, audit, summary = build_publication_claim_rows(
        registry,
        blockers,
        tmp_path / "claims",
    )

    assert len(rows) == 1
    assert len(audit) == 1
    row = rows[0]
    assert row["blocker_field"] == ISOLATION_FIELD
    assert row["claim_value"] == "transvaginal puncture"
    assert row["claim_status"] == "unsupported_vocabulary"
    assert row["parent_artifact_sha256"] == parent_sha
    assert row["trust_class"] == "derived_publication_claim"
    assert Path(row["local_path"]).is_file()
    claim = json.loads(Path(row["local_path"]).read_text(encoding="utf-8"))
    assert claim["parent_artifact_sha256"] == parent_sha
    assert claim["blocker_field"] == ISOLATION_FIELD
    assert claim["claim_value"] == "transvaginal puncture"
    assert "transvaginal puncture" in claim["supporting_spans"][0]["matched_text"].lower()
    assert summary["claims_emitted"] == 1
    assert summary["unsupported_vocabulary_claims"] == 1
    assert summary["no_claim_fields"] == 1


def test_publication_claim_extractor_never_infers_hcd_from_q_exactive(tmp_path: Path) -> None:
    registry, _, _ = publication_registry(
        tmp_path,
        accession="PXD023366",
        text=(
            "Peptide analysis was performed on a Q Exactive HF-X Hybrid "
            "Quadrupole-Orbitrap mass spectrometer in data-dependent acquisition mode."
        ),
    )
    blockers = blocker_manifest(tmp_path, "PXD023366", [DISSOCIATION_FIELD])
    rows, audit, summary = build_publication_claim_rows(
        registry,
        blockers,
        tmp_path / "claims",
    )
    assert rows == []
    assert audit == []
    assert summary["claims_emitted"] == 0
    assert summary["no_claim_fields"] == 1


def test_publication_claim_extractor_accepts_explicit_hcd(tmp_path: Path) -> None:
    registry, _, _ = publication_registry(
        tmp_path,
        accession="PXD023366",
        text="MS/MS spectra were acquired using higher-energy collisional dissociation (HCD).",
    )
    blockers = blocker_manifest(tmp_path, "PXD023366", [DISSOCIATION_FIELD])
    rows, _, summary = build_publication_claim_rows(
        registry,
        blockers,
        tmp_path / "claims",
    )
    assert len(rows) == 1
    assert rows[0]["blocker_field"] == DISSOCIATION_FIELD
    assert rows[0]["claim_value"] == "HCD"
    assert rows[0]["claim_status"] == "supported_vocabulary"
    assert summary["supported_vocabulary_claims"] == 1


def test_publication_claim_extractor_fails_closed_on_conflicting_values(tmp_path: Path) -> None:
    registry, _, _ = publication_registry(
        tmp_path,
        accession="PXD023366",
        text=(
            "Some single cells were isolated by manual picking. "
            "Other cells were isolated by fluorescence-activated cell sorting (FACS)."
        ),
    )
    blockers = blocker_manifest(tmp_path, "PXD023366", [ISOLATION_FIELD])
    rows, audit, summary = build_publication_claim_rows(
        registry,
        blockers,
        tmp_path / "claims",
    )
    assert rows == []
    assert len(audit) == 2
    assert {row["claim_status"] for row in audit} == {"conflict_unresolved"}
    assert {row["accepted"] for row in audit} == {"false"}
    assert summary["conflicting_fields"] == 1


def test_publication_claim_extractor_skips_empty_blocker_fields(tmp_path: Path) -> None:
    registry, _, _ = publication_registry(
        tmp_path,
        accession="PXD058457",
        text="Cells were isolated by FACS before single-cell proteomic analysis.",
        identity="doi:10.1016/j.mcpro.2025.101018",
    )
    blockers = blocker_manifest(tmp_path, "PXD058457", [])
    rows, audit, summary = build_publication_claim_rows(
        registry,
        blockers,
        tmp_path / "claims",
    )
    assert rows == []
    assert audit == []
    assert summary["skipped_empty_blocker_fields"] == 1


def test_publication_claim_registry_preserves_parent_independence(tmp_path: Path) -> None:
    registry, _, parent_sha = publication_registry(
        tmp_path,
        accession="PXD023366",
        text="The oocytes were obtained by transvaginal puncture with an 18-gauge needle.",
    )
    blockers = blocker_manifest(tmp_path, "PXD023366", [ISOLATION_FIELD])
    rows, _, _ = build_publication_claim_rows(
        registry,
        blockers,
        tmp_path / "claims",
    )
    claim_manifest = tmp_path / "claim_manifest.tsv"
    write_tsv(claim_manifest, rows, list(rows[0]))

    merged, _ = build_registry([registry, claim_manifest], None)
    claim_row = next(row for row in merged if row["source_kind"] == "publication_field_claim")
    assert claim_row["parent_artifact_sha256"] == parent_sha
    assert claim_row["independence_class"] == "derived_from_trusted_source"
    assert claim_row["is_independent"] == "true"
    assert claim_row["blocker_field"] == ISOLATION_FIELD
    assert claim_row["claim_value"] == "transvaginal puncture"
    assert claim_row["claim_status"] == "unsupported_vocabulary"


def test_harness_extracts_claim_and_replans_without_false_closure(tmp_path: Path) -> None:
    registry, _, _ = publication_registry(
        tmp_path,
        accession="PXD023366",
        text=(
            "The oocytes were obtained by transvaginal puncture with an 18-gauge needle. "
            "Peptide analysis was performed on a Q Exactive HF-X mass spectrometer."
        ),
    )
    blockers = blocker_manifest(
        tmp_path,
        "PXD023366",
        [ISOLATION_FIELD, DISSOCIATION_FIELD],
    )
    accessions = tmp_path / "accessions.txt"
    accessions.write_text("PXD023366\n", encoding="utf-8")
    candidates = tmp_path / "candidates.tsv"
    write_tsv(
        candidates,
        [{"accession": "PXD023366", "candidate_sha256": "1" * 64}],
        ["accession", "candidate_sha256"],
    )
    spec = {
        "schema_version": "pride-scp-sdrf-annotation-run-spec-v1",
        "run_id": "publication-claim-test",
        "provenance": {"policy_version": "p"},
        "inputs": {
            "accessions_file": str(accessions),
            "candidate_manifest": str(candidates),
            "blocker_manifest": str(blockers),
            "evidence_registry": str(registry),
        },
    }
    spec_path = tmp_path / "run_spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    summary = extract_publication_claims_and_replan(spec_path, tmp_path / "out")
    assert summary["publication_claim_extraction"]["claims_emitted"] == 1
    state = next(
        csv.DictReader((tmp_path / "out" / "state_ledger.tsv").open(), delimiter="\t")
    )
    assert state["independent_evidence_count"] == "1"
    assert state["independent_evidence_fields"] == ISOLATION_FIELD
    assert state["applicable_resolvers"] == ""
    assert state["terminal_state"] == "HUMAN_REVIEW_ACTIONABLE"
    assert (
        state["decision_reason"]
        == "independent_blocker_evidence_present_but_no_registered_resolver"
    )

    merged = list(csv.DictReader(registry.open(), delimiter="\t"))
    claim = next(row for row in merged if row["source_kind"] == "publication_field_claim")
    assert claim["is_independent"] == "true"
    assert claim["independence_class"] == "derived_from_trusted_source"
    assert claim["blocker_field"] == ISOLATION_FIELD
    assert claim["claim_value"] == "transvaginal puncture"


def test_specific_hcd_phrase_does_not_double_count_as_cid(tmp_path: Path) -> None:
    registry, _, _ = publication_registry(
        tmp_path,
        accession="PXD023366",
        text="MS/MS used beam-type collision-induced dissociation for fragmentation.",
    )
    blockers = blocker_manifest(tmp_path, "PXD023366", [DISSOCIATION_FIELD])
    rows, audit, summary = build_publication_claim_rows(
        registry,
        blockers,
        tmp_path / "claims",
    )
    assert len(rows) == 1
    assert rows[0]["claim_value"] == "HCD"
    assert rows[0]["claim_status"] == "supported_vocabulary"
    assert len(audit) == 1
    assert summary["conflicting_fields"] == 0


def test_specific_droplet_microfluidics_does_not_conflict_with_generic_rule(
    tmp_path: Path,
) -> None:
    registry, _, _ = publication_registry(
        tmp_path,
        accession="PXD023366",
        text="Single cells were isolated using droplet microfluidics before processing.",
    )
    blockers = blocker_manifest(tmp_path, "PXD023366", [ISOLATION_FIELD])
    rows, audit, summary = build_publication_claim_rows(
        registry,
        blockers,
        tmp_path / "claims",
    )
    assert len(rows) == 1
    assert rows[0]["claim_value"] == "droplet microfluidics"
    assert len(audit) == 1
    assert summary["conflicting_fields"] == 0


def test_harness_claim_extraction_is_idempotent(tmp_path: Path) -> None:
    registry, _, _ = publication_registry(
        tmp_path,
        accession="PXD023366",
        text="The oocytes were obtained by transvaginal puncture with an 18-gauge needle.",
    )
    blockers = blocker_manifest(tmp_path, "PXD023366", [ISOLATION_FIELD])
    accessions = tmp_path / "accessions.txt"
    accessions.write_text("PXD023366\n", encoding="utf-8")
    candidates = tmp_path / "candidates.tsv"
    write_tsv(
        candidates,
        [{"accession": "PXD023366", "candidate_sha256": "1" * 64}],
        ["accession", "candidate_sha256"],
    )
    spec = {
        "schema_version": "pride-scp-sdrf-annotation-run-spec-v1",
        "run_id": "publication-claim-idempotency",
        "provenance": {"policy_version": "p"},
        "inputs": {
            "accessions_file": str(accessions),
            "candidate_manifest": str(candidates),
            "blocker_manifest": str(blockers),
            "evidence_registry": str(registry),
        },
    }
    spec_path = tmp_path / "run_spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    first = extract_publication_claims_and_replan(spec_path, tmp_path / "out1")
    first_rows = list(csv.DictReader(registry.open(), delimiter="\t"))
    first_claim = next(row for row in first_rows if row["source_kind"] == "publication_field_claim")
    first_sha = first_claim["artifact_sha256"]
    first_path = first_claim["local_path"]

    second = extract_publication_claims_and_replan(spec_path, tmp_path / "out2")
    second_rows = list(csv.DictReader(registry.open(), delimiter="\t"))
    second_claims = [row for row in second_rows if row["source_kind"] == "publication_field_claim"]

    assert len(first_rows) == 2
    assert len(second_rows) == 2
    assert len(second_claims) == 1
    assert second_claims[0]["artifact_sha256"] == first_sha
    assert second_claims[0]["local_path"] == first_path
    assert first["publication_claim_extraction"]["claims_emitted"] == 1
    assert second["publication_claim_extraction"]["claims_emitted"] == 1
