from __future__ import annotations

import csv
import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "audit_scp_isolation_vocabulary.py"
SPEC = importlib.util.spec_from_file_location("audit_scp_isolation_vocabulary", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)


def write_tsv(path: Path, headers: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def semantics(tmp_path: Path) -> Path:
    path = tmp_path / "semantics.json"
    path.write_text(
        json.dumps(
            {
                "version": "fixture-v1",
                "fields": {
                    mod.TARGET_FIELD: {
                        "accepted_values": ["FACS", "manual picking"],
                        "aliases": {
                            "fluorescence-activated cell sorting": "FACS",
                        },
                        "known_explicit_but_unsupported": [
                            "transvaginal puncture",
                            "capillary microsampling",
                        ],
                        "explicit_exclusions": ["cell lysis"],
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_classifies_preferred_and_alias(tmp_path: Path) -> None:
    contract = mod.load_contract(semantics(tmp_path))
    preferred = mod.classify_phrase(
        phrase="FACS", contract=contract, source_kind="candidate_sdrf_value"
    )
    alias = mod.classify_phrase(
        phrase="fluorescence-activated cell sorting",
        contract=contract,
        source_kind="candidate_sdrf_value",
    )
    assert preferred[0:2] == ("FACS", "canonical_preferred")
    assert alias[0:2] == ("FACS", "canonical_alias")


def test_publication_claim_is_evidence_backed_extension(tmp_path: Path) -> None:
    contract = mod.load_contract(semantics(tmp_path))
    result = mod.classify_phrase(
        phrase="transvaginal puncture",
        contract=contract,
        source_kind="publication_field_claim",
        claim_status="unsupported_vocabulary",
    )
    assert result == (
        "transvaginal puncture",
        "evidence_backed_extension",
        True,
        "known_noncanonical_extension",
    )


def test_candidate_only_noncanonical_value_stays_ambiguous(tmp_path: Path) -> None:
    contract = mod.load_contract(semantics(tmp_path))
    result = mod.classify_phrase(
        phrase="transvaginal puncture",
        contract=contract,
        source_kind="candidate_sdrf_value",
    )
    assert result[1] == "ambiguous"
    assert result[2] is False


def test_semantic_mismatch_claim_stays_mismatch(tmp_path: Path) -> None:
    contract = mod.load_contract(semantics(tmp_path))
    result = mod.classify_phrase(
        phrase="cell lysis",
        contract=contract,
        source_kind="publication_field_claim",
        claim_status="semantic_mismatch",
    )
    assert result[1] == "semantic_mismatch"


def test_candidate_row_scope_and_ontology_wrapper(tmp_path: Path) -> None:
    contract = mod.load_contract(semantics(tmp_path))
    candidate = tmp_path / "PXD000001" / "PXD000001.sdrf.tsv"
    write_tsv(
        candidate,
        ["source name", mod.TARGET_FIELD],
        [
            {"source name": "a", mod.TARGET_FIELD: "NT=FACS;AC=PRIDE:1"},
            {"source name": "b", mod.TARGET_FIELD: "NT=FACS;AC=PRIDE:1"},
        ],
    )
    observations = mod.observations_from_candidate(candidate, contract)
    assert len(observations) == 1
    assert observations[0].mapping_class == "canonical_preferred"
    assert observations[0].normalized_phrase == "FACS"
    assert observations[0].row_scope == "all_rows"
    assert observations[0].candidate_row_count == 2


def test_candidate_placeholder_is_ambiguous(tmp_path: Path) -> None:
    contract = mod.load_contract(semantics(tmp_path))
    candidate = tmp_path / "PXD000002.sdrf.tsv"
    write_tsv(
        candidate,
        [mod.TARGET_FIELD],
        [{mod.TARGET_FIELD: "not applicable"}],
    )
    observations = mod.observations_from_candidate(candidate, contract)
    assert observations[0].mapping_class == "ambiguous"
    assert observations[0].review_note == "non_substantive_placeholder"


def test_registry_claim_observation(tmp_path: Path) -> None:
    contract = mod.load_contract(semantics(tmp_path))
    registry = tmp_path / "registry.tsv"
    headers = [
        "accession",
        "source_kind",
        "blocker_field",
        "artifact_sha256",
        "source_identity",
        "parent_artifact_sha256",
        "claim_value",
        "claim_status",
        "claim_text",
        "row_scope",
    ]
    write_tsv(
        registry,
        headers,
        [
            {
                "accession": "PXD023366",
                "source_kind": "publication_field_claim",
                "blocker_field": mod.TARGET_FIELD,
                "artifact_sha256": "a" * 64,
                "source_identity": "doi:example",
                "parent_artifact_sha256": "b" * 64,
                "claim_value": "transvaginal puncture",
                "claim_status": "unsupported_vocabulary",
                "claim_text": "The oocytes were obtained by transvaginal puncture.",
                "row_scope": "unknown",
            }
        ],
    )
    observations = mod.observations_from_registry(registry, contract)
    assert len(observations) == 1
    item = observations[0]
    assert item.mapping_class == "evidence_backed_extension"
    assert item.evidence_ref == f"claim:{'a' * 64}"
    assert item.evidence_backed is True


def test_escalation_claim_observation(tmp_path: Path) -> None:
    contract = mod.load_contract(semantics(tmp_path))
    packet = tmp_path / "packet.jsonl"
    packet.write_text(
        json.dumps(
            {
                "accession": "PXD023366",
                "field": mod.TARGET_FIELD,
                "evidence": [
                    {
                        "kind": "publication_field_claim",
                        "evidence_ref": "claim:abc",
                        "claim_value": "transvaginal puncture",
                        "claim_status": "unsupported_vocabulary",
                        "source_identity": "doi:example",
                        "text": "oocytes were obtained by transvaginal puncture",
                        "row_scope": "unknown",
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    observations = mod.observations_from_escalation(packet, contract)
    assert len(observations) == 1
    assert observations[0].source_kind == "escalation_publication_field_claim"
    assert observations[0].mapping_class == "evidence_backed_extension"


def test_fulltext_hit_is_discovery_only(tmp_path: Path) -> None:
    contract = mod.load_contract(semantics(tmp_path))
    article = tmp_path / "article.txt"
    article.write_text(
        "Methods. The oocytes were obtained by transvaginal puncture before processing.",
        encoding="utf-8",
    )
    registry = tmp_path / "registry.tsv"
    write_tsv(
        registry,
        ["accession", "source_kind", "local_path", "artifact_sha256", "source_identity"],
        [
            {
                "accession": "PXD023366",
                "source_kind": "publication_fulltext",
                "local_path": str(article),
                "artifact_sha256": "c" * 64,
                "source_identity": "doi:example",
            }
        ],
    )
    observations = mod.fulltext_hit_observations(
        registry=registry,
        contract=contract,
        seed_terms=(),
        window_chars=200,
        max_hits_per_term=2,
    )
    assert len(observations) == 1
    assert observations[0].mapping_class == "ambiguous"
    assert observations[0].evidence_backed is False
    assert observations[0].review_note == "fulltext_hit_requires_claim_or_review"


def test_short_term_search_uses_token_boundaries() -> None:
    assert mod.exact_term_positions("acid CID cidic CID", "CID") == [5, 15]


def test_summary_counts_unique_accessions(tmp_path: Path) -> None:
    contract = mod.load_contract(semantics(tmp_path))
    observations = []
    for accession in ("PXD000001", "PXD000002"):
        normalized, mapping, backed, note = mod.classify_phrase(
            phrase="transvaginal puncture",
            contract=contract,
            source_kind="publication_field_claim",
        )
        observations.append(
            mod.Observation(
                schema_version=mod.SCHEMA_VERSION,
                accession=accession,
                exact_source_phrase="transvaginal puncture",
                normalized_phrase=normalized,
                mapping_class=mapping,
                source_kind="publication_field_claim",
                evidence_backed=backed,
                source_identity="doi:x",
                evidence_ref=f"claim:{accession}",
                artifact_sha256="",
                row_scope="unknown",
                current_sdrf_value="",
                value_row_count=0,
                candidate_row_count=0,
                claim_status="",
                representative_evidence_text="text",
                source_path="registry.tsv",
                review_note=note,
            )
        )
    summary = mod.summarize(observations)
    assert len(summary) == 1
    assert summary[0]["accession_count"] == 2
    assert summary[0]["observation_count"] == 2


def test_run_audit_writes_expected_outputs(tmp_path: Path) -> None:
    sem = semantics(tmp_path)
    registry = tmp_path / "registry.tsv"
    write_tsv(
        registry,
        [
            "accession",
            "source_kind",
            "blocker_field",
            "artifact_sha256",
            "claim_value",
            "claim_status",
            "claim_text",
            "row_scope",
        ],
        [
            {
                "accession": "PXD023366",
                "source_kind": "publication_field_claim",
                "blocker_field": mod.TARGET_FIELD,
                "artifact_sha256": "d" * 64,
                "claim_value": "transvaginal puncture",
                "claim_status": "unsupported_vocabulary",
                "claim_text": "The oocytes were obtained by transvaginal puncture.",
                "row_scope": "unknown",
            }
        ],
    )
    candidate = tmp_path / "candidates" / "PXD023366" / "PXD023366.sdrf.tsv"
    write_tsv(
        candidate,
        ["source name", mod.TARGET_FIELD],
        [
            {"source name": "oocyte-1", mod.TARGET_FIELD: "not applicable"},
            {"source name": "oocyte-2", mod.TARGET_FIELD: "not applicable"},
        ],
    )
    out = tmp_path / "out"
    result = mod.run_audit(
        semantics=sem,
        output_dir=out,
        evidence_registries=(registry,),
        candidate_roots=(candidate.parent.parent,),
        candidates=(),
        candidate_manifests=(),
        escalation_jsonl=(),
        scan_publication_fulltext=False,
        seed_terms=(),
        window_chars=1000,
        max_hits_per_term=3,
    )
    assert result["evidence_backed_extension_count"] == 1
    assert (out / "isolation_vocabulary_observations.tsv").is_file()
    assert (out / "isolation_vocabulary_summary.tsv").is_file()
    assert (out / "isolation_vocabulary_extensions.json").is_file()
    assert (out / "isolation_vocabulary_report.md").is_file()
    assert (out / "provenance.json").is_file()
    payload = json.loads((out / "isolation_vocabulary_extensions.json").read_text())
    assert payload["extensions"][0]["normalized_phrase"] == "transvaginal puncture"
    report = (out / "isolation_vocabulary_report.md").read_text()
    assert "transvaginal puncture" in report


def test_candidate_manifest_supplies_accession_for_generic_filename(tmp_path: Path) -> None:
    contract = mod.load_contract(semantics(tmp_path))
    candidate = tmp_path / "generic" / "confident_candidate.sdrf.tsv"
    write_tsv(
        candidate,
        [mod.TARGET_FIELD],
        [{mod.TARGET_FIELD: "FACS"}],
    )
    manifest = tmp_path / "candidate_manifest.tsv"
    write_tsv(
        manifest,
        ["accession", "candidate_sdrf"],
        [{"accession": "PXD023366", "candidate_sdrf": str(candidate)}],
    )
    entries = mod.candidates_from_manifest(manifest)
    assert entries == [("PXD023366", candidate.resolve())]
    observations = mod.observations_from_candidate(
        entries[0][1], contract, accession_override=entries[0][0]
    )
    assert observations[0].accession == "PXD023366"
    assert observations[0].mapping_class == "canonical_preferred"
