#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

ARCH4 = ["PXD035339", "PXD046467", "PXD041388", "PXD025634"]


def load_json(path: Path):
    return json.loads(path.read_text())


def text_blob(node: dict, keys: tuple[str, ...]) -> str:
    return " ".join(str(node.get(key, "")) for key in keys).lower()


def material_blob(node: dict) -> str:
    return text_blob(node, ("label", "organism", "biological_material", "experimental_role", "notes"))


def regime_blob(node: dict) -> str:
    return text_blob(
        node,
        (
            "label",
            "experimental_role",
            "isolation_or_loading_method",
            "input_or_cell_count_regime",
            "notes",
        ),
    )


def acquisition_blob(node: dict) -> str:
    return text_blob(node, ("label", "acquisition_method_or_platform", "notes"))


def summarize_accession(output: Path, accession: str) -> dict:
    root = output / "study_factor_graphs" / accession
    accepted_path = root / "accepted_graph.json"
    proposal_path = root / "proposal.json"
    if not accepted_path.is_file():
        return {
            "accession": accession,
            "present": False,
            "status": "missing",
            "materials": [],
            "regimes": [],
            "acquisitions": [],
            "relations": [],
            "raw_links": [],
            "removed_raw_links": [],
            "model_calls": 0,
            "proposal_decision": "",
        }
    accepted = load_json(accepted_path)
    proposal = load_json(proposal_path) if proposal_path.is_file() else {}
    return {
        "accession": accession,
        "present": True,
        "status": accepted.get("status", ""),
        "materials": accepted.get("materials", []),
        "regimes": accepted.get("regimes", []),
        "acquisitions": accepted.get("acquisitions", []),
        "relations": accepted.get("relations", []),
        "raw_links": accepted.get("raw_links", []),
        "material_count": len(accepted.get("materials", [])),
        "regime_count": len(accepted.get("regimes", [])),
        "acquisition_count": len(accepted.get("acquisitions", [])),
        "relation_count": len(accepted.get("relations", [])),
        "open_questions": accepted.get("open_questions", []),
        "rejected_items": accepted.get("rejected_items", []),
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
    summary_path = output / "study_factor_graph_stage1_summary.json"
    if not summary_path.is_file():
        raise SystemExit(f"missing FactorGraph Stage-1 summary: {summary_path}")
    summary = load_json(summary_path)
    details = [summarize_accession(output, accession) for accession in ARCH4]
    by_acc = {row["accession"]: row for row in details}

    p35339 = by_acc["PXD035339"]
    p46467 = by_acc["PXD046467"]
    p41388 = by_acc["PXD041388"]
    p25634 = by_acc["PXD025634"]

    # PXD035339: preserve orthogonal experimental regimes even if they share HeLa/CE-MS context.
    p35339_hydrodynamic = any(
        ("hydrodynamic" in regime_blob(node) or "manual" in regime_blob(node))
        and ("single" in regime_blob(node) or "1 cell" in regime_blob(node))
        for node in p35339.get("regimes", [])
    )
    p35339_spray = any(
        "spray" in regime_blob(node)
        and ("voltage" in regime_blob(node) or "low-input" in regime_blob(node) or "low input" in regime_blob(node) or "low-number" in regime_blob(node))
        for node in p35339.get("regimes", [])
    )
    p35339_factorized = (
        p35339.get("status") == "accepted"
        and p35339_hydrodynamic
        and p35339_spray
        and len(p35339.get("regimes", [])) >= 2
    )

    # PXD046467: preserve distinct material/role and Xenopus sampling regime.
    p46467_hela = any("hela" in material_blob(node) for node in p46467.get("materials", []))
    p46467_reference = any(
        "commercial" in material_blob(node)
        or "reference" in material_blob(node)
        or "method development" in material_blob(node)
        for node in p46467.get("materials", [])
    )
    p46467_xenopus = any(
        "xenopus" in material_blob(node) or "laevis" in material_blob(node)
        for node in p46467.get("materials", [])
    )
    p46467_microsampling = any(
        "microsampl" in regime_blob(node) or "aspirat" in regime_blob(node)
        for node in p46467.get("regimes", [])
    )
    p46467_factorized = (
        p46467.get("status") == "accepted"
        and p46467_hela
        and p46467_reference
        and p46467_xenopus
        and p46467_microsampling
    )

    # PXD041388: source-defined evDISCO/tDISCO must survive as a regime.
    p41388_evdisco = any(
        "evdisco" in regime_blob(node)
        or "tdisco" in regime_blob(node)
        or "digital microfluidic" in regime_blob(node)
        for node in p41388.get("regimes", [])
    )

    # PXD025634: explicit Human and Mouse materials must remain distinct; microwell regime retained.
    p25634_human = any(
        "homo sapiens" in material_blob(node) or "human" in material_blob(node) or "hela" in material_blob(node)
        for node in p25634.get("materials", [])
    )
    p25634_mouse = any(
        "mus musculus" in material_blob(node) or "mouse" in material_blob(node) or "ht22" in material_blob(node)
        for node in p25634.get("materials", [])
    )
    p25634_distinct_materials = p25634_human and p25634_mouse and len(p25634.get("materials", [])) >= 2
    p25634_microwell = any(
        "microwell" in regime_blob(node) or "picked single" in regime_blob(node) or "pick" in regime_blob(node)
        for node in p25634.get("regimes", [])
    )

    accepted_raw_links = [
        (row["accession"], link.get("node_id", ""), link.get("raw_file", ""))
        for row in details
        for link in row.get("raw_links", [])
    ]
    one_model_call_each = all(row.get("present") and row.get("model_calls") == 1 for row in details)
    no_downstream_loop = (
        summary.get("total_agent_turns") == 4
        and summary.get("total_tool_actions") == 0
        and summary.get("total_validator_cycles") == 0
        and summary.get("total_validation_errors") == 0
    )
    all_accepted = all(row.get("status") == "accepted" for row in details)
    accepted_node_ids = {
        row["accession"]: {
            node.get("id")
            for group in ("materials", "regimes", "acquisitions")
            for node in row.get(group, [])
        }
        for row in details
    }
    no_orphan_relations = all(
        relation.get("source_id") in accepted_node_ids[row["accession"]]
        and relation.get("target_id") in accepted_node_ids[row["accession"]]
        for row in details
        for relation in row.get("relations", [])
    )
    canonical_ids = all(
        (node.get("id", "").startswith("M") if group == "materials" else node.get("id", "").startswith("R") if group == "regimes" else node.get("id", "").startswith("A"))
        for row in details
        for group in ("materials", "regimes", "acquisitions")
        for node in row.get(group, [])
    )
    safety_gate = all_accepted and no_orphan_relations and canonical_ids
    progress_gate = (
        summary.get("harness_version") == "pride-scp-scientific-workspace-agent-v2-factor-stage1"
        and summary.get("successful") == 4
        and one_model_call_each
        and no_downstream_loop
        and safety_gate
        and p35339_factorized
        and p46467_factorized
        and p41388_evdisco
        and p25634_distinct_materials
        and p25634_microwell
    )

    audit = {
        "harness_version": summary.get("harness_version"),
        "summary": summary,
        "decision_observables": {
            "all_arch4_outputs_present": all(row.get("present") for row in details),
            "all_arch4_factor_graphs_accepted": all_accepted,
            "one_model_call_per_accession": one_model_call_each,
            "no_downstream_annotation_or_validator_loop": no_downstream_loop,
            "rust_assigned_canonical_factor_ids": canonical_ids,
            "no_orphan_relations": no_orphan_relations,
            "accepted_exact_raw_links": accepted_raw_links,
            "raw_links_removed_by_rust": sum(len(row.get("removed_raw_links", [])) for row in details),
            "p35339_hydrodynamic_regime_present": p35339_hydrodynamic,
            "p35339_spray_voltage_regime_present": p35339_spray,
            "p35339_factorized_regimes_present": p35339_factorized,
            "p46467_hela_material_present": p46467_hela,
            "p46467_hela_reference_role_present": p46467_reference,
            "p46467_xenopus_material_present": p46467_xenopus,
            "p46467_capillary_microsampling_regime_present": p46467_microsampling,
            "p46467_factorized_structure_present": p46467_factorized,
            "p41388_evdisco_or_tdisco_regime_present": p41388_evdisco,
            "p25634_human_material_present": p25634_human,
            "p25634_mouse_material_present": p25634_mouse,
            "p25634_distinct_human_mouse_material_nodes": p25634_distinct_materials,
            "p25634_microwell_regime_present": p25634_microwell,
            "factor_stage1_safety_gate_passed": safety_gate,
            "factor_stage1_progress_gate_passed": progress_gate,
            "stop_before_phase_b": not progress_gate,
        },
        "sentinel_details": details,
    }
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n")

    print("===== SCIENTIFIC WORKSPACE AGENT v2 FACTOR STAGE 1 — ARCH4 =====")
    print(json.dumps(summary, indent=2, sort_keys=True))
    print("\n===== FACTOR-STAGE-1 DECISION GATES =====")
    print(json.dumps(audit["decision_observables"], indent=2, sort_keys=True))
    print("\n===== SENTINEL DETAILS =====")
    print(json.dumps(details, indent=2, sort_keys=True))
    print(f"\nfull_audit_json={args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
