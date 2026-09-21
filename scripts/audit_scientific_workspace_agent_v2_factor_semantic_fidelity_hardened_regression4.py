#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

REGRESSION4 = ["PXD046863", "PXD001641", "PXD046467", "PXD035339"]
EXPECTED_HARNESS = "pride-scp-scientific-workspace-agent-v2-factor-semantic-fidelity-hardened"
FACTOR_HARNESS = "pride-scp-scientific-workspace-agent-v2-factor-stage1"


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def low(value) -> str:
    return str(value or "").lower()


def norm(value) -> str:
    text = low(value)
    return " ".join("".join(ch if ch.isalnum() else " " for ch in text).split())


def acquisition_class(value) -> str | None:
    value = norm(value)
    tokens = value.split()
    dda = (
        "data dependent" in value
        or "dda pasef" in value
        or "ddapasef" in value
        or ("dda" in tokens and any(term in value for term in (" ms", "acquisition", "pasef")))
    )
    dia_nn_only = (
        "dia nn" in value
        and "data independent" not in value
        and "dia pasef" not in value
        and "diapasef" not in value
        and "dia mode" not in value
        and "swath" not in value
    )
    dia = (
        "data independent" in value
        or "dia pasef" in value
        or "diapasef" in value
        or "swath" in value
        or (
            not dia_nn_only
            and "dia" in tokens
            and any(term in value for term in (" ms", "acquisition", "mode", "pasef"))
        )
    )
    if dda and not dia:
        return "dda"
    if dia and not dda:
        return "dia"
    return None


def adjudication_outcome(record: dict) -> str:
    adj = record.get("adjudication", {})
    return low(adj.get("outcome")) if isinstance(adj, dict) else ""


def acquisition_faithful(record: dict) -> bool:
    if record.get("concept_type") != "acquisition_mode":
        return True
    if adjudication_outcome(record) != "canonical":
        return True
    observed = acquisition_class(record.get("observed_value"))
    canonical = acquisition_class(record.get("adjudication", {}).get("value"))
    return observed is not None and canonical is not None and observed == canonical


def isolation_faithful(record: dict) -> bool:
    if record.get("concept_type") != "isolation_method":
        return True
    if adjudication_outcome(record) != "canonical":
        return True
    observed = norm(record.get("observed_value"))
    canonical = norm(record.get("adjudication", {}).get("value"))
    if not observed or not canonical:
        return False
    if observed == canonical or canonical in observed or observed in canonical:
        return True
    if canonical == "manual picking":
        return any(
            term in observed
            for term in (
                "manual picking",
                "manual pick",
                "manually picked",
                "picked single cell",
                "single cell picked",
                "cell picking",
            )
        )
    return False


def summarize(output: Path, accession: str) -> dict:
    root = output / "semantic_fidelity_hardened" / accession
    result_path = root / "result.json"
    graph_path = root / "accepted_factor_graph.json"
    observations_path = root / "derived_observations.json"
    if not result_path.is_file() or not graph_path.is_file() or not observations_path.is_file():
        return {"accession": accession, "present": False}
    result = load_json(result_path)
    graph = load_json(graph_path)
    observations = load_json(observations_path)
    return {
        "accession": accession,
        "present": True,
        "result": result,
        "graph": graph,
        "observations": observations.get("observations", []),
        "adjudications": result.get("adjudications", []),
        "bridge_harness": observations.get("harness_version", ""),
        "factor_graph_harness": graph.get("harness_version", ""),
        "factor_graph_status": graph.get("status", ""),
    }


def canonical_acquisition(row: dict, branch_id: str) -> str | None:
    for rec in row.get("adjudications", []):
        if (
            rec.get("concept_type") == "acquisition_mode"
            and rec.get("branch_id") == branch_id
            and adjudication_outcome(rec) == "canonical"
        ):
            return acquisition_class(rec.get("adjudication", {}).get("value"))
    return None


