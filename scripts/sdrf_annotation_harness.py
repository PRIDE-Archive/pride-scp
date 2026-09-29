#!/usr/bin/env python3
"""PRIDE-SCP SDRF annotation harness v2.1 decision/orchestration layer.

v2.1 deliberately does not replace the frozen scientific components.  It consumes their
machine-readable artifacts, normalizes accession state, prevents repeated no-progress work,
and emits the next deterministic queue:

  RUN_RESOLVER
  ACQUIRE_EVIDENCE
  SUBMISSION_READY
  HUMAN_REVIEW_ACTIONABLE
  IMPLEMENTATION_CANDIDATE
  PROVENANCE_CONFLICT
  EVIDENCE_LIMITED

The harness never rewrites an SDRF and never promotes an accession to submission-ready by
itself.  That authority remains with the frozen readiness + validation + review chain.
"""
from __future__ import annotations

import argparse
import csv
import json
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from sdrf_annotation_state import (
    AttemptRecord,
    EvidenceRecord,
    ResolverCapability,
    apply_generic_implementation_gate,
    blocker_key,
    builtin_resolver_catalog,
    decide_case,
    normalize_field_name,
    sha256_file,
    stage_key,
)
from sdrf_evidence_acquisition_planner import (
    load_attempts as load_acquisition_attempts,
)
from sdrf_evidence_acquisition_planner import (
    load_catalog as load_source_catalog,
)
from sdrf_evidence_acquisition_planner import (
    plan_acquisition_for_case,
)
from sdrf_evidence_registry import build_registry as build_evidence_registry
from sdrf_publication_evidence import build_publication_source_rows
from sdrf_publication_evidence import write_tsv as write_publication_source_tsv

VERSION = "pride-scp-sdrf-annotation-harness-v2.3.0"
RUN_SPEC_VERSION = "pride-scp-sdrf-annotation-run-spec-v1"


def read_tsv(path: Path | None) -> list[dict[str, str]]:
    if path is None or not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
        return [dict(row) for row in csv.DictReader(fh, delimiter="\t")]


def write_tsv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, delimiter="\t", lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def read_accessions(path: Path) -> list[str]:
    out = []
    for line in path.read_text(errors="replace").splitlines():
        token = line.strip().split("\t", 1)[0].strip().upper()
        if token.startswith("PXD"):
            out.append(token)
    return sorted(set(out))


def resolve_path(base: Path, value: str | None) -> Path | None:
    if not value:
        return None
    p = Path(value)
    return p if p.is_absolute() else (base / p).resolve()


def load_json(path: Path) -> dict[str, Any]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise TypeError(f"expected JSON object: {path}")
    return obj


def split_fields(value: str) -> list[str]:
    raw = str(value or "").strip()
    if not raw:
        return []
    # Accept compact JSON arrays as well as semicolon-separated ledgers.
    if raw.startswith("["):
        try:
            parsed = json.loads(raw.replace("'", '"'))
            if isinstance(parsed, list):
                return [normalize_field_name(x) for x in parsed if normalize_field_name(x)]
        except json.JSONDecodeError:
            pass
    return [normalize_field_name(x) for x in raw.split(";") if normalize_field_name(x)]


def load_candidates(path: Path | None) -> dict[str, dict[str, str]]:
    out = {}
    base = path.parent if path is not None else Path.cwd()
    for row in read_tsv(path):
        acc = (row.get("accession") or "").strip().upper()
        if not acc:
            continue
        expected = (row.get("candidate_sha256") or "").strip().lower()
        candidate_path = (row.get("candidate_path") or "").strip()
        verified = False
        if candidate_path:
            cp = Path(candidate_path)
            if not cp.is_absolute():
                cp = (base / cp).resolve()
            if not cp.is_file():
                raise FileNotFoundError(f"{acc}: candidate missing: {cp}")
            actual = sha256_file(cp)
            if expected and actual != expected:
                raise ValueError(f"{acc}: candidate SHA mismatch: {actual} != {expected}")
            row["candidate_path"] = str(cp)
            row["candidate_sha256"] = actual
            expected = actual
            verified = True
        row["candidate_sha256"] = expected
        row["sha_verified"] = str(verified).lower()
        out[acc] = row
    return out


