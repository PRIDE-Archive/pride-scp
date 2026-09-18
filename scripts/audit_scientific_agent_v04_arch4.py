#!/usr/bin/env python3
"""Audit the frozen PRIDE-SCP Scientific Agent v0.4 arch4 decision run.

This is evaluation-only. It never supplies labels, mappings, or annotations to the
runtime agent. It inspects artifacts produced after the run and reports the v0.4
decision-gate observables against the frozen v0.3 aggregate reference.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ARCH4 = ["PXD035339", "PXD046467", "PXD041388", "PXD025634"]
V03 = {
    "total_validation_errors": 212,
    "total_agent_turns": 35,
    "total_tool_actions": 32,
    "total_validator_cycles": 6,
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
        {
            "identity": list(identity),
            "values": values,
            "count": len(values),
        }
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


def isolation_persistence(trace: dict[str, Any]) -> dict[str, Any]:
    states = trace.get("states", [])
    deltas_by_turn = {
        int(delta.get("turn", 0)): delta for delta in trace.get("deltas", []) if delta.get("turn")
    }
    observations: list[dict[str, Any]] = []
    survived_without_reemit = False
    first_seen: dict[tuple[str, str, str, str], int] = {}

    for state in states:
        turn = int(state.get("turn", 0))
        delta = deltas_by_turn.get(turn, {})
        upserts = delta.get("claim_upserts", []) if isinstance(delta, dict) else []
        upsert_keys = {
            (*claim_identity(upsert), str(upsert.get("value", "")).strip().lower())
            for upsert in upserts
            if str(upsert.get("concept_type", "")).strip().lower() == "isolation_method"
        }
        isolation_claims = [
            claim
            for claim in state.get("claims", [])
            if str(claim.get("concept_type", "")).strip().lower() == "isolation_method"
        ]
        for claim in isolation_claims:
            key = (*claim_identity(claim), str(claim.get("value", "")).strip().lower())
            first = first_seen.setdefault(key, turn)
            reemitted = key in upsert_keys
            if turn > first and not reemitted:
                survived_without_reemit = True
            observations.append(
                {
                    "turn": turn,
                    "identity": list(key[:3]),
                    "value": claim.get("value", ""),
                    "status": claim.get("status", ""),
                    "evidence_refs": claim.get("evidence_refs", []),
                    "reemitted_same_value_this_turn": reemitted,
                    "first_seen_turn": first,
                }
            )

    return {
        "survived_at_least_one_later_turn_without_same_value_reemit": survived_without_reemit,
        "observations": observations,
    }


def adjudications_for(audit: dict[str, Any], concept: str) -> list[dict[str, Any]]:
    return [
        record
        for record in audit.get("claim_adjudications", [])
        if str(record.get("concept_type", "")).strip().lower() == concept
    ]


def branch_matches(branch: dict[str, Any], needle: str) -> bool:
    hay = " ".join(
        str(branch.get(key, "")) for key in ("id", "label", "notes")
    ).lower()
    return needle.lower() in hay


def audit_one(root: Path, accession: str) -> dict[str, Any]:
    audit_path = root / "audit" / f"{accession}.scientific_agent.json"
    trace_path = root / "workspaces" / accession / "trace.json"
    review_path = root / "review" / f"{accession}.sdrf.review.tsv"
    missing = [str(path) for path in (audit_path, trace_path, review_path) if not path.is_file()]
    if missing:
        return {"accession": accession, "missing": missing}

    audit = read_json(audit_path)
    trace = read_json(trace_path)
    review = read_tsv(review_path)
    claims = audit.get("claims", [])
    branches = audit.get("branches", [])

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
    }

    if accession == "PXD035339":
        out["isolation_persistence"] = isolation_persistence(trace)
        out["isolation_claims_final"] = [
            claim
            for claim in claims
            if str(claim.get("concept_type", "")).strip().lower() == "isolation_method"
        ]
        out["isolation_adjudications_final"] = adjudications_for(audit, "isolation_method")

    if accession == "PXD025634":
        iso = adjudications_for(audit, "isolation_method")
        template_gaps = [
            record
            for record in iso
            if record.get("adjudication", {}).get("outcome") == "template_gap"
        ]
        out["isolation_adjudications_final"] = iso
        out["microwell_template_gap_retained"] = any(
            record.get("adjudication", {}).get("observed_value")
            == "microwell-chip single-cell transfer"
            for record in template_gaps
        )

    if accession == "PXD046467":
        hela = [branch for branch in branches if branch_matches(branch, "hela")]
        xenopus = [branch for branch in branches if branch_matches(branch, "xenopus")]
        out["hela_branches"] = hela
        out["xenopus_branches"] = xenopus
        out["hela_and_xenopus_present"] = bool(hela and xenopus)
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
        out["branch_scope_mask_warnings"] = [
            row
            for row in review
            if row.get("code") == "scientific_agent_branch_scope_masks_project_value"
        ]

    return out


def summarize_gates(summary: dict[str, Any], per_accession: list[dict[str, Any]]) -> dict[str, Any]:
    by_acc = {row["accession"]: row for row in per_accession if "missing" not in row}
    structural = Counter()
    duplicate_groups = 0
    for row in by_acc.values():
        structural.update(row.get("structural_error_counts", {}))
        duplicate_groups += len(row.get("duplicate_active_claim_groups", []))

    p35339 = by_acc.get("PXD035339", {})
    p25634 = by_acc.get("PXD025634", {})
    p46467 = by_acc.get("PXD046467", {})
    p35339_adjudications = p35339.get("isolation_adjudications_final", [])
    p35339_explicit_outcome = any(
        record.get("adjudication", {}).get("outcome")
        in {"canonical", "template_gap", "unresolved", "conflict"}
        for record in p35339_adjudications
    )
    p35339_manual_picking = any(
        record.get("adjudication", {}).get("outcome") == "canonical"
        and record.get("adjudication", {}).get("value") == "manual picking"
        for record in p35339_adjudications
    )

    return {
        "p35339_isolation_survives_without_reemit": p35339.get("isolation_persistence", {}).get(
            "survived_at_least_one_later_turn_without_same_value_reemit", False
        ),
        "p35339_isolation_has_explicit_rust_adjudication": p35339_explicit_outcome,
        "p35339_isolation_canonical_manual_picking": p35339_manual_picking,
        "p25634_microwell_template_gap_retained": p25634.get(
            "microwell_template_gap_retained", False
        ),
        "p46467_hela_and_xenopus_present": p46467.get("hela_and_xenopus_present", False),
        "p46467_sentinel_branches_unresolved_raw_linkage": p46467.get(
            "sentinel_branches_unresolved_raw_linkage", False
        ),
        "p46467_no_unmasked_project_wide_biological_collapse": (
            not p46467.get("project_wide_biological_claims", [])
            or bool(p46467.get("branch_scope_mask_warnings", []))
        ),
        "cell_identifier_invalid_or_unresolved": structural.get(
            "cell_identifier_invalid_or_unresolved", 0
        ),
        "required_integer_invalid": structural.get("required_integer_invalid", 0),
        "duplicate_active_claim_groups": duplicate_groups,
        "turns_under_30": int(summary.get("total_agent_turns", 10**9)) < 30,
        "tool_actions_under_25": int(summary.get("total_tool_actions", 10**9)) < 25,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path, help="Scientific-agent run output root")
    parser.add_argument("--json-out", type=Path, help="Optional path for the complete audit report")
    args = parser.parse_args()

    summary_path = args.output / "scientific_agent_summary.json"
    results_path = args.output / "scientific_agent_results.tsv"
    if not summary_path.is_file() or not results_path.is_file():
        raise SystemExit(
            f"missing scientific-agent summary/results under {args.output}: "
            f"{summary_path.name}, {results_path.name}"
        )

    summary = read_json(summary_path)
    results = read_tsv(results_path)
    per_accession = [audit_one(args.output, accession) for accession in ARCH4]
    comparison = {
        key: {
            "v0.3": old,
            "v0.4": int(summary.get(key, 0)),
            "delta_v04_minus_v03": int(summary.get(key, 0)) - old,
        }
        for key, old in V03.items()
    }
    report = {
        "run_output": str(args.output),
        "frozen_arch4": ARCH4,
        "summary": summary,
        "result_rows": results,
        "per_accession": per_accession,
        "comparison_to_v03": comparison,
        "decision_gate_observables": summarize_gates(summary, per_accession),
        "stop_rule": (
            "If v0.4 is flat or only marginally better, stop incremental harness tuning and "
            "perform the mandatory redesign review; do not implement a small v0.5 patch."
        ),
    }

    print("===== v0.4 ARCH4 SUMMARY =====")
    print(json.dumps(summary, indent=2, sort_keys=True))
    print("\n===== RESULT TABLE =====")
    fields = [
        "accession",
        "status",
        "terminal_status",
        "turns",
        "tool_actions",
        "validator_cycles",
        "locally_valid",
        "validation_errors",
    ]
    print("\t".join(fields))
    for row in results:
        print("\t".join(row.get(field, "") for field in fields))
    print("\n===== v0.3 -> v0.4 AGGREGATE COMPARISON =====")
    for key, values in comparison.items():
        print(
            f"{key}\tv0.3={values['v0.3']}\tv0.4={values['v0.4']}\t"
            f"delta={values['delta_v04_minus_v03']:+d}"
        )
    print("\n===== DECISION-GATE OBSERVABLES =====")
    print(json.dumps(report["decision_gate_observables"], indent=2, sort_keys=True))
    print("\n===== SENTINEL DETAILS =====")
    print(json.dumps(per_accession, indent=2, sort_keys=True))

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"\nfull_audit_json={args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
