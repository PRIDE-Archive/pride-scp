from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import sdrf_llm_completion as mod

SEMANTICS = ROOT / "resources" / "sdrf_field_semantics_v1.json"


def write_tsv(path: Path, headers: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=headers,
            delimiter="\t",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def fixture_inputs(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    accession = "PXDTEST"
    candidate = tmp_path / "candidate.sdrf.tsv"
    blocker = tmp_path / "blockers.tsv"
    registry = tmp_path / "registry.tsv"
    publication = tmp_path / "paper.txt"
    publication.write_text(
        "Peptides were fragmented by higher-energy collisional dissociation.\n",
        encoding="utf-8",
    )
    pub_sha = mod.sha256_file(publication)

    write_tsv(
        candidate,
        ["source name", "comment[dissociation method]"],
        [
            {"source name": "s1", "comment[dissociation method]": ""},
            {"source name": "s2", "comment[dissociation method]": ""},
        ],
    )
    write_tsv(
        blocker,
        ["accession", "blocker_family", "blocker_fields"],
        [
            {
                "accession": accession,
                "blocker_family": "msms_metadata",
                "blocker_fields": "comment[dissociation method]",
            }
        ],
    )
    write_tsv(
        registry,
        [
            "accession",
            "source_kind",
            "source_identity",
            "artifact_sha256",
            "parent_artifact_sha256",
            "local_path",
            "blocker_field",
            "claim_text",
            "claim_value",
            "claim_status",
            "row_scope",
        ],
        [
            {
                "accession": accession,
                "source_kind": "publication_field_claim",
                "source_identity": "doi:test",
                "artifact_sha256": "a" * 64,
                "parent_artifact_sha256": pub_sha,
                "local_path": "",
                "blocker_field": "comment[dissociation method]",
                "claim_text": (
                    "Peptides were fragmented by higher-energy collisional dissociation."
                ),
                "claim_value": "higher-energy collisional dissociation",
                "claim_status": "supported_vocabulary",
                "row_scope": "all_rows",
            },
            {
                "accession": accession,
                "source_kind": "publication_fulltext",
                "source_identity": "doi:test",
                "artifact_sha256": pub_sha,
                "parent_artifact_sha256": "",
                "local_path": str(publication),
                "blocker_field": "",
                "claim_text": "",
                "claim_value": "",
                "claim_status": "",
                "row_scope": "",
            },
        ],
    )
    return candidate, blocker, registry, accession


def test_supported_all_rows_claim_updates_confident_candidate(tmp_path: Path) -> None:
    candidate, blocker, registry, accession = fixture_inputs(tmp_path)
    output = tmp_path / "out"
    summary = mod.run_completion(
        accession=accession,
        candidate_sdrf=candidate,
        blocker_manifest=blocker,
        evidence_registry=registry,
        semantics=SEMANTICS,
        output=output,
        model="fixture",
        ollama_url="http://unused",
        timeout_seconds=1.0,
        response_fixtures={
            "comment[dissociation method]": {
                "decision": "fits",
                "proposed_value": "higher-energy collisional dissociation",
                "application_scope": "all_rows",
                "evidence_refs": ["claim:" + "a" * 64],
                "rationale": "Explicit study-wide fragmentation method.",
            }
        },
        window_chars=200,
        max_publication_windows=2,
    )
    assert summary["accepted_confident_patches"] == 1
    rows = read_tsv(output / "confident_candidate.sdrf.tsv")
    assert {row["comment[dissociation method]"] for row in rows} == {"HCD"}
    assert summary["escalation_items"] == 0


def test_unsupported_value_goes_to_review_draft_and_escalation(tmp_path: Path) -> None:
    accession = "PXD023366"
    candidate = tmp_path / "candidate.sdrf.tsv"
    blocker = tmp_path / "blockers.tsv"
    registry = tmp_path / "registry.tsv"
    write_tsv(
        candidate,
        ["source name", "characteristics[single cell isolation protocol]"],
        [
            {
                "source name": "s1",
                "characteristics[single cell isolation protocol]": "",
            },
            {
                "source name": "s2",
                "characteristics[single cell isolation protocol]": "",
            },
        ],
    )
    write_tsv(
        blocker,
        ["accession", "blocker_family", "blocker_fields"],
        [
            {
                "accession": accession,
                "blocker_family": "single_cell_isolation",
                "blocker_fields": "characteristics[single cell isolation protocol]",
            }
        ],
    )
    write_tsv(
        registry,
        [
            "accession",
            "source_kind",
            "source_identity",
            "artifact_sha256",
            "parent_artifact_sha256",
            "local_path",
            "blocker_field",
            "claim_text",
            "claim_value",
            "claim_status",
            "row_scope",
        ],
        [
            {
                "accession": accession,
                "source_kind": "publication_field_claim",
                "source_identity": "doi:10.1016/j.mcpro.2022.100267",
                "artifact_sha256": "b" * 64,
                "parent_artifact_sha256": "c" * 64,
                "local_path": "",
                "blocker_field": "characteristics[single cell isolation protocol]",
                "claim_text": "The oocytes were obtained by transvaginal puncture.",
                "claim_value": "transvaginal puncture",
                "claim_status": "unsupported_vocabulary",
                "row_scope": "unknown",
            }
        ],
    )
    output = tmp_path / "out"
    summary = mod.run_completion(
        accession=accession,
        candidate_sdrf=candidate,
        blocker_manifest=blocker,
        evidence_registry=registry,
        semantics=SEMANTICS,
        output=output,
        model="fixture",
        ollama_url="http://unused",
        timeout_seconds=1.0,
        response_fixtures={
            "characteristics[single cell isolation protocol]": {
                "decision": "fits",
                "proposed_value": "transvaginal puncture",
                "application_scope": "all_rows",
                "evidence_refs": ["claim:" + "b" * 64],
                "rationale": "Explicit isolation method, but vocabulary is unsupported.",
            }
        },
        window_chars=200,
        max_publication_windows=2,
    )
    assert summary["accepted_confident_patches"] == 0
    assert summary["local_review_proposals"] == 1
    assert summary["escalation_items"] == 1
    confident = read_tsv(output / "confident_candidate.sdrf.tsv")
    assert {row["characteristics[single cell isolation protocol]"] for row in confident} == {
        ""
    }
    draft = read_tsv(output / "local_best_effort.sdrf.tsv")
    assert {row["characteristics[single cell isolation protocol]"] for row in draft} == {
        "transvaginal puncture"
    }
    packets = [
        json.loads(line)
        for line in (output / "evidence_packet.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert packets[0]["escalation_reason"] == "requires_vocabulary_review"


def test_model_context_limit_is_escalated_without_draft_edit(tmp_path: Path) -> None:
    candidate, blocker, registry, accession = fixture_inputs(tmp_path)
    output = tmp_path / "out"
    summary = mod.run_completion(
        accession=accession,
        candidate_sdrf=candidate,
        blocker_manifest=blocker,
        evidence_registry=registry,
        semantics=SEMANTICS,
        output=output,
        model="fixture",
        ollama_url="http://unused",
        timeout_seconds=1.0,
        response_fixtures={
            "comment[dissociation method]": {
                "decision": "context_limit",
                "proposed_value": "",
                "application_scope": "unknown",
                "evidence_refs": ["claim:" + "a" * 64],
                "rationale": "Need more context.",
            }
        },
        window_chars=200,
        max_publication_windows=2,
    )
    assert summary["context_limit_items"] == 1
    assert summary["escalation_items"] == 1
    assert summary["local_review_proposals"] == 0


def test_model_scope_alone_cannot_authorize_confident_patch(tmp_path: Path) -> None:
    candidate, blocker, registry, accession = fixture_inputs(tmp_path)
    _, rows = mod.read_tsv(registry)
    rows[0]["row_scope"] = "unknown"
    headers, _ = mod.read_tsv(registry)
    mod.write_tsv(registry, headers, rows)
    output = tmp_path / "out"
    summary = mod.run_completion(
        accession=accession,
        candidate_sdrf=candidate,
        blocker_manifest=blocker,
        evidence_registry=registry,
        semantics=SEMANTICS,
        output=output,
        model="fixture",
        ollama_url="http://unused",
        timeout_seconds=1.0,
        response_fixtures={
            "comment[dissociation method]": {
                "decision": "fits",
                "proposed_value": "HCD",
                "application_scope": "all_rows",
                "evidence_refs": ["claim:" + "a" * 64],
                "rationale": "Explicit HCD.",
            }
        },
        window_chars=200,
        max_publication_windows=2,
    )
    assert summary["accepted_confident_patches"] == 0
    assert summary["local_review_proposals"] == 1
    assert summary["escalation_items"] == 1


def test_existing_nonblank_values_are_never_overwritten(tmp_path: Path) -> None:
    candidate, blocker, registry, accession = fixture_inputs(tmp_path)
    headers, rows = mod.read_tsv(candidate)
    rows[0]["comment[dissociation method]"] = "CID"
    mod.write_tsv(candidate, headers, rows)
    output = tmp_path / "out"
    summary = mod.run_completion(
        accession=accession,
        candidate_sdrf=candidate,
        blocker_manifest=blocker,
        evidence_registry=registry,
        semantics=SEMANTICS,
        output=output,
        model="fixture",
        ollama_url="http://unused",
        timeout_seconds=1.0,
        response_fixtures={
            "comment[dissociation method]": {
                "decision": "fits",
                "proposed_value": "HCD",
                "application_scope": "all_rows",
                "evidence_refs": ["claim:" + "a" * 64],
                "rationale": "Explicit HCD.",
            }
        },
        window_chars=200,
        max_publication_windows=2,
    )
    assert summary["accepted_confident_patches"] == 0
    confident = read_tsv(output / "confident_candidate.sdrf.tsv")
    assert confident[0]["comment[dissociation method]"] == "CID"
    assert confident[1]["comment[dissociation method]"] == ""


def test_bundle_contains_compact_external_patch_contract(tmp_path: Path) -> None:
    candidate, blocker, registry, accession = fixture_inputs(tmp_path)
    output = tmp_path / "out"
    mod.run_completion(
        accession=accession,
        candidate_sdrf=candidate,
        blocker_manifest=blocker,
        evidence_registry=registry,
        semantics=SEMANTICS,
        output=output,
        model="fixture",
        ollama_url="http://unused",
        timeout_seconds=1.0,
        response_fixtures={
            "comment[dissociation method]": {
                "decision": "ambiguous",
                "proposed_value": "",
                "application_scope": "unknown",
                "evidence_refs": ["claim:" + "a" * 64],
                "rationale": "Ambiguous.",
            }
        },
        window_chars=200,
        max_publication_windows=2,
    )
    expected = {
        "confident_candidate.sdrf.tsv",
        "local_best_effort.sdrf.tsv",
        "accepted_patch.jsonl",
        "review_overlay.tsv",
        "evidence_packet.jsonl",
        "escalation_patch.schema.json",
        "local_adjudications.jsonl",
        "provenance.json",
        "completion_summary.json",
        "README_REVIEW.txt",
    }
    assert expected <= {path.name for path in output.iterdir()}
    schema = json.loads(
        (output / "escalation_patch.schema.json").read_text(encoding="utf-8")
    )
    assert schema["title"] == mod.PATCH_SCHEMA_VERSION


def test_empty_blocker_field_set_produces_unchanged_bundle(tmp_path: Path) -> None:
    accession = "PXD058457"
    candidate = tmp_path / "candidate.sdrf.tsv"
    blocker = tmp_path / "blockers.tsv"
    registry = tmp_path / "registry.tsv"
    write_tsv(candidate, ["source name"], [{"source name": "s1"}])
    write_tsv(
        blocker,
        ["accession", "blocker_family", "blocker_fields"],
        [
            {
                "accession": accession,
                "blocker_family": "row_mapping",
                "blocker_fields": "",
            }
        ],
    )
    write_tsv(registry, ["accession", "source_kind"], [])
    output = tmp_path / "out"
    summary = mod.run_completion(
        accession=accession,
        candidate_sdrf=candidate,
        blocker_manifest=blocker,
        evidence_registry=registry,
        semantics=SEMANTICS,
        output=output,
        model="fixture",
        ollama_url="http://unused",
        timeout_seconds=1.0,
        response_fixtures={},
        window_chars=200,
        max_publication_windows=2,
    )
    assert summary["local_fields_attempted"] == 0
    assert summary["accepted_confident_patches"] == 0
    assert summary["escalation_items"] == 0
    assert (output / "confident_candidate.sdrf.tsv").read_bytes() == candidate.read_bytes()
