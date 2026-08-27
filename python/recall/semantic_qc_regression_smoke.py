#!/usr/bin/env python3
"""Offline logic regression for v0.1.8 independent semantic QC."""
from __future__ import annotations

import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("semantic_qc", HERE / "adjudicate_semantic_qc.py")
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


def main() -> None:
    # Critic uncertainty always triggers jury.
    needed, why = mod.jury_required(
        {"unified_route": "review_medium", "review_flags": []},
        {"decision": "uncertain"},
    )
    assert needed and why == "critic_uncertain"

    # A provisional include that the critic wants to exclude gets independent jury review.
    needed, why = mod.jury_required(
        {"unified_route": "include_candidate", "review_flags": []},
        {"decision": "exclude"},
    )
    assert needed and why == "high_recall_route_vs_exclude"

    # Weak-route include calls are also checked for overcalling.
    needed, why = mod.jury_required(
        {"unified_route": "review_low", "review_flags": []},
        {"decision": "include"},
    )
    assert needed and why == "weak_route_vs_include"

    # Agreement is decisive; disagreement stays uncertain.
    assert mod.combine_decisions({"decision": "include"}, None) == ("include", "critic_only")
    assert mod.combine_decisions({"decision": "include"}, {"decision": "include"}) == (
        "include",
        "critic_jury_agree",
    )
    assert mod.combine_decisions({"decision": "include"}, {"decision": "exclude"}) == (
        "uncertain",
        "critic_jury_disagree_or_jury_uncertain",
    )
    assert mod.combine_decisions({"decision": "uncertain"}, {"decision": "exclude"}) == (
        "exclude",
        "jury_resolved_critic_uncertainty",
    )

    # Evidence packets explicitly carry both source semantic claims and direct excerpts.
    row = {
        "accession": "PXD900999",
        "dataset_title": "fixture",
        "dataset_description": "Individual cells were isolated and analyzed by LC-MS/MS.",
        "semantic_evidence_mode": "publication_backed",
        "semantic_priority": "A_specific",
        "specific_scp_labels": ["single_cell_proteomics"],
        "method_labels": [],
        "broad_context_labels": [],
        "adjacent_labels": [],
        "negative_context_labels": [],
        "unified_route": "review_high",
        "unified_route_reason": "fixture conflict",
        "review_flags": ["specific_discovery_vs_stage04_conflict"],
        "stage04_classification": "no",
        "stage04_model_classification": "yes",
        "stage04_gate_tiers": ["none"],
        "stage04_publication_titles": ["Single-cell proteomics fixture"],
        "stage04_accession_mentioned": True,
        "stage04_grounded_sample_count": 0,
        "stage04_gate_reasons": ["no grounded sample"],
        "stage04_gate_evidence": ["Individual cells were isolated and analyzed by LC-MS/MS."],
        "stage04_sample_summaries": [],
        "stage04_validation_warnings": [],
        "discovery_hits": [],
    }
    evidence = mod.evidence_text(row, 12000)
    assert "Individual cells were isolated and analyzed by LC-MS/MS." in evidence
    assert "Stage-04 final classification: no" in evidence

    print("All v0.1.8 semantic-QC logic regression tests passed.")
    print("Selective jury triggers protect both recall and precision conflicts.")
    print("Critic/jury disagreement remains unresolved rather than being forced.")
    print("Evidence packets preserve source excerpts and prior semantic provenance.")


if __name__ == "__main__":
    main()
