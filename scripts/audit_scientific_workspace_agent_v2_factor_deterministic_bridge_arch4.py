#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

ARCH4 = ["PXD035339", "PXD046467", "PXD041388", "PXD025634"]
HISTORICAL_V13_ERRORS = {
    "PXD035339": 15,
    "PXD046467": 73,
    "PXD041388": 37,
    "PXD025634": 87,
}
EXPECTED_HARNESS = "pride-scp-scientific-workspace-agent-v2-factor-deterministic-bridge"
FACTOR_HARNESS = "pride-scp-scientific-workspace-agent-v2-factor-stage1"


def load_json(path: Path):
    return json.loads(path.read_text())


def low(value) -> str:
    return str(value or "").lower()


def observation_blob(obs: dict) -> str:
    return " ".join(
        low(obs.get(key))
        for key in ("concept_type", "observed_value", "notes", "target_factor_id")
    )


def adjudication_outcome(record: dict) -> str:
    adj = record.get("adjudication", {})
    return low(adj.get("outcome")) if isinstance(adj, dict) else ""


def accepted_node_ids(graph: dict) -> set[str]:
    return {
        node.get("id", "")
        for key in ("materials", "regimes", "acquisitions")
        for node in graph.get(key, [])
        if node.get("id")
    }


def summarize(output: Path, accession: str) -> dict:
    root = output / "deterministic_bridge" / accession
    result_path = root / "result.json"
    graph_path = root / "accepted_factor_graph.json"
    accepted_path = root / "derived_observations.json"
    if not result_path.is_file() or not graph_path.is_file() or not accepted_path.is_file():
        return {
            "accession": accession,
            "present": False,
            "terminal_status": "missing",
            "model_calls": None,
            "validator_cycles": 0,
            "validation_errors": None,
            "observations": [],
            "adjudications": [],
            "factor_graph": {},
        }
    result = load_json(result_path)
    graph = load_json(graph_path)
    acceptance = load_json(accepted_path)
    validation = result.get("validation", {})
    return {
        "accession": accession,
        "present": True,
        "terminal_status": result.get("terminal_status", ""),
        "model_calls": result.get("model_calls", None),
        "tool_actions": result.get("tool_actions", 0),
        "validator_cycles": result.get("validator_cycles", 0),
        "validation_errors": validation.get("validation_errors"),
        "validation_warnings": validation.get("validation_warnings"),
        "observations": acceptance.get("observations", []),
        "rejected_observations": acceptance.get("rejected_observations", []),
        "adjudications": result.get("adjudications", []),
        "factor_graph": graph,
        "factor_graph_status": graph.get("status", ""),
        "factor_graph_harness": graph.get("harness_version", ""),
        "bridge_harness": acceptance.get("harness_version", ""),
        "result_path": str(result_path),
    }


