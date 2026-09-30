"""State, hashing and decision primitives for the PRIDE-SCP SDRF annotation harness.

This module is intentionally orchestration-only.  It does not mutate SDRFs and it does
not weaken any scientific/readiness policy.  The harness uses these primitives to make
repeatable decisions from immutable candidate/evidence identities and normalized blocker
records.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

VERSION = "pride-scp-sdrf-annotation-state-v2.2.2"

TERMINAL_STATES = {
    "SUBMISSION_READY",
    "HUMAN_REVIEW_ACTIONABLE",
    "IMPLEMENTATION_CANDIDATE",
    "PROVENANCE_CONFLICT",
    "EVIDENCE_LIMITED",
}

NONTERMINAL_STATE = "PENDING_RESOLUTION"

NO_PROGRESS_STATUSES = {
    "no_change",
    "no_applicable_evidence",
    "same_validation_errors",
    "unsupported_or_exhausted",
    "evidence_limited",
}

PLACEHOLDERS = {
    "",
    "not available",
    "not applicable",
    "unknown",
    "na",
    "n/a",
    "none",
    "null",
}

# Order matters: more specific blocker classes precede broad classes.
BLOCKER_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("provenance_conflict", ("provenance_conflict", "provenance conflict", "source conflict")),
    ("template_gap", ("template_gap", "template gap", "unsupported template")),
    ("parser_gap", ("parser gap", "parser_gap", "unsupported structured artifact", "structured artifact parser")),
    ("cell_line_composite", ("composite cell-line", "composite cell line", "cell-line label", "cell line]")),
    ("cellosaurus_accession", ("cellosaurus accession", "cellosaurus")),
    ("single_cell_isolation", ("single_cell_isolation", "single cell isolation", "isolation protocol")),
    ("cell_identifier", ("cell_identifier", "cell identifier")),
    ("required_integer", ("required_integer", "required integer", "integer_invalid")),
    ("dissociation_method", ("dissociation method", "dissociation_unresolved")),
    ("label", ("comment[label]", "labeling_mapping", "label mapping", "reporter label")),
    ("row_mapping", ("row_mapping", "row mapping", "mapping_unresolved", "mapping incomplete")),
    ("material_ontology", ("ontology_mapping", "ontology", "material ambiguity")),
    ("metadata_incomplete", ("metadata_incomplete", "metadata incomplete", "missing_required")),
    ("validator_compatibility", ("validator compatibility", "validator drift", "skills compatibility")),
    ("candidate_missing", ("candidate_missing", "no_trusted_candidate", "no source-closed candidate")),
    ("semantic_conflict", ("semantic conflict", "evidence contradiction", "scientific conflict")),
)


@dataclass(frozen=True)
class ResolverCapability:
    resolver_id: str
    version: str
    supported_fields: tuple[str, ...] = ()
    supported_families: tuple[str, ...] = ()
    accepted_trust_classes: tuple[str, ...] = ("trusted_independent", "trusted_deposited")
    requires_independent_evidence: bool = True
    can_fill_placeholder: bool = True
    can_narrow_composite: bool = False
    can_expand_multiplex: bool = False
    overwrite_policy: str = "never_conflicting_concrete"

    @classmethod
    def from_dict(cls, obj: dict[str, Any]) -> ResolverCapability:
        return cls(
            resolver_id=str(obj["resolver_id"]),
            version=str(obj.get("version") or "unknown"),
            supported_fields=tuple(str(x) for x in obj.get("supported_fields") or ()),
            supported_families=tuple(str(x) for x in obj.get("supported_families") or ()),
            accepted_trust_classes=tuple(str(x) for x in obj.get("accepted_trust_classes") or ()),
            requires_independent_evidence=bool(obj.get("requires_independent_evidence", True)),
            can_fill_placeholder=bool(obj.get("can_fill_placeholder", True)),
            can_narrow_composite=bool(obj.get("can_narrow_composite", False)),
            can_expand_multiplex=bool(obj.get("can_expand_multiplex", False)),
            overwrite_policy=str(obj.get("overwrite_policy") or "never_conflicting_concrete"),
        )


@dataclass(frozen=True)
class EvidenceRecord:
    accession: str
    artifact_sha256: str
    blocker_field: str = ""
    source_kind: str = ""
    source_provider: str = ""
    source_locator: str = ""
    local_path: str = ""
    trust_class: str = "untrusted_or_unknown"
    independence_class: str = "provenance_unknown"
    provenance_status: str = "unknown"
    is_independent: bool = False
    parent_artifact_sha256: str = ""
    derivation_operation: str = ""
    retrieved_at: str = ""
    retrieval_method: str = ""
    original_filename: str = ""
    media_type: str = ""
    claim_value: str = ""
    claim_status: str = ""
    claim_rule_id: str = ""
    claim_text: str = ""
    claim_source_start: str = ""
    claim_source_end: str = ""
    claim_extractor_version: str = ""


@dataclass(frozen=True)
class AttemptRecord:
    stage_key: str
    accession: str
    resolver_id: str
    status: str
    candidate_sha256: str
    evidence_set_sha256: str
    policy_version: str
    blocker_key: str


@dataclass
class CaseDecision:
    accession: str
    input_state: str
    reason_code: str
    blocker_family: str
    blocker_fields: list[str]
    candidate_sha256: str
    evidence_set_sha256: str
    independent_evidence_count: int
    independent_evidence_fields: list[str] = field(default_factory=list)
    applicable_resolvers: list[str] = field(default_factory=list)
    cached_no_progress_resolvers: list[str] = field(default_factory=list)
    next_action: str = ""
    terminal_state: str = ""
    decision_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def normalize_field_name(value: str) -> str:
    s = re.sub(r"\s+", " ", str(value or "").strip()).lower()
    # Historical serialization bugs occasionally wrapped a field in Python-list syntax.
    m = re.fullmatch(r"\['(.+)'\]", s)
    if m:
        s = m.group(1)
    if s == "[]":
        return ""
    return s


def normalize_blocker_family(state: str, reason_code: str, blocker_fields: Iterable[str] = ()) -> str:
    text = " ".join(
        [str(state or ""), str(reason_code or ""), *(str(x or "") for x in blocker_fields)]
    ).lower()
    for family, needles in BLOCKER_PATTERNS:
        if any(n in text for n in needles):
            return family
    if "submission_ready" in text:
        return "closed"
    return "unknown"


def blocker_key(family: str, fields: Iterable[str]) -> str:
    normalized = sorted({normalize_field_name(x) for x in fields if normalize_field_name(x)})
    return canonical_sha256({"family": family, "fields": normalized})


def evidence_set_sha256(records: Iterable[EvidenceRecord]) -> str:
    rows = sorted(
        {
            (
                r.accession.upper(),
                r.artifact_sha256.lower(),
                normalize_field_name(r.blocker_field),
                r.source_provider,
                r.source_locator,
                r.trust_class,
                r.independence_class,
                r.provenance_status,
                bool(r.is_independent),
                r.parent_artifact_sha256.lower(),
                r.derivation_operation,
            )
            for r in records
            if r.artifact_sha256
        }
    )
    return canonical_sha256(rows)


def stage_key(
    *,
    accession: str,
    candidate_sha256: str,
    evidence_set_sha256_value: str,
    resolver_id: str,
    resolver_version: str,
    policy_version: str,
    blocker_key_value: str,
) -> str:
    return canonical_sha256(
        {
            "accession": accession.upper(),
            "candidate_sha256": candidate_sha256.lower(),
            "evidence_set_sha256": evidence_set_sha256_value.lower(),
            "resolver_id": resolver_id,
            "resolver_version": resolver_version,
            "policy_version": policy_version,
            "blocker_key": blocker_key_value,
        }
    )


def resolver_supports(capability: ResolverCapability, family: str, fields: Iterable[str]) -> bool:
    normalized_fields = {normalize_field_name(x) for x in fields if normalize_field_name(x)}
    supported_fields = {normalize_field_name(x) for x in capability.supported_fields}
    if normalized_fields and supported_fields.intersection(normalized_fields):
        return True
    return family in set(capability.supported_families)


def trusted_independent_evidence(
    evidence: Iterable[EvidenceRecord],
    *,
    accession: str,
    blocker_fields: Iterable[str],
    accepted_trust_classes: Iterable[str] | None = None,
) -> list[EvidenceRecord]:
    fields = {normalize_field_name(x) for x in blocker_fields if normalize_field_name(x)}
    # Evidence is blocker-aligned only when the blocker identifies at least one exact
    # SDRF field.  An empty blocker field set must fail closed: otherwise an unscoped
    # independent source (for example a publication full text) could authorize a
    # family-level resolver such as row_mapping without any field-specific claim.
    if not fields:
        return []
    accepted = set(accepted_trust_classes or ())
    out: list[EvidenceRecord] = []
    for record in evidence:
        if record.accession.upper() != accession.upper() or not record.is_independent:
            continue
        if accepted and record.trust_class not in accepted:
            continue
        field = normalize_field_name(record.blocker_field)
        if fields and field not in fields:
            continue
        out.append(record)
    return out


def decide_case(
    *,
    accession: str,
    input_state: str,
    reason_code: str,
    blocker_fields: Iterable[str],
    candidate_sha256: str,
    evidence: Iterable[EvidenceRecord],
    resolvers: Iterable[ResolverCapability],
    attempts: Iterable[AttemptRecord],
    policy_version: str,
) -> CaseDecision:
    fields = sorted({normalize_field_name(x) for x in blocker_fields if normalize_field_name(x)})
    family = normalize_blocker_family(input_state, reason_code, fields)
    bkey = blocker_key(family, fields)
    evidence_rows = list(evidence)
    esha = evidence_set_sha256(evidence_rows)

    result = CaseDecision(
        accession=accession.upper(),
        input_state=input_state,
        reason_code=reason_code,
        blocker_family=family,
        blocker_fields=fields,
        candidate_sha256=candidate_sha256,
        evidence_set_sha256=esha,
        independent_evidence_count=0,
    )

    if input_state == "submission_ready":
        result.terminal_state = "SUBMISSION_READY"
        result.decision_reason = "frozen_readiness_already_submission_ready"
        return result

    if family == "provenance_conflict":
        result.terminal_state = "PROVENANCE_CONFLICT"
        result.decision_reason = "explicit_provenance_conflict_fails_closed"
        return result

    resolver_list = list(resolvers)
    attempt_by_key = {x.stage_key: x for x in attempts}
    independent_union: dict[tuple[str, str], EvidenceRecord] = {}

    for cap in resolver_list:
        if not resolver_supports(cap, family, fields):
            continue
        accepted = cap.accepted_trust_classes if cap.requires_independent_evidence else ()
        supporting = trusted_independent_evidence(
            evidence_rows,
            accession=accession,
            blocker_fields=fields,
            accepted_trust_classes=accepted,
        )
        if cap.requires_independent_evidence and not supporting:
            continue
        for record in supporting:
            independent_union[(record.artifact_sha256, normalize_field_name(record.blocker_field))] = record
        skey = stage_key(
            accession=accession,
            candidate_sha256=candidate_sha256,
            evidence_set_sha256_value=esha,
            resolver_id=cap.resolver_id,
            resolver_version=cap.version,
            policy_version=policy_version,
            blocker_key_value=bkey,
        )
        prior = attempt_by_key.get(skey)
        if prior and prior.status in NO_PROGRESS_STATUSES:
            result.cached_no_progress_resolvers.append(cap.resolver_id)
            continue
        result.applicable_resolvers.append(cap.resolver_id)

    # Count all independent blocker-aligned evidence, not only evidence accepted by one resolver.
    all_independent = trusted_independent_evidence(
        evidence_rows,
        accession=accession,
        blocker_fields=fields,
        accepted_trust_classes=None,
    )
    result.independent_evidence_count = len(
        {(x.artifact_sha256, normalize_field_name(x.blocker_field)) for x in all_independent}
    )
    result.independent_evidence_fields = sorted(
        {normalize_field_name(x.blocker_field) for x in all_independent if normalize_field_name(x.blocker_field)}
    )

    if result.applicable_resolvers:
        result.next_action = "RUN_RESOLVER"
        result.decision_reason = "independent_blocker_aligned_evidence_and_applicable_resolver"
        return result

    if result.cached_no_progress_resolvers:
        result.terminal_state = "EVIDENCE_LIMITED"
        result.decision_reason = "all_applicable_resolvers_cached_no_progress_for_same_content_key"
        return result

    if result.independent_evidence_count > 0:
        result.terminal_state = "HUMAN_REVIEW_ACTIONABLE"
        result.decision_reason = "independent_blocker_evidence_present_but_no_registered_resolver"
        return result

    result.terminal_state = "EVIDENCE_LIMITED"
    if not fields:
        result.decision_reason = "no_exact_blocker_field_and_no_applicable_independent_evidence"
    else:
        result.decision_reason = "no_independent_blocker_aligned_evidence"
    return result


def apply_generic_implementation_gate(
    decisions: list[CaseDecision],
    *,
    threshold: int,
) -> dict[str, list[str]]:
    """Promote repeated unsupported-but-evidenced blocker fields to implementation candidates.

    Only HUMAN_REVIEW_ACTIONABLE cases are eligible here: they already have independent evidence
    but no registered resolver.  A generic code change is authorized only when the same exact
    blocker field reaches the cohort threshold.
    """
    by_field: dict[str, list[CaseDecision]] = {}
    for d in decisions:
        if d.terminal_state != "HUMAN_REVIEW_ACTIONABLE":
            continue
        for field_name in d.independent_evidence_fields:
            if field_name in d.blocker_fields:
                by_field.setdefault(field_name, []).append(d)

    promoted: dict[str, list[str]] = {}
    for field_name, rows in sorted(by_field.items()):
        unique = {x.accession for x in rows}
        if len(unique) < threshold:
            continue
        promoted[field_name] = sorted(unique)
        for d in rows:
            if d.accession in unique:
                d.terminal_state = "IMPLEMENTATION_CANDIDATE"
                d.decision_reason = (
                    f"generic_blocker_field_threshold_met:{field_name}:{len(unique)}>={threshold}"
                )
    return promoted


def builtin_resolver_catalog() -> list[ResolverCapability]:
    return [
        ResolverCapability(
            resolver_id="structured_mapping_v2",
            version="pride-scp-structured-row-mapping-resolver-v2",
            supported_fields=(
                "source name",
                "assay name",
                "characteristics[cell identifier]",
                "characteristics[biological replicate]",
                "characteristics[cell line]",
                "characteristics[cellosaurus accession]",
                "characteristics[organism]",
                "characteristics[organism part]",
                "characteristics[sample type]",
                "characteristics[individual]",
                "comment[label]",
            ),
            supported_families=("row_mapping",),
            accepted_trust_classes=(
                "trusted_independent",
                "trusted_deposited",
                "trusted_local_deposited_sdrf",
                "trusted_publication_supplement",
            ),
            can_fill_placeholder=True,
            can_narrow_composite=True,
            can_expand_multiplex=True,
        ),
        ResolverCapability(
            resolver_id="cellosaurus_exact_v1",
            version="pride-scp-cellosaurus-resolver-v1",
            supported_fields=("characteristics[cellosaurus accession]",),
            supported_families=("cellosaurus_accession",),
            accepted_trust_classes=(
                "trusted_independent",
                "trusted_deposited",
                "trusted_local_deposited_sdrf",
            ),
            can_fill_placeholder=True,
        ),
    ]
