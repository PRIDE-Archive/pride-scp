from __future__ import annotations

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