def load_blockers(path: Path | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in read_tsv(path):
        acc = (row.get("accession") or "").strip().upper()
        if not acc:
            continue
        fields = split_fields(row.get("blocker_fields", ""))
        if not fields and row.get("blocker_field"):
            fields = split_fields(row["blocker_field"])
        out[acc] = {
            "state": row.get("state") or row.get("baseline_terminal_state") or row.get("final_state") or "",
            "reason_code": row.get("reason_code") or row.get("baseline_reason_code") or "",
            "blocker_fields": fields,
        }
    return out


def load_evidence(path: Path | None) -> list[EvidenceRecord]:
    out = []
    for row in read_tsv(path):
        sha = (row.get("artifact_sha256") or row.get("source_sha256") or "").strip().lower()
        acc = (row.get("accession") or "").strip().upper()
        if not acc or not sha:
            continue
        independent = (row.get("is_independent") or "").strip().lower() in {"true", "1", "yes"}
        out.append(
            EvidenceRecord(
                accession=acc,
                artifact_sha256=sha,
                blocker_field=row.get("blocker_field", ""),
                source_kind=row.get("source_kind", ""),
                source_provider=row.get("source_provider", ""),
                source_locator=row.get("source_locator", ""),
                local_path=row.get("local_path", ""),
                trust_class=row.get("trust_class", "untrusted_or_unknown"),
                independence_class=row.get("independence_class", "provenance_unknown"),
                provenance_status=row.get("provenance_status", "unknown"),
                is_independent=independent,
                parent_artifact_sha256=row.get("parent_artifact_sha256", ""),
                derivation_operation=row.get("derivation_operation", ""),
                retrieved_at=row.get("retrieved_at", ""),
                retrieval_method=row.get("retrieval_method", ""),
                original_filename=row.get("original_filename", ""),
                media_type=row.get("media_type", ""),
            )
        )
    return out


def load_attempts(path: Path | None) -> list[AttemptRecord]:
    out = []
    for row in read_tsv(path):
        if not row.get("stage_key"):
            continue
        out.append(
            AttemptRecord(
                stage_key=row["stage_key"],
                accession=row.get("accession", ""),
                resolver_id=row.get("resolver_id", ""),
                status=row.get("status", ""),
                candidate_sha256=row.get("candidate_sha256", ""),
                evidence_set_sha256=row.get("evidence_set_sha256", ""),
                policy_version=row.get("policy_version", ""),
                blocker_key=row.get("blocker_key", ""),
            )
        )
    return out


def load_resolvers(path: Path | None) -> list[ResolverCapability]:
    if path is None:
        return builtin_resolver_catalog()
    obj = load_json(path)
    rows = obj.get("resolvers") or []
    if not isinstance(rows, list):
        raise TypeError("resolver catalog must contain a list named 'resolvers'")
    return [ResolverCapability.from_dict(x) for x in rows]


def verify_run_spec(spec_path: Path, spec: dict[str, Any]) -> dict[str, str]:
    if spec.get("schema_version") != RUN_SPEC_VERSION:
        raise ValueError(
            f"unsupported run spec schema {spec.get('schema_version')!r}; expected {RUN_SPEC_VERSION!r}"
        )
    base = spec_path.parent
    provenance = spec.get("provenance") or {}
    sif_path = resolve_path(base, provenance.get("sif_path"))
    expected_sif_sha = str(provenance.get("sif_sha256") or "").lower()
    actual_sif_sha = ""
    if sif_path is not None:
        if not sif_path.is_file():
            raise FileNotFoundError(f"SIF missing: {sif_path}")
        actual_sif_sha = sha256_file(sif_path)
        if expected_sif_sha and expected_sif_sha != actual_sif_sha:
            raise ValueError(f"SIF SHA mismatch: {actual_sif_sha} != {expected_sif_sha}")
    return {
        "run_spec_sha256": sha256_file(spec_path),
        "sif_path": str(sif_path or ""),
        "sif_sha256": actual_sif_sha or expected_sif_sha,
        "git_sha": str(provenance.get("git_sha") or ""),
        "policy_version": str(provenance.get("policy_version") or "unknown"),
    }


EVIDENCE_REGISTRY_FIELDS = [
    "accession", "artifact_sha256", "declared_sha256", "sha_verified", "blocker_field",
    "source_kind", "source_provider", "source_locator", "source_identity", "publication_doi",
    "publication_pmid", "publication_pmcid", "publication_identity_status", "retrieved_at", "retrieval_method",
    "original_filename", "media_type", "local_path", "byte_size", "parent_artifact_sha256",
    "derivation_operation", "trust_class", "independence_class", "is_independent",
    "provenance_status", "candidate_hash_equal", "manifest_path",
]


def ingest_provider_manifest_and_replan(
    spec_path: Path, provider_source_manifest: Path, output: Path
) -> dict[str, Any]:
    """Merge provider evidence by content identity, rewrite the configured registry, then replan."""
    spec = load_json(spec_path)
    base = spec_path.parent
    inputs = spec.get("inputs") or {}
    evidence_registry = resolve_path(base, inputs.get("evidence_registry"))
    if evidence_registry is None:
        raise ValueError("run spec inputs.evidence_registry is required for provider ingestion")
    candidate_manifest = resolve_path(base, inputs.get("candidate_manifest"))
    manifests = [provider_source_manifest]
    if evidence_registry.is_file():
        manifests.insert(0, evidence_registry)
    rows, registry_summary = build_evidence_registry(manifests, candidate_manifest)
    write_tsv(evidence_registry, rows, EVIDENCE_REGISTRY_FIELDS)
    registry_summary_path = evidence_registry.with_name("evidence_registry_summary.json")
    registry_summary_path.write_text(json.dumps(registry_summary, indent=2) + "\n", encoding="utf-8")
    summary = plan(spec_path, output)
    summary["provider_ingestion"] = {
        "source_manifest": str(provider_source_manifest),
        "evidence_registry": str(evidence_registry),
        "evidence_registry_summary": str(registry_summary_path),
        "records": registry_summary["records"],
        "accessions": registry_summary["accessions"],
    }
    (output / "run_manifest.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def ingest_publication_manifests_and_replan(
    spec_path: Path, publication_manifests: list[Path], output: Path
) -> dict[str, Any]:
    """Register trusted cached publication artifacts, then replan without claiming field support."""
    spec = load_json(spec_path)
    base = spec_path.parent
    inputs = spec.get("inputs") or {}
    evidence_registry = resolve_path(base, inputs.get("evidence_registry"))
    if evidence_registry is None:
        raise ValueError("run spec inputs.evidence_registry is required for publication ingestion")
    candidate_manifest = resolve_path(base, inputs.get("candidate_manifest"))

    publication_rows, publication_summary = build_publication_source_rows(publication_manifests)
    output.mkdir(parents=True, exist_ok=True)
    source_manifest = output / "cached_publication_source_manifest.tsv"
    write_publication_source_tsv(source_manifest, publication_rows)

    manifests = [source_manifest]
    if evidence_registry.is_file():
        manifests.insert(0, evidence_registry)
    rows, registry_summary = build_evidence_registry(manifests, candidate_manifest)
    write_tsv(evidence_registry, rows, EVIDENCE_REGISTRY_FIELDS)
    registry_summary_path = evidence_registry.with_name("evidence_registry_summary.json")
    registry_summary_path.write_text(json.dumps(registry_summary, indent=2) + "\n", encoding="utf-8")

    summary = plan(spec_path, output)
    summary["publication_ingestion"] = {
        **publication_summary,
        "source_manifest": str(source_manifest),
        "evidence_registry": str(evidence_registry),
        "evidence_registry_summary": str(registry_summary_path),
        "registry_records": registry_summary["records"],
        "registry_independent_records": registry_summary["independent_records"],
    }
    (output / "run_manifest.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def plan(spec_path: Path, output: Path) -> dict[str, Any]:
    spec = load_json(spec_path)
    verified = verify_run_spec(spec_path, spec)
    base = spec_path.parent
    inputs = spec.get("inputs") or {}
    accessions_file = resolve_path(base, inputs.get("accessions_file"))
    if accessions_file is None or not accessions_file.is_file():
        raise FileNotFoundError("run spec inputs.accessions_file is required")
    accessions = read_accessions(accessions_file)

    candidate_manifest = resolve_path(base, inputs.get("candidate_manifest"))
    blocker_manifest = resolve_path(base, inputs.get("blocker_manifest"))
    evidence_registry = resolve_path(base, inputs.get("evidence_registry"))
    attempt_ledger = resolve_path(base, inputs.get("attempt_ledger"))
    resolver_catalog = resolve_path(base, inputs.get("resolver_catalog"))
    acquisition_cfg = spec.get("evidence_acquisition") or {}
    acquisition_enabled = bool(acquisition_cfg.get("enabled", False))
    source_catalog_path = resolve_path(base, acquisition_cfg.get("source_catalog"))
    source_attempt_ledger_path = resolve_path(base, acquisition_cfg.get("attempt_ledger"))
    max_source_classes = int(acquisition_cfg.get("max_source_classes_per_accession") or 4)

    candidates = load_candidates(candidate_manifest)
    blockers = load_blockers(blocker_manifest)
    evidence = load_evidence(evidence_registry)
    attempts = load_attempts(attempt_ledger)
    resolvers = load_resolvers(resolver_catalog)
    policy_version = verified["policy_version"]
    source_catalog_version = ""
    source_strategies = []
    acquisition_attempts = []
    if acquisition_enabled:
        if source_catalog_path is None or not source_catalog_path.is_file():
            raise FileNotFoundError("evidence_acquisition.source_catalog is required when acquisition is enabled")
        source_catalog_version, source_strategies = load_source_catalog(source_catalog_path)
        acquisition_attempts = load_acquisition_attempts(source_attempt_ledger_path)

    evidence_by_acc: dict[str, list[EvidenceRecord]] = defaultdict(list)
    for row in evidence:
        evidence_by_acc[row.accession].append(row)
    attempts_by_acc: dict[str, list[AttemptRecord]] = defaultdict(list)
    for row in attempts:
        attempts_by_acc[row.accession.upper()].append(row)

    decisions = []
    for acc in accessions:
        cand = candidates.get(acc, {})
        candidate_sha = (cand.get("candidate_sha256") or "").strip().lower()
        if not candidate_sha:
            # Candidate absence is itself a stable blocker state; use a zero sentinel so
            # stage keys remain deterministic without pretending a candidate exists.
            candidate_sha = "0" * 64
        blocker = blockers.get(acc, {})
        state = str(blocker.get("state") or "candidate_missing")
        reason = str(blocker.get("reason_code") or ("candidate_missing" if acc not in candidates else "unclassified"))
        fields = list(blocker.get("blocker_fields") or [])
        decisions.append(
            decide_case(
                accession=acc,
                input_state=state,
                reason_code=reason,
                blocker_fields=fields,
                candidate_sha256=candidate_sha,
                evidence=evidence_by_acc.get(acc, []),
                resolvers=resolvers,
                attempts=attempts_by_acc.get(acc, []),
                policy_version=policy_version,
            )
        )

    threshold = int(spec.get("generic_implementation_threshold") or 3)
    implementation_candidates = apply_generic_implementation_gate(decisions, threshold=threshold)

    acquisition_plan_rows = []
    if acquisition_enabled:
        acquisition_attempts_by_acc: dict[str, list[Any]] = defaultdict(list)
        for attempt in acquisition_attempts:
            acquisition_attempts_by_acc[attempt.accession.upper()].append(attempt)
        for decision in decisions:
            if decision.terminal_state != "EVIDENCE_LIMITED":
                continue
            bkey = blocker_key(decision.blocker_family, decision.blocker_fields)
            planned = plan_acquisition_for_case(
                accession=decision.accession,
                blocker_family=decision.blocker_family,
                blocker_fields=decision.blocker_fields,
                blocker_key=bkey,
                evidence_set_sha256=decision.evidence_set_sha256,
                policy_version=policy_version,
                strategies=source_strategies,
                attempts=acquisition_attempts_by_acc.get(decision.accession, []),
                strategy_version=source_catalog_version,
                max_source_classes=max_source_classes,
            )
            if planned:
                acquisition_plan_rows.extend(planned)
                decision.terminal_state = ""
                decision.next_action = "ACQUIRE_EVIDENCE"
                decision.decision_reason = "blocker_directed_source_strategy_available"

    output.mkdir(parents=True, exist_ok=True)
    decision_rows = [d.as_dict() for d in decisions]
    for row in decision_rows:
        row["blocker_fields"] = ";".join(row["blocker_fields"])
        row["independent_evidence_fields"] = ";".join(row["independent_evidence_fields"])
        row["applicable_resolvers"] = ";".join(row["applicable_resolvers"])
        row["cached_no_progress_resolvers"] = ";".join(row["cached_no_progress_resolvers"])

    candidate_rows = []
    for acc in accessions:
        row = candidates.get(acc, {})
        candidate_rows.append({
            "accession": acc,
            "candidate_sha256": row.get("candidate_sha256", ""),
            "candidate_path": row.get("candidate_path", ""),
            "sha_verified": row.get("sha_verified", "false"),
            "provenance": row.get("provenance", row.get("source", "")),
        })
    write_tsv(
        output / "candidate_registry.tsv",
        candidate_rows,
        ["accession", "candidate_sha256", "candidate_path", "sha_verified", "provenance"],
    )

    state_fields = [
        "accession", "input_state", "reason_code", "blocker_family", "blocker_fields",
        "candidate_sha256", "evidence_set_sha256", "independent_evidence_count",
        "independent_evidence_fields", "applicable_resolvers", "cached_no_progress_resolvers", "next_action",
        "terminal_state", "decision_reason",
    ]
    resolver_by_id = {x.resolver_id: x for x in resolvers}
    resolver_plan_rows = []
    for decision in decisions:
        if decision.next_action != "RUN_RESOLVER":
            continue
        bkey = blocker_key(decision.blocker_family, decision.blocker_fields)
        for resolver_id in decision.applicable_resolvers:
            cap = resolver_by_id[resolver_id]
            resolver_plan_rows.append({
                "stage_key": stage_key(
                    accession=decision.accession,
                    candidate_sha256=decision.candidate_sha256,
                    evidence_set_sha256_value=decision.evidence_set_sha256,
                    resolver_id=cap.resolver_id,
                    resolver_version=cap.version,
                    policy_version=policy_version,
                    blocker_key_value=bkey,
                ),
                "accession": decision.accession,
                "resolver_id": cap.resolver_id,
                "resolver_version": cap.version,
                "policy_version": policy_version,
                "blocker_family": decision.blocker_family,
                "blocker_fields": ";".join(decision.blocker_fields),
                "blocker_key": bkey,
                "candidate_sha256": decision.candidate_sha256,
                "evidence_set_sha256": decision.evidence_set_sha256,
                "status": "pending",
            })
    write_tsv(
        output / "resolver_plan.tsv",
        resolver_plan_rows,
        [
            "stage_key", "accession", "resolver_id", "resolver_version", "policy_version",
            "blocker_family", "blocker_fields", "blocker_key", "candidate_sha256",
            "evidence_set_sha256", "status",
        ],
    )
    write_tsv(
        output / "evidence_acquisition_plan.tsv",
        acquisition_plan_rows,
        [
            "stage_key", "accession", "source_class", "priority", "provider",
            "acquisition_method", "expected_trust_class", "requires_external_locator",
            "blocker_family", "blocker_fields", "blocker_key", "evidence_set_sha256",
            "policy_version", "strategy_version", "status",
        ],
    )
    write_tsv(
        output / "evidence_acquisition_attempts.tsv",
        [vars(x) for x in acquisition_attempts],
        [
            "stage_key", "accession", "source_class", "status", "evidence_set_sha256",
            "policy_version", "blocker_key", "strategy_version",
        ],
    )
    # Refresh rows after acquisition planning mutates terminal/action classification.
    decision_rows = [d.as_dict() for d in decisions]
    for row in decision_rows:
        row["blocker_fields"] = ";".join(row["blocker_fields"])
        row["independent_evidence_fields"] = ";".join(row["independent_evidence_fields"])
        row["applicable_resolvers"] = ";".join(row["applicable_resolvers"])
        row["cached_no_progress_resolvers"] = ";".join(row["cached_no_progress_resolvers"])
    write_tsv(output / "state_ledger.tsv", decision_rows, state_fields)
    action_rows = [r for r in decision_rows if r["next_action"]]
    write_tsv(output / "action_queue.tsv", action_rows, state_fields)
    human_rows = [r for r in decision_rows if r["terminal_state"] == "HUMAN_REVIEW_ACTIONABLE"]
    write_tsv(output / "human_review_queue.tsv", human_rows, state_fields)
    limited_rows = [r for r in decision_rows if r["terminal_state"] == "EVIDENCE_LIMITED"]
    write_tsv(output / "evidence_limited.tsv", limited_rows, state_fields)
    ready_rows = [r for r in decision_rows if r["terminal_state"] == "SUBMISSION_READY"]
    write_tsv(output / "submission_ready.tsv", ready_rows, state_fields)
    write_tsv(
        output / "resolver_attempts.tsv",
        [vars(x) for x in attempts],
        ["stage_key", "accession", "resolver_id", "status", "candidate_sha256",
         "evidence_set_sha256", "policy_version", "blocker_key"],
    )

    impl_rows = [
        {
            "blocker_field": field,
            "accession_count": len(accs),
            "accessions": ";".join(accs),
            "threshold": threshold,
            "generic_change_authorized": "true",
        }
        for field, accs in sorted(implementation_candidates.items())
    ]
    write_tsv(
        output / "implementation_candidates.tsv",
        impl_rows,
        ["blocker_field", "accession_count", "accessions", "threshold", "generic_change_authorized"],
    )

    counts = Counter((d.terminal_state or d.next_action or "UNCLASSIFIED") for d in decisions)
    summary = {
        "version": VERSION,
        "run_spec_version": RUN_SPEC_VERSION,
        "run_id": str(spec.get("run_id") or spec_path.stem),
        **verified,
        "accessions": len(accessions),
        "candidate_records": len(candidates),
        "evidence_records": len(evidence),
        "attempt_records": len(attempts),
        "resolver_count": len(resolvers),
        "evidence_acquisition_enabled": acquisition_enabled,
        "evidence_source_catalog_version": source_catalog_version,
        "evidence_acquisition_plan_count": len(acquisition_plan_rows),
        "evidence_acquisition_attempt_count": len(acquisition_attempts),
        "generic_implementation_threshold": threshold,
        "generic_implementation_candidates": implementation_candidates,
        "decision_counts": dict(sorted(counts.items())),
        "outputs": {
            "candidate_registry": str(output / "candidate_registry.tsv"),
            "state_ledger": str(output / "state_ledger.tsv"),
            "resolver_attempts": str(output / "resolver_attempts.tsv"),
            "action_queue": str(output / "action_queue.tsv"),
            "resolver_plan": str(output / "resolver_plan.tsv"),
            "evidence_acquisition_plan": str(output / "evidence_acquisition_plan.tsv"),
            "evidence_acquisition_attempts": str(output / "evidence_acquisition_attempts.tsv"),
            "human_review_queue": str(output / "human_review_queue.tsv"),
            "implementation_candidates": str(output / "implementation_candidates.tsv"),
            "evidence_limited": str(output / "evidence_limited.tsv"),
            "submission_ready": str(output / "submission_ready.tsv"),
        },
    }
    (output / "run_manifest.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    md = [
        f"# PRIDE-SCP SDRF annotation harness — {summary['run_id']}",
        "",
        f"Harness: `{VERSION}`",
        f"Run spec SHA256: `{summary['run_spec_sha256']}`",
        f"Policy: `{policy_version}`",
        f"Accessions: **{len(accessions)}**",
        "",
        "## Decisions",
        "",
    ]
    for key, value in sorted(counts.items()):
        md.append(f"- **{key}**: {value}")
    md.extend(["", "## Generic implementation candidates", ""])
    if implementation_candidates:
        for field, accs in sorted(implementation_candidates.items()):
            md.append(f"- `{field}`: {len(accs)} accessions — {', '.join(accs)}")
    else:
        md.append("- None")
    md.extend([
        "",
        "## Operator surfaces",
        "",
        "Inspect `action_queue.tsv`, `resolver_plan.tsv`, `evidence_acquisition_plan.tsv`, `human_review_queue.tsv`, `implementation_candidates.tsv`, and `submission_ready.tsv`.",
        "The harness does not mutate SDRFs and does not itself confer submission readiness.",
        "",
    ])
    (output / "RUN_SUMMARY.md").write_text("\n".join(md), encoding="utf-8")
    return summary


def self_test() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "accessions.txt").write_text(
            "PXD900001\nPXD900002\nPXD900003\nPXD900004\nPXD900005\n",
            encoding="utf-8",
        )
        candidate_rows = [
            {"accession": f"PXD90000{i}", "candidate_sha256": str(i) * 64}
            for i in range(1, 6)
        ]
        write_tsv(root / "candidates.tsv", candidate_rows, ["accession", "candidate_sha256"])
        blockers = [
            {"accession": "PXD900001", "state": "blocked_metadata_incomplete", "reason_code": "single_cell_isolation_unresolved", "blocker_fields": "characteristics[single cell isolation protocol]"},
            {"accession": "PXD900002", "state": "blocked_metadata_incomplete", "reason_code": "single_cell_isolation_unresolved", "blocker_fields": "characteristics[single cell isolation protocol]"},
            {"accession": "PXD900003", "state": "blocked_metadata_incomplete", "reason_code": "single_cell_isolation_unresolved", "blocker_fields": "characteristics[single cell isolation protocol]"},
            {"accession": "PXD900004", "state": "blocked_bigbio_check", "reason_code": "cell identifier unresolved", "blocker_fields": "characteristics[cell identifier]"},
            {"accession": "PXD900005", "state": "provenance_conflict", "reason_code": "provenance_conflict", "blocker_fields": ""},
        ]
        write_tsv(root / "blockers.tsv", blockers, ["accession", "state", "reason_code", "blocker_fields"])
        evidence_rows = []
        for i in range(1, 4):
            evidence_rows.append({
                "accession": f"PXD90000{i}",
                "artifact_sha256": ("a" if i == 1 else "b" if i == 2 else "c") * 64,
                "blocker_field": "characteristics[single cell isolation protocol]",
                "trust_class": "trusted_independent",
                "is_independent": "true",
                "provenance_status": "independent_source_provenance_present",
            })
        evidence_rows.append({
            "accession": "PXD900004",
            "artifact_sha256": "d" * 64,
            "blocker_field": "characteristics[cell identifier]",
            "trust_class": "trusted_independent",
            "is_independent": "true",
            "provenance_status": "independent_source_provenance_present",
        })
        write_tsv(
            root / "evidence.tsv",
            evidence_rows,
            ["accession", "artifact_sha256", "blocker_field", "trust_class", "is_independent", "provenance_status"],
        )
        spec = {
            "schema_version": RUN_SPEC_VERSION,
            "run_id": "self-test",
            "generic_implementation_threshold": 3,
            "provenance": {"policy_version": "policy-test"},
            "inputs": {
                "accessions_file": "accessions.txt",
                "candidate_manifest": "candidates.tsv",
                "blocker_manifest": "blockers.tsv",
                "evidence_registry": "evidence.tsv",
            },
        }
        spec_path = root / "run_spec.json"
        spec_path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
        summary = plan(spec_path, root / "out")
        assert summary["decision_counts"]["IMPLEMENTATION_CANDIDATE"] == 3
        assert summary["decision_counts"]["RUN_RESOLVER"] == 1
        assert summary["decision_counts"]["PROVENANCE_CONFLICT"] == 1

        # Record no-progress for the exact PXD900004 content key and confirm a replay is terminal.
        ledger_rows = read_tsv(root / "out" / "state_ledger.tsv")
        row4 = next(r for r in ledger_rows if r["accession"] == "PXD900004")
        skey = stage_key(
            accession="PXD900004",
            candidate_sha256="4" * 64,
            evidence_set_sha256_value=row4["evidence_set_sha256"],
            resolver_id="structured_mapping_v2",
            resolver_version="pride-scp-structured-row-mapping-resolver-v2",
            policy_version="policy-test",
            blocker_key_value=blocker_key("cell_identifier", ["characteristics[cell identifier]"]),
        )
        write_tsv(
            root / "attempts.tsv",
            [{
                "stage_key": skey,
                "accession": "PXD900004",
                "resolver_id": "structured_mapping_v2",
                "status": "no_change",
                "candidate_sha256": "4" * 64,
                "evidence_set_sha256": row4["evidence_set_sha256"],
                "policy_version": "policy-test",
                "blocker_key": blocker_key("cell_identifier", ["characteristics[cell identifier]"]),
            }],
            ["stage_key", "accession", "resolver_id", "status", "candidate_sha256", "evidence_set_sha256", "policy_version", "blocker_key"],
        )
        spec["inputs"]["attempt_ledger"] = "attempts.tsv"
        spec_path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
        summary2 = plan(spec_path, root / "out2")
        assert summary2["decision_counts"]["EVIDENCE_LIMITED"] == 1
        assert summary2["decision_counts"]["IMPLEMENTATION_CANDIDATE"] == 3
        assert summary2["decision_counts"]["PROVENANCE_CONFLICT"] == 1
    print("sdrf_annotation_harness self-test: PASS")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-spec", type=Path)
    p.add_argument("--output", type=Path)
    p.add_argument("--ingest-source-manifest", type=Path, help="Provider source_manifest.tsv to ingest before automatically replanning")
    p.add_argument("--ingest-publication-manifest", action="append", type=Path, default=[], help="Cached publication manifest to register before automatically replanning; repeatable")
    p.add_argument("--self-test", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()
    if args.self_test:
        self_test()
        return 0
    if args.run_spec is None or args.output is None:
        raise SystemExit("--run-spec and --output are required unless --self-test")
    if args.ingest_source_manifest is not None and args.ingest_publication_manifest:
        raise SystemExit("choose provider ingestion or publication ingestion in one invocation")
    if args.ingest_source_manifest is not None:
        summary = ingest_provider_manifest_and_replan(
            args.run_spec, args.ingest_source_manifest.resolve(), args.output
        )
    elif args.ingest_publication_manifest:
        summary = ingest_publication_manifests_and_replan(
            args.run_spec, [path.resolve() for path in args.ingest_publication_manifest], args.output
        )
    else:
        summary = plan(args.run_spec, args.output)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
