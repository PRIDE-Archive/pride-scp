from __future__ import annotations
import json

import csv
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from sdrf_annotation_state import (  # noqa: E402
    AttemptRecord,
    EvidenceRecord,
    ResolverCapability,
    apply_generic_implementation_gate,
    blocker_key,
    decide_case,
    evidence_set_sha256,
    stage_key,
)


def evidence(acc: str, field: str, sha: str = "a" * 64) -> EvidenceRecord:
    return EvidenceRecord(
        accession=acc,
        artifact_sha256=sha,
        blocker_field=field,
        trust_class="trusted_independent",
        provenance_status="independent_source_provenance_present",
        is_independent=True,
    )


def test_stage_key_changes_with_candidate_and_evidence() -> None:
    bkey = blocker_key("cell_identifier", ["characteristics[cell identifier]"])
    base = dict(
        accession="PXD900001",
        resolver_id="r",
        resolver_version="1",
        policy_version="p",
        blocker_key_value=bkey,
    )
    k1 = stage_key(candidate_sha256="1" * 64, evidence_set_sha256_value="a" * 64, **base)
    k2 = stage_key(candidate_sha256="2" * 64, evidence_set_sha256_value="a" * 64, **base)
    k3 = stage_key(candidate_sha256="1" * 64, evidence_set_sha256_value="b" * 64, **base)
    assert len({k1, k2, k3}) == 3


def test_supported_independent_evidence_runs_resolver() -> None:
    field = "characteristics[cell identifier]"
    cap = ResolverCapability(
        resolver_id="r",
        version="1",
        supported_fields=(field,),
        accepted_trust_classes=("trusted_independent",),
    )
    d = decide_case(
        accession="PXD900001",
        input_state="blocked_metadata_incomplete",
        reason_code="cell_identifier_invalid_or_unresolved",
        blocker_fields=[field],
        candidate_sha256="1" * 64,
        evidence=[evidence("PXD900001", field)],
        resolvers=[cap],
        attempts=[],
        policy_version="p",
    )
    assert d.next_action == "RUN_RESOLVER"
    assert d.terminal_state == ""


def test_exact_no_progress_cache_prevents_rerun() -> None:
    acc = "PXD900001"
    field = "characteristics[cell identifier]"
    ev = evidence(acc, field)
    cap = ResolverCapability(
        resolver_id="r",
        version="1",
        supported_fields=(field,),
        accepted_trust_classes=("trusted_independent",),
    )
    esha = evidence_set_sha256([ev])
    bkey = blocker_key("cell_identifier", [field])
    key = stage_key(
        accession=acc,
        candidate_sha256="1" * 64,
        evidence_set_sha256_value=esha,
        resolver_id="r",
        resolver_version="1",
        policy_version="p",
        blocker_key_value=bkey,
    )
    attempt = AttemptRecord(
        stage_key=key,
        accession=acc,
        resolver_id="r",
        status="no_change",
        candidate_sha256="1" * 64,
        evidence_set_sha256=esha,
        policy_version="p",
        blocker_key=bkey,
    )
    d = decide_case(
        accession=acc,
        input_state="blocked_metadata_incomplete",
        reason_code="cell_identifier_invalid_or_unresolved",
        blocker_fields=[field],
        candidate_sha256="1" * 64,
        evidence=[ev],
        resolvers=[cap],
        attempts=[attempt],
        policy_version="p",
    )
    assert d.terminal_state == "EVIDENCE_LIMITED"
    assert d.cached_no_progress_resolvers == ["r"]


def test_candidate_change_invalidates_no_progress_cache() -> None:
    acc = "PXD900001"
    field = "characteristics[cell identifier]"
    ev = evidence(acc, field)
    cap = ResolverCapability(
        resolver_id="r",
        version="1",
        supported_fields=(field,),
        accepted_trust_classes=("trusted_independent",),
    )
    esha = evidence_set_sha256([ev])
    bkey = blocker_key("cell_identifier", [field])
    old_key = stage_key(
        accession=acc,
        candidate_sha256="1" * 64,
        evidence_set_sha256_value=esha,
        resolver_id="r",
        resolver_version="1",
        policy_version="p",
        blocker_key_value=bkey,
    )
    old = AttemptRecord(old_key, acc, "r", "no_change", "1" * 64, esha, "p", bkey)
    d = decide_case(
        accession=acc,
        input_state="blocked_metadata_incomplete",
        reason_code="cell_identifier_invalid_or_unresolved",
        blocker_fields=[field],
        candidate_sha256="2" * 64,
        evidence=[ev],
        resolvers=[cap],
        attempts=[old],
        policy_version="p",
    )
    assert d.next_action == "RUN_RESOLVER"