def first_canonical_acquisition(row: dict) -> str | None:
    for rec in row.get("adjudications", []):
        if rec.get("concept_type") == "acquisition_mode" and adjudication_outcome(rec) == "canonical":
            return acquisition_class(rec.get("adjudication", {}).get("value"))
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--json-out", required=True, type=Path)
    args = ap.parse_args()

    summary_path = args.output / "factor_semantic_fidelity_hardened_summary.json"
    if not summary_path.is_file():
        raise SystemExit(f"missing semantic-fidelity summary: {summary_path}")
    summary = load_json(summary_path)
    details = [summarize(args.output, acc) for acc in REGRESSION4]
    by_acc = {row["accession"]: row for row in details}

    all_present = all(row.get("present") for row in details)
    bounded = (
        summary.get("accessions_requested") == 4
        and summary.get("successful") == 4
        and summary.get("errors") == 0
        and summary.get("total_agent_turns") == 0
        and summary.get("total_tool_actions") == 0
        and summary.get("total_validator_cycles") == 4
        and all(row.get("result", {}).get("model_calls") == 0 for row in details)
        and all(row.get("result", {}).get("tool_actions") == 0 for row in details)
        and all(row.get("result", {}).get("validator_cycles") == 1 for row in details)
    )
    factor_graphs_frozen = all(
        row.get("factor_graph_status") == "accepted"
        and row.get("factor_graph_harness") == FACTOR_HARNESS
        for row in details
    )
    harness_identity = all(row.get("bridge_harness") == EXPECTED_HARNESS for row in details)

    all_acquisition_faithful = all(
        acquisition_faithful(rec)
        for row in details
        for rec in row.get("adjudications", [])
    )
    all_isolation_faithful = all(
        isolation_faithful(rec)
        for row in details
        for rec in row.get("adjudications", [])
    )

    p46863 = by_acc["PXD046863"]
    p1641 = by_acc["PXD001641"]
    p46467 = by_acc["PXD046467"]
    p35339 = by_acc["PXD035339"]

    p46863_a1_dia = canonical_acquisition(p46863, "A001") == "dia"
    p46863_a2_dda = canonical_acquisition(p46863, "A002") == "dda"
    p1641_dda = first_canonical_acquisition(p1641) == "dda"
    p46467_dia = first_canonical_acquisition(p46467) == "dia"
    p35339_no_false_manual = not any(
        rec.get("concept_type") == "isolation_method"
        and adjudication_outcome(rec) == "canonical"
        and norm(rec.get("adjudication", {}).get("value")) == "manual picking"
        for rec in p35339.get("adjudications", [])
    )

    safety = all(
        (
            all_present,
            summary.get("harness_version") == EXPECTED_HARNESS,
            bounded,
            factor_graphs_frozen,
            harness_identity,
            all_acquisition_faithful,
            all_isolation_faithful,
            p46863_a1_dia,
            p46863_a2_dda,
            p1641_dda,
            p46467_dia,
            p35339_no_false_manual,
        )
    )

    decision = {
        "all_regression_outputs_present": all_present,
        "bounded_semantic_fidelity_execution": bounded,
        "accepted_factor_graphs_frozen": factor_graphs_frozen,
        "semantic_fidelity_harness_identity_held": harness_identity,
        "all_acquisition_canonicalizations_semantically_faithful": all_acquisition_faithful,
        "all_isolation_canonicalizations_semantically_faithful": all_isolation_faithful,
        "p46863_a1_remains_dia": p46863_a1_dia,
        "p46863_a2_is_dda": p46863_a2_dda,
        "p1641_dda_control_passed": p1641_dda,
        "p46467_dia_control_passed": p46467_dia,
        "p35339_no_false_manual_picking": p35339_no_false_manual,
        "semantic_fidelity_regression4_safety_gate_passed": safety,
        "stop_before_accepted22_rerun": not safety,
    }

    audit = {
        "summary": summary,
        "decision_observables": decision,
        "accession_details": details,
    }
    args.json_out.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print("===== PRIDE-SCP v2 SEMANTIC-FIDELITY HARDENING — REGRESSION4 =====")
    print(json.dumps(decision, indent=2, sort_keys=True))
    print(f"full_audit_json={args.json_out}")
    return 0 if safety else 2


if __name__ == "__main__":
    raise SystemExit(main())