def has_obs(row: dict, target: str, *terms: str, concept: str | None = None) -> bool:
    for obs in row.get("observations", []):
        if obs.get("target_factor_id") != target:
            continue
        if concept is not None and obs.get("concept_type") != concept:
            continue
        blob = observation_blob(obs)
        if all(term.lower() in blob for term in terms):
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--json-out", required=True, type=Path)
    args = ap.parse_args()

    summary_path = args.output / "factor_deterministic_bridge_summary.json"
    if not summary_path.is_file():
        raise SystemExit(f"missing deterministic bridge summary: {summary_path}")
    summary = load_json(summary_path)
    details = [summarize(args.output, acc) for acc in ARCH4]
    by_acc = {row["accession"]: row for row in details}

    all_present = all(row.get("present") for row in details)
    zero_model_calls_each = all(row.get("model_calls") == 0 for row in details)
    one_validator_each = all(row.get("validator_cycles") == 1 for row in details)
    no_tool_loop = all(row.get("tool_actions") == 0 for row in details)
    bounded = (
        summary.get("total_agent_turns") == 0
        and summary.get("total_tool_actions") == 0
        and summary.get("total_validator_cycles") == 4
        and zero_model_calls_each
        and one_validator_each
        and no_tool_loop
    )

    no_orphan_observations = True
    rust_factor_ids_only = True
    for row in details:
        ids = accepted_node_ids(row.get("factor_graph", {}))
        for obs in row.get("observations", []):
            target = obs.get("target_factor_id", "")
            no_orphan_observations &= target in ids
            rust_factor_ids_only &= (
                bool(target)
                and target[0] in "MRA"
                and len(target) == 4
                and target[1:].isdigit()
            )

    p35339 = by_acc["PXD035339"]
    p35339_hydrodynamic = has_obs(
        p35339, "R002", "hydrodynamic", concept="isolation_method"
    ) or has_obs(p35339, "R002", "manual", concept="isolation_method")
    p35339_spray = has_obs(
        p35339, "R001", "spray", concept="isolation_method"
    )
    p35339_no_cross_scope = not (
        has_obs(p35339, "R001", "hydrodynamic", concept="isolation_method")
        or has_obs(p35339, "R002", "spray", concept="isolation_method")
    )
    p35339_scoped = p35339_hydrodynamic and p35339_spray and p35339_no_cross_scope

    p46467 = by_acc["PXD046467"]
    p46467_capillary = any(
        obs.get("target_factor_id") == "R002"
        and obs.get("concept_type") == "isolation_method"
        and any(term in observation_blob(obs) for term in ("capillary", "aspirat", "microsampl"))
        for obs in p46467.get("observations", [])
    )
    p46467_no_hela_isolation = not any(
        obs.get("target_factor_id") == "R001"
        and obs.get("concept_type") == "isolation_method"
        for obs in p46467.get("observations", [])
    )
    p46467_scoped = p46467_capillary and p46467_no_hela_isolation

    p41388 = by_acc["PXD041388"]
    p41388_evdisco = any(
        obs.get("target_factor_id") == "R001"
        and obs.get("concept_type") == "isolation_method"
        and any(term in observation_blob(obs) for term in ("evdisco", "tdisco", "digital microfluidic"))
        for obs in p41388.get("observations", [])
    )
    p41388_has_isolation_adjudication = any(
        rec.get("concept_type") == "isolation_method"
        and adjudication_outcome(rec) in {"canonical", "template_gap", "unresolved", "conflict"}
        for rec in p41388.get("adjudications", [])
    )
    p41388_scoped = p41388_evdisco and p41388_has_isolation_adjudication

    p25634 = by_acc["PXD025634"]
    p25634_microwell = any(
        obs.get("target_factor_id") == "R001"
        and obs.get("concept_type") == "isolation_method"
        and any(term in observation_blob(obs) for term in ("microwell", "picked single", "pick"))
        for obs in p25634.get("observations", [])
    )
    p25634_template_gap = any(
        rec.get("concept_type") == "isolation_method"
        and adjudication_outcome(rec) == "template_gap"
        for rec in p25634.get("adjudications", [])
    )
    p25634_no_manual_picking_canonical = not any(
        rec.get("concept_type") == "isolation_method"
        and adjudication_outcome(rec) == "canonical"
        and "manual picking" in low(rec.get("adjudication", {}).get("value"))
        for rec in p25634.get("adjudications", [])
    )
    graph25634 = p25634.get("factor_graph", {})
    organisms25634 = {
        low(node.get("organism")) for node in graph25634.get("materials", [])
    }
    p25634_materials_preserved = (
        len(graph25634.get("materials", [])) >= 2
        and "homo sapiens" in organisms25634
        and "mus musculus" in organisms25634
    )
    p25634_scoped = (
        p25634_microwell
        and p25634_template_gap
        and p25634_no_manual_picking_canonical
        and p25634_materials_preserved
    )

    factor_graphs_frozen = all(
        row.get("factor_graph_status") == "accepted"
        and row.get("factor_graph_harness") == FACTOR_HARNESS
        for row in details
    )
    bridge_identity_held = all(row.get("bridge_harness") == EXPECTED_HARNESS for row in details)

    validation_deltas = {}
    validation_gain_any = False
    for row in details:
        acc = row["accession"]
        current = row.get("validation_errors")
        baseline = HISTORICAL_V13_ERRORS[acc]
        delta = None if current is None else current - baseline
        validation_deltas[acc] = {
            "historical_v13": baseline,
            "deterministic_bridge": current,
            "delta": delta,
        }
        if current is not None and current < baseline:
            validation_gain_any = True

    material_review_gain = all(
        [p35339_scoped, p46467_scoped, p41388_scoped, p25634_scoped]
    )
    safety_gate = (
        all_present
        and bounded
        and factor_graphs_frozen
        and bridge_identity_held
        and no_orphan_observations
        and rust_factor_ids_only
        and p35339_no_cross_scope
        and p46467_no_hela_isolation
        and p25634_no_manual_picking_canonical
    )
    progress_gate = (
        summary.get("harness_version") == EXPECTED_HARNESS
        and summary.get("successful") == 4
        and safety_gate
        and (validation_gain_any or material_review_gain)
    )

    audit = {
        "harness_version": summary.get("harness_version"),
        "summary": summary,
        "decision_observables": {
            "all_arch4_outputs_present": all_present,
            "zero_model_calls_per_accession": zero_model_calls_each,
            "one_validator_pass_per_accession": one_validator_each,
            "no_conversational_tool_loop": no_tool_loop,
            "bounded_deterministic_bridge_execution": bounded,
            "accepted_factor_graphs_frozen": factor_graphs_frozen,
            "deterministic_bridge_harness_identity_held": bridge_identity_held,
            "no_orphan_observations": no_orphan_observations,
            "rust_factor_ids_only": rust_factor_ids_only,
            "p35339_hydrodynamic_observation_on_r002": p35339_hydrodynamic,
            "p35339_spray_observation_on_r001": p35339_spray,
            "p35339_no_cross_regime_observation_collapse": p35339_no_cross_scope,
            "p46467_capillary_observation_on_xenopus_regime": p46467_capillary,
            "p46467_no_hela_isolation_invention": p46467_no_hela_isolation,
            "p41388_source_faithful_evdisco_observation": p41388_evdisco,
            "p41388_isolation_reaches_rust_adjudication": p41388_has_isolation_adjudication,
            "p25634_microwell_observation_retained": p25634_microwell,
            "p25634_template_gap_retained": p25634_template_gap,
            "p25634_no_forced_manual_picking_canonical": p25634_no_manual_picking_canonical,
            "p25634_human_mouse_factor_structure_preserved": p25634_materials_preserved,
            "historical_validation_comparison": validation_deltas,
            "at_least_one_validation_error_reduction": validation_gain_any,
            "materially_improved_review_state": material_review_gain,
            "factor_deterministic_bridge_safety_gate_passed": safety_gate,
            "factor_deterministic_bridge_progress_gate_passed": progress_gate,
            "stop_before_broader_cohort": not progress_gate,
        },
        "sentinel_details": details,
    }

    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n")

    print("===== PRIDE-SCP v2 FACTOR DETERMINISTIC BRIDGE — ARCH4 =====")
    print(json.dumps(summary, indent=2, sort_keys=True))
    print("\n===== DETERMINISTIC-BRIDGE DECISION GATES =====")
    print(json.dumps(audit["decision_observables"], indent=2, sort_keys=True))
    print("\n===== SENTINEL DETAILS =====")
    print(json.dumps(details, indent=2, sort_keys=True))
    print(f"\nfull_audit_json={args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