def test_publication_or_untrusted_context_does_not_authorize_projection() -> None:
    field = "characteristics[single cell isolation protocol]"
    ev = EvidenceRecord(
        accession="PXD900001",
        artifact_sha256="a" * 64,
        blocker_field=field,
        trust_class="publication_context",
        provenance_status="review_only",
        is_independent=False,
    )
    d = decide_case(
        accession="PXD900001",
        input_state="blocked_metadata_incomplete",
        reason_code="single_cell_isolation_unresolved",
        blocker_fields=[field],
        candidate_sha256="1" * 64,
        evidence=[ev],
        resolvers=[],
        attempts=[],
        policy_version="p",
    )
    assert d.terminal_state == "EVIDENCE_LIMITED"


def test_three_unsupported_but_independently_evidenced_cases_trigger_generic_gate() -> None:
    field = "characteristics[single cell isolation protocol]"
    decisions = []
    for i in range(3):
        acc = f"PXD90000{i + 1}"
        decisions.append(
            decide_case(
                accession=acc,
                input_state="blocked_metadata_incomplete",
                reason_code="single_cell_isolation_unresolved",
                blocker_fields=[field],
                candidate_sha256=str(i + 1) * 64,
                evidence=[evidence(acc, field, chr(ord('a') + i) * 64)],
                resolvers=[],
                attempts=[],
                policy_version="p",
            )
        )
    promoted = apply_generic_implementation_gate(decisions, threshold=3)
    assert promoted[field] == ["PXD900001", "PXD900002", "PXD900003"]
    assert {d.terminal_state for d in decisions} == {"IMPLEMENTATION_CANDIDATE"}


def test_provenance_conflict_is_terminal() -> None:
    d = decide_case(
        accession="PXD900001",
        input_state="provenance_conflict",
        reason_code="provenance_conflict",
        blocker_fields=[],
        candidate_sha256="1" * 64,
        evidence=[],
        resolvers=[],
        attempts=[],
        policy_version="p",
    )
    assert d.terminal_state == "PROVENANCE_CONFLICT"


def test_generic_gate_uses_only_independently_evidenced_blocker_field() -> None:
    isolation = "characteristics[single cell isolation protocol]"
    cell_id = "characteristics[cell identifier]"
    decisions = []
    for i in range(3):
        acc = f"PXD91000{i + 1}"
        decisions.append(
            decide_case(
                accession=acc,
                input_state="blocked_metadata_incomplete",
                reason_code="multiple blockers",
                blocker_fields=[isolation, cell_id],
                candidate_sha256=str(i + 1) * 64,
                evidence=[evidence(acc, cell_id, chr(ord('e') + i) * 64)],
                resolvers=[],
                attempts=[],
                policy_version="p",
            )
        )
    promoted = apply_generic_implementation_gate(decisions, threshold=3)
    assert cell_id in promoted
    assert isolation not in promoted


def test_unscoped_independent_evidence_does_not_match_exact_blocker_field() -> None:
    field = "characteristics[cell identifier]"
    ev = EvidenceRecord(
        accession="PXD920001",
        artifact_sha256="f" * 64,
        blocker_field="",
        trust_class="trusted_independent",
        provenance_status="independent_source_provenance_present",
        is_independent=True,
    )
    cap = ResolverCapability(
        resolver_id="r",
        version="1",
        supported_fields=(field,),
        accepted_trust_classes=("trusted_independent",),
    )
    d = decide_case(
        accession="PXD920001",
        input_state="blocked_metadata_incomplete",
        reason_code="cell identifier unresolved",
        blocker_fields=[field],
        candidate_sha256="1" * 64,
        evidence=[ev],
        resolvers=[cap],
        attempts=[],
        policy_version="p",
    )
    assert d.terminal_state == "EVIDENCE_LIMITED"
    assert d.next_action == ""


