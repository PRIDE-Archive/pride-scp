#!/usr/bin/env python3
"""Audit the frozen arch4 Scientific Workspace Agent v1.0 redesign run.

Evaluation only. This script reads post-run artifacts and never supplies runtime
labels, mappings, GT truth, or annotations to the scientific agent.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ARCH4 = ["PXD035339", "PXD046467", "PXD041388", "PXD025634"]
V04 = {
    "total_validation_errors": 212,
    "total_agent_turns": 36,
    "total_tool_actions": 36,
    "total_validator_cycles": 5,
}
STRUCTURAL_ERROR_CODES = {
    "cell_identifier_invalid_or_unresolved",
    "required_integer_invalid",
}
BIOLOGICAL_SCOPE_CONCEPTS = {
    "organism",
    "organism_part",
    "disease",
    "cell_type",
    "cell_line",
    "sample_type",
    "isolation_method",
}


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def claim_identity(claim: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(claim.get("concept_type", "")).strip().lower(),
        str(claim.get("scope", "")).strip().lower(),
        str(claim.get("branch_id", "")).strip(),
    )


def duplicate_claim_groups(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for claim in claims:
        groups[claim_identity(claim)].append(str(claim.get("value", "")))
    return [
        {"identity": list(identity), "values": values, "count": len(values)}
        for identity, values in sorted(groups.items())
        if len(values) > 1
    ]


def structural_errors(review_rows: list[dict[str, str]]) -> dict[str, int]:
    counts = Counter(
        row.get("code", "")
        for row in review_rows
        if row.get("level") == "error" and row.get("code") in STRUCTURAL_ERROR_CODES
    )
    return {code: counts.get(code, 0) for code in sorted(STRUCTURAL_ERROR_CODES)}


def adjudications_for(audit: dict[str, Any], concept: str) -> list[dict[str, Any]]:
    return [
        record
        for record in audit.get("claim_adjudications", [])
        if str(record.get("concept_type", "")).strip().lower() == concept
    ]


def branch_identity_matches(branch: dict[str, Any], needle: str) -> bool:
    # Intentionally exclude notes: v0.4 falsely classified one HeLa branch as
    # Xenopus merely because the notes discussed the Xenopus protocol.
    hay = " ".join(str(branch.get(key, "")) for key in ("id", "label")).lower()
    return needle.lower() in hay


def task_by_id(audit: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    for task in audit.get("tasks", []):
        if str(task.get("id", "")).strip() == task_id:
            return task
    return None


def isolation_task(audit: dict[str, Any]) -> dict[str, Any] | None:
    for task in audit.get("tasks", []):
        if str(task.get("concept_type", "")).strip().lower() == "isolation_method":
            return task
    return None


def candidate_evidence_details(
    evidence: dict[str, Any], task: dict[str, Any] | None
) -> list[dict[str, str]]:
    if not task:
        return []
    by_id = {str(item.get("id", "")): item for item in evidence.get("evidence", [])}
    out = []
    for ref in task.get("evidence_candidates", []):
        item = by_id.get(str(ref))
        if not item:
            continue
        text = str(item.get("text", ""))
        out.append(
            {
                "id": str(ref),
                "source_kind": str(item.get("source_kind", "")),
                "source_label": str(item.get("source_label", "")),
                "text_preview": text[:500],
            }
        )
    return out


def audit_one(root: Path, accession: str) -> dict[str, Any]:
    audit_path = root / "audit" / f"{accession}.scientific_agent.json"
    trace_path = root / "workspaces" / accession / "trace.json"
    review_path = root / "review" / f"{accession}.sdrf.review.tsv"
    evidence_path = root / "evidence" / f"{accession}.evidence.json"
    notebook_path = root / "workspaces" / accession / "NOTEBOOK.md"
    missing = [
        str(path)
        for path in (audit_path, trace_path, review_path, evidence_path, notebook_path)
        if not path.is_file()
    ]
    if missing:
        return {"accession": accession, "missing": missing}

    audit = read_json(audit_path)
    trace = read_json(trace_path)
    review = read_tsv(review_path)
    evidence = read_json(evidence_path)
    claims = audit.get("claims", [])
    branches = audit.get("branches", [])
    task = isolation_task(audit)
    study_task = task_by_id(audit, "task:study_structure")
    candidate_details = candidate_evidence_details(evidence, task)
    reads = [
        result
        for result in trace.get("evidence_action_results", [])
        if result.get("action") == "READ_EVIDENCE_CONTEXT"
    ]

    out: dict[str, Any] = {
        "accession": accession,
        "terminal_status": audit.get("terminal_status"),
        "locally_valid": audit.get("locally_valid"),
        "validation_errors": audit.get("validation_errors"),
        "turns": audit.get("turns_completed"),
        "tool_actions": audit.get("tool_actions_completed"),
        "validator_cycles": audit.get("validator_cycles_completed"),
        "duplicate_active_claim_groups": duplicate_claim_groups(claims),
        "structural_error_counts": structural_errors(review),
        "tasks": audit.get("tasks", []),
        "study_structure_task": study_task,
        "isolation_task": task,
        "isolation_candidate_evidence": candidate_details,
        "read_context_actions": reads,
        "notebook_path": str(notebook_path),
    }

    if accession == "PXD035339":
        out["hydrodynamic_candidate_surfaced"] = any(
            "hydrodynamic" in row["text_preview"].lower()
            or "on-capillary" in row["text_preview"].lower()
            or "on capillary" in row["text_preview"].lower()
            for row in candidate_details
        )
        out["isolation_claims_final"] = [
            claim
            for claim in claims
            if str(claim.get("concept_type", "")).strip().lower() == "isolation_method"
        ]
        out["isolation_adjudications_final"] = adjudications_for(audit, "isolation_method")

    if accession == "PXD025634":
        iso = adjudications_for(audit, "isolation_method")
        out["isolation_adjudications_final"] = iso
        out["microwell_template_gap_retained"] = any(
            record.get("adjudication", {}).get("outcome") == "template_gap"
            and record.get("adjudication", {}).get("observed_value")
            == "microwell-chip single-cell transfer"
            for record in iso
        )

    if accession == "PXD046467":
        hela = [branch for branch in branches if branch_identity_matches(branch, "hela")]
        xenopus = [branch for branch in branches if branch_identity_matches(branch, "xenopus")]
        hela_ids = {str(branch.get("id", "")) for branch in hela}
        xenopus_ids = {str(branch.get("id", "")) for branch in xenopus}
        out["hela_branches"] = hela
        out["xenopus_branches"] = xenopus
        out["distinct_hela_and_xenopus_branches"] = bool(
            hela_ids and xenopus_ids and hela_ids.isdisjoint(xenopus_ids)
        )
        relevant = hela + xenopus
        out["sentinel_branches_unresolved_raw_linkage"] = bool(relevant) and all(
            branch.get("linkage_status") == "unresolved"
            and not branch.get("linked_raw_files", [])
            for branch in relevant
        )
        out["project_wide_biological_claims"] = [
            claim
            for claim in claims
            if claim_identity(claim)[1] == "project"
            and claim_identity(claim)[0] in BIOLOGICAL_SCOPE_CONCEPTS
            and str(claim.get("value", "")).strip()
        ]

    return out


def summarize_gates(summary: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_acc = {row["accession"]: row for row in rows if "missing" not in row}
    structural = Counter()
    duplicates = 0
    for row in by_acc.values():
        structural.update(row.get("structural_error_counts", {}))
        duplicates += len(row.get("duplicate_active_claim_groups", []))

    p35 = by_acc.get("PXD035339", {})
    p25 = by_acc.get("PXD025634", {})
    p46 = by_acc.get("PXD046467", {})
    p35_adj = p35.get("isolation_adjudications_final", [])
    return {
        "all_accessions_have_study_structure_task": all(
            bool(row.get("study_structure_task")) for row in by_acc.values()
        ) and len(by_acc) == len(ARCH4),
        "p46467_study_structure_status": str(
            p46.get("study_structure_task", {}).get("status", "")
        ),
        "p35339_hydrodynamic_candidate_surfaced": p35.get(
            "hydrodynamic_candidate_surfaced", False
        ),
        "p35339_context_reader_used": bool(p35.get("read_context_actions", [])),
        "p35339_isolation_has_explicit_rust_adjudication": any(
            record.get("adjudication", {}).get("outcome")
            in {"canonical", "template_gap", "unresolved", "conflict"}
            for record in p35_adj
        ),
        "p35339_isolation_canonical_manual_picking": any(
            record.get("adjudication", {}).get("outcome") == "canonical"
            and record.get("adjudication", {}).get("value") == "manual picking"
            for record in p35_adj
        ),
        "p25634_microwell_template_gap_retained": p25.get(
            "microwell_template_gap_retained", False
        ),
        "p46467_distinct_hela_and_xenopus_branches": p46.get(
            "distinct_hela_and_xenopus_branches", False
        ),
        "p46467_unresolved_raw_linkage": p46.get(
            "sentinel_branches_unresolved_raw_linkage", False
        ),
        "p46467_no_project_wide_biological_collapse": not p46.get(
            "project_wide_biological_claims", []
        ),
        "cell_identifier_invalid_or_unresolved": structural.get(
            "cell_identifier_invalid_or_unresolved", 0
        ),
        "required_integer_invalid": structural.get("required_integer_invalid", 0),
        "duplicate_active_claim_groups": duplicates,
        "validation_errors_below_v04": int(summary.get("total_validation_errors", 10**9))
        < V04["total_validation_errors"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()

    summary = read_json(args.output / "scientific_agent_summary.json")
    results = read_tsv(args.output / "scientific_agent_results.tsv")
    per_accession = [audit_one(args.output, accession) for accession in ARCH4]
    comparison = {
        key: {
            "v0.4": old,
            "v1.0": int(summary.get(key, 0)),
            "delta_v10_minus_v04": int(summary.get(key, 0)) - old,
        }
        for key, old in V04.items()
    }
    report = {
        "run_output": str(args.output),
        "frozen_arch4": ARCH4,
        "summary": summary,
        "result_rows": results,
        "comparison_to_v04": comparison,
        "decision_observables": summarize_gates(summary, per_accession),
        "per_accession": per_accession,
        "interpretation_rule": (
            "v1.0 is a redesign experiment, not a validator-green tuning pass. Primary success is "
            "that task-focused retrieval/read tools surface and adjudicate scientifically useful "
            "evidence while preserving fail-closed safety. If the evidence is surfaced but Qwen "
            "still cannot form the claim, run the same v1.0 workspace once with a stronger model "
            "as a capacity diagnostic before changing architecture again."
        ),
    }

    print("===== SCIENTIFIC WORKSPACE AGENT v1.0 ARCH4 =====")
    print(json.dumps(summary, indent=2, sort_keys=True))
    print("\n===== RESULT TABLE =====")
    if results:
        cols = [
            "accession",
            "status",
            "terminal_status",
            "turns",
            "tool_actions",
            "validator_cycles",
            "locally_valid",
            "validation_errors",
        ]
        print("\t".join(cols))
        for row in results:
            print("\t".join(str(row.get(col, "")) for col in cols))
    print("\n===== v0.4 -> v1.0 =====")
    for key, values in comparison.items():
        print(
            f"{key}\tv0.4={values['v0.4']}\tv1.0={values['v1.0']}"
            f"\tdelta={values['delta_v10_minus_v04']:+d}"
        )
    print("\n===== REDESIGN OBSERVABLES =====")
    print(json.dumps(report["decision_observables"], indent=2, sort_keys=True))
    print("\n===== SENTINEL DETAILS =====")
    print(json.dumps(per_accession, indent=2, sort_keys=True))

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"\nfull_audit_json={args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
