#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

ARCH4 = ["PXD035339", "PXD046467", "PXD041388", "PXD025634"]


def load_json(path: Path):
    return json.loads(path.read_text())


def blob(branch: dict) -> str:
    return " ".join(
        str(branch.get(key, ""))
        for key in (
            "label",
            "biological_material",
            "experimental_role",
            "isolation_context",
            "acquisition_context",
            "notes",
        )
    ).lower()


def has_term(branches: list[dict], *terms: str) -> bool:
    return any(all(term.lower() in blob(branch) for term in terms) for branch in branches)


def summarize_accession(output: Path, accession: str) -> dict:
    root = output / "study_graphs" / accession
    accepted_path = root / "accepted_graph.json"
    proposal_path = root / "proposal.json"
    if not accepted_path.is_file():
        return {
            "accession": accession,
            "present": False,
            "status": "missing",
            "branches": [],
            "model_calls": 0,
            "removed_raw_links": [],
            "proposal_decision": "",
        }
    accepted = load_json(accepted_path)
    proposal = load_json(proposal_path) if proposal_path.is_file() else {}
    return {
        "accession": accession,
        "present": True,
        "status": accepted.get("status", ""),
        "branches": accepted.get("branches", []),
        "branch_count": len(accepted.get("branches", [])),
        "open_questions": accepted.get("open_questions", []),
        "rejected_branches": accepted.get("rejected_branches", []),
        "removed_raw_links": accepted.get("removed_raw_links", []),
        "model_calls": accepted.get("model_calls", 0),
        "reason": accepted.get("reason", ""),
        "proposal_decision": proposal.get("decision", ""),
        "proposal_reason": proposal.get("reason", ""),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--json-out", required=True, type=Path)
    args = ap.parse_args()

    output = args.output
    summary_path = output / "study_graph_stage1_summary.json"
    if not summary_path.is_file():
        raise SystemExit(f"missing StudyGraph Stage-1 summary: {summary_path}")
    summary = load_json(summary_path)
    details = [summarize_accession(output, accession) for accession in ARCH4]
    by_acc = {row["accession"]: row for row in details}

    p35339 = by_acc["PXD035339"]
    p46467 = by_acc["PXD046467"]
    p41388 = by_acc["PXD041388"]
    p25634 = by_acc["PXD025634"]

    p35339_branches = p35339.get("branches", [])
    p46467_branches = p46467.get("branches", [])

    p35339_hydrodynamic = any(
        ("hydrodynamic" in blob(branch) or "manual" in blob(branch))
        and ("single" in blob(branch) or "hela" in blob(branch))
        for branch in p35339_branches
    )
    p35339_spray = any(
        "spray" in blob(branch)
        and ("voltage" in blob(branch) or "low-input" in blob(branch) or "low input" in blob(branch))
        for branch in p35339_branches
    )
    p35339_distinct = (
        p35339.get("status") == "accepted"
        and len(p35339_branches) >= 2
        and p35339_hydrodynamic
        and p35339_spray
    )

    p46467_hela = any("hela" in blob(branch) for branch in p46467_branches)
    p46467_xenopus = any("xenopus" in blob(branch) or "laevis" in blob(branch) for branch in p46467_branches)
    p46467_distinct = (
        p46467.get("status") == "accepted"
        and len(p46467_branches) >= 2
        and p46467_hela
        and p46467_xenopus
    )

    accepted_links = [
        (row["accession"], branch.get("id", ""), raw, branch.get("linkage_status", ""))
        for row in details
        for branch in row.get("branches", [])
        for raw in branch.get("linked_raw_files", [])
    ]
    linkage_statuses_known = all(
        branch.get("linkage_status") in {"supported", "partial", "unresolved"}
        for row in details
        for branch in row.get("branches", [])
    )
    unresolved_without_raw_is_allowed = all(
        branch.get("linkage_status") == "unresolved"
        for row in details
        for branch in row.get("branches", [])
        if not branch.get("linked_raw_files", [])
    )

    one_model_call_each = all(row.get("model_calls") == 1 for row in details if row.get("present")) and all(
        row.get("present") for row in details
    )
    no_downstream_loop = (
        summary.get("total_agent_turns") == 4
        and summary.get("total_tool_actions") == 0
        and summary.get("total_validator_cycles") == 0
    )
    safety_gate = linkage_statuses_known and unresolved_without_raw_is_allowed
    stage1_progress_gate = (
        summary.get("harness_version") == "pride-scp-scientific-workspace-agent-v2-stage1"
        and summary.get("successful") == 4
        and one_model_call_each
        and no_downstream_loop
        and safety_gate
        and p35339_distinct
        and p46467_distinct
    )

    audit = {
        "harness_version": summary.get("harness_version"),
        "summary": summary,
        "decision_observables": {
            "all_arch4_outputs_present": all(row.get("present") for row in details),
            "one_model_call_per_accession": one_model_call_each,
            "no_downstream_annotation_or_validator_loop": no_downstream_loop,
            "linkage_statuses_known": linkage_statuses_known,
            "unresolved_branch_without_raw_files_is_accepted_state": unresolved_without_raw_is_allowed,
            "accepted_exact_raw_links": accepted_links,
            "raw_links_removed_by_rust": sum(len(row.get("removed_raw_links", [])) for row in details),
            "p35339_status": p35339.get("status"),
            "p35339_distinct_hydrodynamic_and_spray_branches": p35339_distinct,
            "p35339_hydrodynamic_branch_present": p35339_hydrodynamic,
            "p35339_spray_branch_present": p35339_spray,
            "p46467_status": p46467.get("status"),
            "p46467_distinct_hela_and_xenopus_branches": p46467_distinct,
            "p46467_hela_branch_present": p46467_hela,
            "p46467_xenopus_branch_present": p46467_xenopus,
            "p41388_status": p41388.get("status"),
            "p25634_status": p25634.get("status"),
            "stage1_safety_gate_passed": safety_gate,
            "stage1_progress_gate_passed": stage1_progress_gate,
            "stop_before_phase_b": not stage1_progress_gate,
        },
        "sentinel_details": details,
    }
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n")

    print("===== SCIENTIFIC WORKSPACE AGENT v2 STAGE 1 — STUDYGRAPH ARCH4 =====")
    print(json.dumps(summary, indent=2, sort_keys=True))
    print("\n===== STAGE-1 DECISION GATES =====")
    print(json.dumps(audit["decision_observables"], indent=2, sort_keys=True))
    print("\n===== SENTINEL DETAILS =====")
    print(json.dumps(details, indent=2, sort_keys=True))
    print(f"\nfull_audit_json={args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