def test_evidence_set_hash_changes_with_provenance_identity() -> None:
    field = "characteristics[cell identifier]"
    base = EvidenceRecord(
        accession="PXD930001",
        artifact_sha256="a" * 64,
        blocker_field=field,
        source_provider="PRIDE",
        source_locator="https://example.org/a.tsv",
        trust_class="trusted_deposited",
        independence_class="deposited_repository",
        provenance_status="independent_source_provenance_present",
        is_independent=True,
    )
    changed = EvidenceRecord(
        accession="PXD930001",
        artifact_sha256="a" * 64,
        blocker_field=field,
        source_provider="PRIDE",
        source_locator="https://example.org/other.tsv",
        trust_class="trusted_deposited",
        independence_class="deposited_repository",
        provenance_status="independent_source_provenance_present",
        is_independent=True,
    )
    assert evidence_set_sha256([base]) != evidence_set_sha256([changed])


def test_provenance_registry_preserves_external_copy_and_parent_lineage(tmp_path: Path) -> None:
    from sdrf_evidence_registry import build_registry, sha256_file as registry_sha

    parent = tmp_path / "source.tsv"
    parent.write_text("raw\tvalue\na.raw\tx\n", encoding="utf-8")
    child = tmp_path / "parsed.tsv"
    child.write_text("raw\tvalue\ta.raw\tx\n", encoding="utf-8")
    psha = registry_sha(parent)
    manifest = tmp_path / "sources.tsv"
    manifest.write_text(
        "accession\tlocal_path\ttrust_class\tsource_locator\tsource_provider\tparent_artifact_sha256\tderivation_operation\n"
        f"PXD930001\t{parent}\ttrusted_deposited\thttps://example.org/deposited.tsv\tPRIDE\t\t\n"
        f"PXD930001\t{child}\ttrusted_deposited\t\tPRIDE\t{psha}\tparse_table\n",
        encoding="utf-8",
    )
    rows, _ = build_registry([manifest], None)
    parent_row = next(r for r in rows if r["local_path"] == str(parent))
    child_row = next(r for r in rows if r["local_path"] == str(child))
    assert parent_row["independence_class"] == "deposited_repository"
    assert child_row["independence_class"] == "derived_from_trusted_source"
    assert child_row["is_independent"] == "true"
    assert child_row["parent_artifact_sha256"] == psha


