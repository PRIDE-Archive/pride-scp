#!/usr/bin/env python3
"""Audit the frozen arch4 Scientific Workspace Agent v1.3 typed-edit-affordance run.

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
V12_QWEN36 = {
    "total_validation_errors": 212,
    "total_agent_turns": 16,
    "total_tool_actions": 10,
    "total_validator_cycles": 4,
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
COMMANDS = {
    "read_evidence",
    "search_evidence",
    "edit_study_structure",
    "edit_scientific_observation",
    "escalate",
}


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def observation_value(observation: dict[str, Any]) -> str:
    return str(observation.get("observed_value", observation.get("value", "")))


def observation_identity(observation: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(observation.get("concept_type", "")).strip().lower(),
        str(observation.get("scope", "")).strip().lower(),
        str(observation.get("branch_id", "")).strip(),
    )


def duplicate_observation_groups(
    observations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for observation in observations:
        groups[observation_identity(observation)].append(observation_value(observation))
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
    records = audit.get("observation_adjudications", audit.get("claim_adjudications", []))
    return [
        record
        for record in records
        if str(record.get("concept_type", "")).strip().lower() == concept
    ]


def branch_identity_matches(branch: dict[str, Any], needle: str) -> bool:
    # Deliberately exclude notes. A prior audit falsely counted another organism
    # merely because one branch's prose mentioned it.
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
    out: list[dict[str, str]] = []
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


def command_counts(trace: dict[str, Any]) -> dict[str, int]:
    counts = Counter(str(command.get("command", "")) for command in trace.get("commands", []))
    return {command: counts.get(command, 0) for command in sorted(COMMANDS)}


def max_consecutive_command(trace: dict[str, Any], command_name: str) -> int:
    best = 0
    current = 0
    for command in trace.get("commands", []):
        if str(command.get("command", "")) == command_name:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def duplicate_skipped_actions(trace: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        result
        for result in trace.get("evidence_action_results", [])
        if result.get("outcome") == "duplicate_skipped"
    ]


def executed_evidence_actions(trace: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        result
        for result in trace.get("evidence_action_results", [])
        if result.get("action")
    ]


def read_or_search_actions(trace: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        result
        for result in executed_evidence_actions(trace)
        if result.get("outcome") != "duplicate_skipped"
        and (
            result.get("action") == "READ_EVIDENCE_CONTEXT"
            or str(result.get("action", "")).startswith("SEARCH_")
            or result.get("action")
            in {"EXPAND_EVIDENCE_CONTEXT", "LOOKUP_KG_TERM", "COMPARE_CONFLICTING_EVIDENCE"}
        )
    ]


def has_manual_picking_canonical(records: list[dict[str, Any]]) -> bool:
    return any(
        record.get("adjudication", {}).get("outcome") == "canonical"
        and record.get("adjudication", {}).get("value") == "manual picking"
        for record in records
    )


def has_wording_only_conflict(records: list[dict[str, Any]]) -> bool:
    for record in records:
        adj = record.get("adjudication", {})
        if adj.get("outcome") != "conflict":
            continue
        reason = str(adj.get("reason", "")).lower()
        if "manual_picking" in reason or "hydrodynamic_loading" in reason:
            return True
        if "stable claim identity" in reason and "hydrodynamic" in reason:
            return True
    return False


def audit_one(root: Path, accession: str) -> dict[str, Any]:
    audit_path = root / "audit" / f"{accession}.scientific_agent.json"
    trace_path = root / "workspaces" / accession / "trace.json"
    review_path = root / "review" / f"{accession}.sdrf.review.tsv"
    evidence_path = root / "evidence" / f"{accession}.evidence.json"
    notebook_path = root / "workspaces" / accession / "NOTEBOOK.md"
    command_history_path = root / "workspaces" / accession / "command_history.json"
    missing = [
        str(path)
        for path in (
            audit_path,
            trace_path,
            review_path,
            evidence_path,
            notebook_path,
            command_history_path,
        )
        if not path.is_file()
    ]
    if missing:
        return {"accession": accession, "missing": missing}

    audit = read_json(audit_path)
    trace = read_json(trace_path)
    review = read_tsv(review_path)
    evidence = read_json(evidence_path)
    observations = audit.get("scientific_observations", audit.get("claims", []))
    branches = audit.get("branches", [])
    task = isolation_task(audit)
    study_task = task_by_id(audit, "task:study_structure")
    candidate_details = candidate_evidence_details(evidence, task)
    actions = executed_evidence_actions(trace)
    reads = [result for result in actions if result.get("action") == "READ_EVIDENCE_CONTEXT"]
    commands = trace.get("commands", [])
    invalid_commands = [
        command for command in commands if str(command.get("command", "")) not in COMMANDS
    ]

    out: dict[str, Any] = {
        "accession": accession,
        "terminal_status": audit.get("terminal_status"),
        "locally_valid": audit.get("locally_valid"),
        "validation_errors": audit.get("validation_errors"),
        "turns": audit.get("turns_completed"),
        "tool_actions": audit.get("tool_actions_completed"),
        "validator_cycles": audit.get("validator_cycles_completed"),
        "command_counts": command_counts(trace),
        "commands": commands,
        "invalid_commands": invalid_commands,
        "max_consecutive_read_commands": max_consecutive_command(trace, "read_evidence"),
        "duplicate_skipped_actions": duplicate_skipped_actions(trace),
        "executed_evidence_actions": actions,
        "read_context_actions": reads,
        "read_or_search_action_count": len(read_or_search_actions(trace)),
        "duplicate_active_observation_groups": duplicate_observation_groups(observations),
        "structural_error_counts": structural_errors(review),
        "tasks": audit.get("tasks", []),
        "study_structure_task": study_task,
        "study_structure_attempts": int((study_task or {}).get("attempts", 0)),
        "isolation_task": task,
        "isolation_task_attempts": int((task or {}).get("attempts", 0)),
        "isolation_candidate_evidence": candidate_details,
        "notebook_path": str(notebook_path),
        "branches": branches,
        "isolation_observations_final": [
            observation
            for observation in observations
            if str(observation.get("concept_type", "")).strip().lower() == "isolation_method"
        ],
    }

    if accession == "PXD035339":
        out["hydrodynamic_candidate_surfaced"] = any(
            "hydrodynamic" in row["text_preview"].lower()
            or "on-capillary" in row["text_preview"].lower()
            or "on capillary" in row["text_preview"].lower()
            for row in candidate_details
        )
        adjudications = adjudications_for(audit, "isolation_method")
        out["isolation_adjudications_final"] = adjudications
        out["canonical_manual_picking"] = has_manual_picking_canonical(adjudications)
        out["wording_only_conflict_present"] = has_wording_only_conflict(adjudications)

    if accession == "PXD025634":
        iso = adjudications_for(audit, "isolation_method")
        out["isolation_adjudications_final"] = iso
        out["microwell_template_gap_retained"] = any(
            record.get("adjudication", {}).get("outcome") == "template_gap"
            and "microwell" in str(
                record.get("adjudication", {}).get(
                    "observed_value", record.get("observed_value", "")
                )
            ).lower()
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
        out["project_wide_biological_observations"] = [
            observation
            for observation in observations
            if observation_identity(observation)[1] == "project"
            and observation_identity(observation)[0] in BIOLOGICAL_SCOPE_CONCEPTS
            and observation_value(observation).strip()
        ]

    if accession == "PXD041388":
        out["isolation_human_review"] = bool(task and task.get("status") == "human_review")
        out["evdisco_observation_recorded"] = any(
            "evdisco" in observation_value(observation).lower()
            or "digital microfluidic isolation" in observation_value(observation).lower()
            for observation in out.get("isolation_observations_final", [])
        )

    return out


def summarize_gates(summary: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_acc = {row["accession"]: row for row in rows if "missing" not in row}
    structural = Counter()
    duplicates = 0
    total_read_search = 0
    invalid_commands = 0
    total_command_reads = 0
    total_command_searches = 0
    total_study_structure_edits = 0
    total_scientific_observation_edits = 0
    total_command_escalations = 0
    duplicate_skipped = 0
    max_consecutive_reads = 0
    for row in by_acc.values():
        structural.update(row.get("structural_error_counts", {}))
        duplicates += len(row.get("duplicate_active_observation_groups", []))
        total_read_search += int(row.get("read_or_search_action_count", 0))
        invalid_commands += len(row.get("invalid_commands", []))
        counts = row.get("command_counts", {})
        total_command_reads += int(counts.get("read_evidence", 0))
        total_command_searches += int(counts.get("search_evidence", 0))
        total_study_structure_edits += int(counts.get("edit_study_structure", 0))
        total_scientific_observation_edits += int(
            counts.get("edit_scientific_observation", 0)
        )
        total_command_escalations += int(counts.get("escalate", 0))
        duplicate_skipped += len(row.get("duplicate_skipped_actions", []))
        max_consecutive_reads = max(
            max_consecutive_reads, int(row.get("max_consecutive_read_commands", 0))
        )

    p35 = by_acc.get("PXD035339", {})
    p25 = by_acc.get("PXD025634", {})
    p46 = by_acc.get("PXD046467", {})
    p41 = by_acc.get("PXD041388", {})
    p35_adj = p35.get("isolation_adjudications_final", [])
    return {
        "all_accessions_have_study_structure_task": all(
            bool(row.get("study_structure_task")) for row in by_acc.values()
        )
        and len(by_acc) == len(ARCH4),
        "only_known_single_commands_emitted": invalid_commands == 0,
        "read_evidence_commands": total_command_reads,
        "search_evidence_commands": total_command_searches,
        "study_structure_edit_commands": total_study_structure_edits,
        "scientific_observation_edit_commands": total_scientific_observation_edits,
        "typed_edit_commands": total_study_structure_edits
        + total_scientific_observation_edits,
        "escalate_commands": total_command_escalations,
        "executed_read_or_search_actions": total_read_search,
        "real_evidence_tool_protocol_exercised": total_read_search > 0,
        "duplicate_skipped_action_results": duplicate_skipped,
        "max_consecutive_read_commands": max_consecutive_reads,
        "finite_state_read_decision_gate_held": max_consecutive_reads <= 1,
        "at_least_one_edit_or_escalation": (
            total_study_structure_edits
            + total_scientific_observation_edits
            + total_command_escalations
        )
        > 0,
        "all_accessions_left_study_structure_or_attempted_isolation": all(
            int(row.get("isolation_task_attempts", 0)) > 0
            or str((row.get("study_structure_task") or {}).get("status", "")) in {"resolved", "human_review"}
            for row in by_acc.values()
        ) and len(by_acc) == len(ARCH4),
        "p35339_hydrodynamic_candidate_surfaced": p35.get(
            "hydrodynamic_candidate_surfaced", False
        ),
        "p35339_context_reader_used": bool(p35.get("read_context_actions", [])),
        "p35339_isolation_has_explicit_rust_adjudication": any(
            record.get("adjudication", {}).get("outcome")
            in {"canonical", "template_gap", "unresolved", "conflict"}
            for record in p35_adj
        ),
        "p35339_isolation_canonical_manual_picking": p35.get(
            "canonical_manual_picking", False
        ),
        "p35339_no_observation_vs_vocabulary_wording_conflict": not p35.get(
            "wording_only_conflict_present", False
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
            "project_wide_biological_observations", []
        ),
        "p41388_human_review_is_allowed": p41.get("isolation_human_review", False),
        "cell_identifier_invalid_or_unresolved": structural.get(
            "cell_identifier_invalid_or_unresolved", 0
        ),
        "required_integer_invalid": structural.get("required_integer_invalid", 0),
        "duplicate_active_observation_groups": duplicates,
        "validation_errors_below_qwen36_v12": int(
            summary.get("total_validation_errors", 10**9)
        )
        < V12_QWEN36["total_validation_errors"],
        "typed_edit_progress_gate": (
            total_study_structure_edits > 0
            and total_scientific_observation_edits > 0
        ),
        "sentinel_scientific_state_progress": bool(
            (p35.get("isolation_observations_final", []) and p35_adj)
            or p46.get("distinct_hela_and_xenopus_branches", False)
            or p41.get("evdisco_observation_recorded", False)
            or p25.get("isolation_observations_final", [])
        ),
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
            "qwen3.6_v1.2": old,
            "qwen3.6_v1.3": int(summary.get(key, 0)),
            "delta_v13_minus_v12": int(summary.get(key, 0)) - old,
        }
        for key, old in V12_QWEN36.items()
    }
    gates = summarize_gates(summary, per_accession)
    safety_gate = (
        gates["cell_identifier_invalid_or_unresolved"] == 0
        and gates["required_integer_invalid"] == 0
        and gates["duplicate_active_observation_groups"] == 0
        and gates["p25634_microwell_template_gap_retained"]
        and gates["p46467_no_project_wide_biological_collapse"]
        and gates["finite_state_read_decision_gate_held"]
    )
    progress_gate = bool(
        gates["typed_edit_progress_gate"]
        and gates["sentinel_scientific_state_progress"]
        and safety_gate
    )
    gates["safety_gate_passed"] = safety_gate
    gates["v13_progress_gate_passed"] = progress_gate
    gates["narrow_iteration_stop_rule_triggered"] = not progress_gate

    report = {
        "run_output": str(args.output),
        "frozen_arch4": ARCH4,
        "summary": summary,
        "result_rows": results,
        "comparison_to_qwen36_v12": comparison,
        "decision_observables": gates,
        "per_accession": per_accession,
        "interpretation_rule": (
            "v1.3 tests typed edit affordances with the same Qwen3.6-27B model and frozen arch4. "
            "The run must produce both study-structure and scientific-observation edits, move at least one "
            "sentinel scientific state forward, and preserve all safety gates. If not, the narrow iteration "
            "stop rule is triggered: do not make a v1.4 prompt/schema tweak; reassess the architecture."
        ),
    }

    print("===== SCIENTIFIC WORKSPACE AGENT v1.3 ARCH4 / QWEN3.6-27B =====")
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
    print("\n===== QWEN3.6 v1.2 -> v1.3 =====")
    for key, values in comparison.items():
        print(
            f"{key}\tv1.2={values['qwen3.6_v1.2']}\tv1.3={values['qwen3.6_v1.3']}"
            f"\tdelta={values['delta_v13_minus_v12']:+d}"
        )
    print("\n===== v1.3 TYPED-EDIT / PROGRESS / SAFETY GATES =====")
    print(json.dumps(report["decision_observables"], indent=2, sort_keys=True))
    print("\n===== SENTINEL DETAILS =====")
    print(json.dumps(per_accession, indent=2, sort_keys=True))

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"\nfull_audit_json={args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