def test_provenance_registry_candidate_parent_fails_closed(tmp_path: Path) -> None:
    from sdrf_evidence_registry import build_registry, sha256_file as registry_sha

    candidate = tmp_path / "candidate.tsv"
    candidate.write_text("raw\tvalue\na.raw\tx\n", encoding="utf-8")
    csha = registry_sha(candidate)
    derived = tmp_path / "derived.tsv"
    derived.write_text("raw\tvalue\ta.raw\ty\n", encoding="utf-8")
    candidates = tmp_path / "candidates.tsv"
    candidates.write_text(
        f"accession\tcandidate_sha256\nPXD930001\t{csha}\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "sources.tsv"
    manifest.write_text(
        "accession\tlocal_path\ttrust_class\tparent_artifact_sha256\tderivation_operation\n"
        f"PXD930001\t{derived}\ttrusted_deposited\t{csha}\tparse_table\n",
        encoding="utf-8",
    )
    rows, _ = build_registry([manifest], candidates)
    assert rows[0]["independence_class"] == "candidate_derived"
    assert rows[0]["is_independent"] == "false"


def test_duplicate_content_paths_collapse_to_one_evidence_identity(tmp_path: Path) -> None:
    from sdrf_evidence_registry import build_registry

    a = tmp_path / "a.tsv"
    b = tmp_path / "b.tsv"
    a.write_text("x\n1\n", encoding="utf-8")
    b.write_bytes(a.read_bytes())
    manifest = tmp_path / "sources.tsv"
    manifest.write_text(
        "accession\tlocal_path\tblocker_field\ttrust_class\tsource_locator\n"
        f"PXD930001\t{a}\tcharacteristics[cell identifier]\ttrusted_deposited\thttps://example.org/a.tsv\n"
        f"PXD930001\t{b}\tcharacteristics[cell identifier]\ttrusted_deposited\thttps://example.org/a.tsv\n",
        encoding="utf-8",
    )
    rows, summary = build_registry([manifest], None)
    assert len(rows) == 1
    assert summary["records"] == 1


def test_acquisition_planner_prioritizes_deposited_source_and_caches_exhaustion() -> None:
    from sdrf_evidence_acquisition_planner import (
        AcquisitionAttempt,
        SourceStrategy,
        plan_acquisition_for_case,
    )

    strategies = [
        SourceStrategy("publication_supplement", 40, ("single_cell_isolation",), (), "pub", "supp", "trusted_publication_supplement"),
        SourceStrategy("deposited_sdrf", 10, ("single_cell_isolation",), (), "PRIDE", "deposited", "trusted_deposited"),
    ]
    kwargs = dict(
        accession="PXD930001",
        blocker_family="single_cell_isolation",
        blocker_fields=["characteristics[single cell isolation protocol]"],
        blocker_key="b" * 64,
        evidence_set_sha256="e" * 64,
        policy_version="p",
        strategies=strategies,
        strategy_version="v1",
        max_source_classes=2,
    )
    rows = plan_acquisition_for_case(attempts=[], **kwargs)
    assert [r["source_class"] for r in rows] == ["deposited_sdrf"]
    exhausted = AcquisitionAttempt(
        stage_key=rows[0]["stage_key"], accession="PXD930001", source_class="deposited_sdrf",
        status="source_exhausted", evidence_set_sha256="e" * 64, policy_version="p",
        blocker_key="b" * 64, strategy_version="v1",
    )
    rows2 = plan_acquisition_for_case(attempts=[exhausted], **kwargs)
    assert [r["source_class"] for r in rows2] == ["publication_supplement"]


def test_harness_acquisition_mode_turns_evidence_limited_into_acquire_evidence(tmp_path: Path) -> None:
    import json
    from sdrf_annotation_harness import plan, write_tsv

    (tmp_path / "accessions.txt").write_text("PXD930001\n", encoding="utf-8")
    write_tsv(
        tmp_path / "candidates.tsv",
        [{"accession": "PXD930001", "candidate_sha256": "1" * 64}],
        ["accession", "candidate_sha256"],
    )
    write_tsv(
        tmp_path / "blockers.tsv",
        [{
            "accession": "PXD930001",
            "state": "blocked_metadata_incomplete",
            "reason_code": "single_cell_isolation_unresolved",
            "blocker_fields": "characteristics[single cell isolation protocol]",
        }],
        ["accession", "state", "reason_code", "blocker_fields"],
    )
    catalog = {
        "schema_version": "pride-scp-sdrf-evidence-source-catalog-v1",
        "catalog_revision": "test-v1",
        "strategies": [{
            "source_class": "deposited_sdrf",
            "priority": 10,
            "families": ["single_cell_isolation"],
            "provider": "PRIDE",
            "acquisition_method": "deposited_structured_metadata",
            "expected_trust_class": "trusted_deposited",
        }],
    }
    (tmp_path / "source_catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    spec = {
        "schema_version": "pride-scp-sdrf-annotation-run-spec-v1",
        "run_id": "acq-test",
        "provenance": {"policy_version": "p"},
        "inputs": {
            "accessions_file": "accessions.txt",
            "candidate_manifest": "candidates.tsv",
            "blocker_manifest": "blockers.tsv",
        },
        "evidence_acquisition": {
            "enabled": True,
            "source_catalog": "source_catalog.json",
            "max_source_classes_per_accession": 1,
        },
    }
    spec_path = tmp_path / "run_spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    summary = plan(spec_path, tmp_path / "out")
    assert summary["decision_counts"] == {"ACQUIRE_EVIDENCE": 1}
    rows = list(csv.DictReader((tmp_path / "out" / "evidence_acquisition_plan.tsv").open(), delimiter="\t"))
    assert rows[0]["source_class"] == "deposited_sdrf"


def test_candidate_missing_can_plan_structured_source_discovery() -> None:
    from sdrf_evidence_acquisition_planner import SourceStrategy, plan_acquisition_for_case

    strategy = SourceStrategy(
        "deposited_sdrf", 10, ("candidate_missing",), (), "PRIDE", "deposited", "trusted_deposited"
    )
    rows = plan_acquisition_for_case(
        accession="PXD940001",
        blocker_family="candidate_missing",
        blocker_fields=[],
        blocker_key="b" * 64,
        evidence_set_sha256="e" * 64,
        policy_version="p",
        strategies=[strategy],
        attempts=[],
        strategy_version="v1",
        max_source_classes=4,
    )
    assert len(rows) == 1
    assert rows[0]["source_class"] == "deposited_sdrf"


def test_harness_returns_to_evidence_limited_when_acquisition_source_is_exhausted(tmp_path: Path) -> None:
    import json
    from sdrf_annotation_harness import plan, write_tsv

    (tmp_path / "accessions.txt").write_text("PXD940001\n", encoding="utf-8")
    write_tsv(
        tmp_path / "blockers.tsv",
        [{"accession": "PXD940001", "state": "candidate_missing", "reason_code": "candidate_missing", "blocker_fields": ""}],
        ["accession", "state", "reason_code", "blocker_fields"],
    )
    catalog = {
        "schema_version": "pride-scp-sdrf-evidence-source-catalog-v1",
        "catalog_revision": "test-v1",
        "strategies": [{
            "source_class": "deposited_sdrf",
            "priority": 10,
            "families": ["candidate_missing"],
            "provider": "PRIDE",
            "acquisition_method": "deposited_structured_metadata",
            "expected_trust_class": "trusted_deposited",
        }],
    }
    (tmp_path / "source_catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    spec = {
        "schema_version": "pride-scp-sdrf-annotation-run-spec-v1",
        "run_id": "acq-exhaust-test",
        "provenance": {"policy_version": "p"},
        "inputs": {"accessions_file": "accessions.txt", "blocker_manifest": "blockers.tsv"},
        "evidence_acquisition": {
            "enabled": True,
            "source_catalog": "source_catalog.json",
            "max_source_classes_per_accession": 1,
        },
    }
    spec_path = tmp_path / "run_spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    first = plan(spec_path, tmp_path / "out1")
    assert first["decision_counts"] == {"ACQUIRE_EVIDENCE": 1}
    planned = list(csv.DictReader((tmp_path / "out1" / "evidence_acquisition_plan.tsv").open(), delimiter="\t"))[0]
    write_tsv(
        tmp_path / "acq_attempts.tsv",
        [{
            "stage_key": planned["stage_key"],
            "accession": planned["accession"],
            "source_class": planned["source_class"],
            "status": "source_exhausted",
            "evidence_set_sha256": planned["evidence_set_sha256"],
            "policy_version": planned["policy_version"],
            "blocker_key": planned["blocker_key"],
            "strategy_version": planned["strategy_version"],
        }],
        ["stage_key", "accession", "source_class", "status", "evidence_set_sha256", "policy_version", "blocker_key", "strategy_version"],
    )
    spec["evidence_acquisition"]["attempt_ledger"] = "acq_attempts.tsv"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    second = plan(spec_path, tmp_path / "out2")
    assert second["decision_counts"] == {"EVIDENCE_LIMITED": 1}
    assert second["evidence_acquisition_plan_count"] == 0


def test_production_source_catalog_keeps_depositor_sdrf_provenance_gated() -> None:
    catalog_path = Path(__file__).resolve().parents[1] / "resources" / "sdrf_evidence_source_catalog_v1.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    first = min(catalog["strategies"], key=lambda row: int(row["priority"]))
    assert first["source_class"] == "depositor_sdrf"
    assert first["expected_trust_class"] == "provenance_gated_depositor_candidate"
    assert first["expected_trust_class"] != "trusted_deposited"


def test_provider_manifest_identity_dedup_and_changed_bytes(tmp_path: Path) -> None:
    from sdrf_evidence_registry import build_registry
    from sdrf_evidence_registry import sha256_file as registry_sha

    artifact = tmp_path / "depositor.sdrf.tsv"
    artifact.write_text("source name\tcomment[data file]\na\ta.raw\n", encoding="utf-8")
    sha1 = registry_sha(artifact)
    size1 = artifact.stat().st_size
    manifest = tmp_path / "source_manifest.tsv"
    header = (
        "accession\tsource_kind\tsource_provider\tsource_locator\tretrieval_method\t"
        "original_filename\tlocal_path\ttrust_class\tblocker_field\tderivation_operation\t"
        "artifact_sha256\tartifact_size_bytes\n"
    )
    row = (
        f"PXD930001\tdepositor_sdrf_candidate\tPRIDE Archive\thttps://example.org/a.tsv\t"
        f"pride_project_file_download\tdepositor.sdrf.tsv\t{artifact}\tuntrusted_or_unknown\t"
        f"characteristics[cell identifier]\tdownload_copy\t{sha1}\t{size1}\n"
    )
    manifest.write_text(header + row + row, encoding="utf-8")
    rows, summary = build_registry([manifest], None)
    assert len(rows) == 1
    assert summary["records"] == 1
    assert rows[0]["artifact_sha256"] == sha1
    assert rows[0]["sha_verified"] == "true"
    assert rows[0]["is_independent"] == "false"
    assert rows[0]["provenance_status"] == "locator_present_but_trust_unproven"

    artifact.write_text("source name\tcomment[data file]\nb\tb.raw\n", encoding="utf-8")
    sha2 = registry_sha(artifact)
    assert sha2 != sha1
    try:
        build_registry([manifest], None)
    except ValueError as exc:
        assert "declared SHA mismatch" in str(exc)
    else:
        raise AssertionError("changed bytes reused an old provider identity")


def test_provider_community_metadata_is_not_registered_as_independent_evidence(tmp_path: Path) -> None:
    from sdrf_evidence_registry import build_registry

    manifest = tmp_path / "source_manifest.tsv"
    manifest.write_text(
        "accession\tsource_kind\tsource_provider\tsource_locator\tlocal_path\ttrust_class\tartifact_sha256\tartifact_size_bytes\n"
        "PXD930001\tpride_community_annotated_sdrf\tPRIDE Archive\thttps://example.org/community.tsv\t\t"
        "community_curated_untrusted_for_independence\t\t0\n",
        encoding="utf-8",
    )
    rows, summary = build_registry([manifest], None)
    assert rows == []
    assert summary["independent_records"] == 0


def test_provider_ingestion_rewrites_registry_and_replans_with_new_evidence_sha(tmp_path: Path) -> None:
    from sdrf_annotation_harness import ingest_provider_manifest_and_replan, write_tsv
    from sdrf_evidence_registry import sha256_file as registry_sha

    (tmp_path / "accessions.txt").write_text("PXD930001\n", encoding="utf-8")
    write_tsv(
        tmp_path / "candidates.tsv",
        [{"accession": "PXD930001", "candidate_sha256": "1" * 64}],
        ["accession", "candidate_sha256"],
    )
    write_tsv(
        tmp_path / "blockers.tsv",
        [{
            "accession": "PXD930001",
            "state": "blocked_metadata_incomplete",
            "reason_code": "cell_identifier_invalid_or_unresolved",
            "blocker_fields": "characteristics[cell identifier]",
        }],
        ["accession", "state", "reason_code", "blocker_fields"],
    )
    (tmp_path / "evidence.tsv").write_text("accession\tartifact_sha256\n", encoding="utf-8")
    spec = {
        "schema_version": "pride-scp-sdrf-annotation-run-spec-v1",
        "run_id": "provider-ingest-test",
        "provenance": {"policy_version": "p"},
        "inputs": {
            "accessions_file": "accessions.txt",
            "candidate_manifest": "candidates.tsv",
            "blocker_manifest": "blockers.tsv",
            "evidence_registry": "evidence.tsv",
        },
    }
    spec_path = tmp_path / "run_spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    __import__("sdrf_annotation_harness").plan(spec_path, tmp_path / "before")
    before_row = next(csv.DictReader((tmp_path / "before" / "state_ledger.tsv").open(), delimiter="\t"))

    artifact = tmp_path / "provider.tsv"
    artifact.write_text("raw\tcell\na.raw\tcell-1\n", encoding="utf-8")
    sha = registry_sha(artifact)
    provider = tmp_path / "source_manifest.tsv"
    provider.write_text(
        "accession\tsource_kind\tsource_provider\tsource_locator\tlocal_path\ttrust_class\tblocker_field\t"
        "derivation_operation\tartifact_sha256\tartifact_size_bytes\n"
        f"PXD930001\tdepositor_sdrf_candidate\tPRIDE Archive\thttps://example.org/provider.tsv\t{artifact}\t"
        f"untrusted_or_unknown\tcharacteristics[cell identifier]\tdownload_copy\t{sha}\t{artifact.stat().st_size}\n",
        encoding="utf-8",
    )
    after = ingest_provider_manifest_and_replan(spec_path, provider, tmp_path / "after")
    after_row = next(csv.DictReader((tmp_path / "after" / "state_ledger.tsv").open(), delimiter="\t"))
    assert before_row["evidence_set_sha256"] != after_row["evidence_set_sha256"]
    assert after["provider_ingestion"]["records"] == 1
    registry_rows = list(csv.DictReader((tmp_path / "evidence.tsv").open(), delimiter="\t"))
    assert registry_rows[0]["artifact_sha256"] == sha
    assert registry_rows[0]["is_independent"] == "false"
