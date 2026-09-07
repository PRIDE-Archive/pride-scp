#!/usr/bin/env python3
import importlib.util
import json
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("curation_v19", HERE / "pride_scp_curation_v19.py")
MOD = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MOD)


def base_result(**updates):
    result = {
        "biological_sample_unit": "single HeLa cell",
        "biological_unit_class": "single_cell",
        "true_single_cell_ms_samples_present": "yes",
        "cells_per_target_ms_sample": "one",
        "individual_identity_preserved_to_ms": "yes",
        "destructive_pooling_before_ms": "no",
        "pooling_stage": "none",
        "benchmark_only": "no",
        "adjacent_single_cell_only": "no",
        "reanalysis_only": "no",
        "mixed_design": "no",
        "evidence_sufficiency": "sufficient",
        "evidence_refs": {field: ["E001"] for field in MOD.FACT_FIELDS},
        "reason": "fixture",
    }
    result.update(updates)
    return result


def item(raw, label="publication:preparation:methods", ref="E001"):
    return {"ref": ref, "source_kind": "publication", "source_label": label, "text": raw}


def test_linkage_router_quarantines_declared_mismatch_and_cross_routes():
    root = Path(tempfile.mkdtemp())
    evidence = root / "bundle.json"
    evidence.write_text(json.dumps({
        "publication_pride_accessions": ["PXD000002"],
        "tasks": {"samples": [{"section": "methods", "page": 1, "text": "An individual neuron was isolated for LC-MS."}]},
    }))
    ann = root / "PXD000001.json"
    ann.write_text(json.dumps({
        "target_accession": "PXD000001",
        "provenance": {"semantic_evidence_file": str(evidence)},
    }))
    routed, diagnostics = MOD.load_annotations_with_linkage(root)
    assert "PXD000001" not in routed
    assert len(routed["PXD000002"]) == 1
    assert routed["PXD000002"][0]["_annotation_route_kind"] == "explicit_cross_directory_route"
    assert diagnostics[0]["declared_target_matches_explicit"] is False


def test_linkage_router_keeps_unscoped_declared_bundle():
    root = Path(tempfile.mkdtemp())
    evidence = root / "bundle.json"
    evidence.write_text(json.dumps({"tasks": {"samples": []}}))
    ann = root / "PXD000003.json"
    ann.write_text(json.dumps({
        "target_accession": "PXD000003",
        "provenance": {"semantic_evidence_file": str(evidence)},
    }))
    routed, _ = MOD.load_annotations_with_linkage(root)
    assert len(routed["PXD000003"]) == 1
    assert routed["PXD000003"][0]["_annotation_route_kind"] == "declared_unscoped"


def test_deterministic_pool_quarantines_historical_packet_publication_text():
    candidate = {"accession": "PXD000010"}
    base = [
        {"ref": "E001", "source_kind": "repository", "source_label": "dataset_title", "text": "Single-cell proteomics study"},
        {"ref": "E002", "source_kind": "publication", "source_label": "publication:samples:methods", "text": "WRONG PUBLICATION POOLED CELLS"},
    ]
    items, diag = MOD.build_deterministic_evidence_items(candidate, [], base, quarantine_base_publication=True)
    assert any(x["source_kind"] == "repository" for x in items)
    assert not any("WRONG PUBLICATION" in x["text"] for x in items)
    assert diag["quarantined_base_publication_item_count"] == 1


def test_procedural_single_cell_deposition_normalizes_generic_unit():
    items = [{
        "ref": "E001",
        "source_kind": "repository",
        "source_label": "repository:project_json:sampleProcessingProtocol",
        "text": "After single cell deposition into individual wells, samples were digested and analyzed by LC-MS/MS.",
    }]
    branch = MOD.deterministic_qualifying_branch_evidence(items)
    support = MOD.deterministic_onecell_support(items)
    signals = MOD.deterministic_sample_unit_evidence(items)
    result = base_result(biological_sample_unit=None)
    MOD.normalize_biological_sample_unit(result, signals, support, [], [], [], branch)
    assert branch
    assert result["biological_sample_unit"] == "single cell"
    decision, _ = MOD.deterministic_decision(result, onecell_support=support, qualifying_branch_signals=branch)
    assert decision == "include"


def test_single_cell_sorting_at_resolution_normalizes_only_with_procedure():
    items = [{
        "ref": "E001",
        "source_kind": "repository",
        "source_label": "repository:project_json:sampleProcessingProtocol",
        "text": "OCI-AML cells were sorted at single-cell resolution into a 384-well plate containing lysis buffer. The single-cell samples were digested before LC-MS/MS.",
    }]
    branch = MOD.deterministic_qualifying_branch_evidence(items)
    support = MOD.deterministic_onecell_support(items)
    signals = MOD.deterministic_sample_unit_evidence(items)
    result = base_result(biological_sample_unit="Human")
    MOD.normalize_biological_sample_unit(result, signals, support, [], [], [], branch)
    assert result["biological_sample_unit"] == "single cell"


def test_repository_title_only_single_cell_claim_does_not_create_procedural_sample_unit():
    items = [{
        "ref": "E001",
        "source_kind": "repository",
        "source_label": "dataset_title",
        "text": "Single cell proteomics and epiproteomics of cancer cells treated with inhibitor",
    }]
    assert not MOD.deterministic_qualifying_branch_evidence(items)
    assert not MOD.deterministic_sample_unit_evidence(items)


def test_single_cell_resolution_maldi_is_not_onecell_support_after_hyphen_normalization():
    items = [{
        "ref": "E001",
        "source_kind": "repository",
        "source_label": "dataset_description",
        "text": "MALDI MSI protocol for spatial bottom-up proteomics at single-cell resolution in tissue sections.",
    }]
    assert not MOD.deterministic_onecell_support(items)
    assert not MOD.deterministic_qualifying_branch_evidence(items)


def test_direct_single_bacteria_protein_measurement_is_qualifying_branch():
    items = [{
        "ref": "E001",
        "source_kind": "repository",
        "source_label": "dataset_description",
        "text": "Using this workflow, we quantified more than 50 bacterial proteins from single Bacillus subtilis and Escherichia coli cells by mass spectrometry.",
    }]
    assert MOD.deterministic_qualifying_branch_evidence(items)


def test_procedural_generic_does_not_override_sample_linkage_ambiguity():
    items = [
        item("Single cells were isolated and deposited into wells before proteomic LC-MS analysis.", ref="E001"),
        item("Each proteomic sample was enriched in human neurons or BBB structures before digestion.", ref="E002"),
    ]
    branch = MOD.deterministic_qualifying_branch_evidence(items)
    support = MOD.deterministic_onecell_support(items)
    signals = MOD.deterministic_sample_unit_evidence(items)
    ambiguity = MOD.deterministic_sample_linkage_ambiguity(items)
    result = base_result(biological_sample_unit=None)
    MOD.normalize_biological_sample_unit(result, signals, support, [], ambiguity, [], branch)
    assert result["biological_sample_unit"] is None


def test_one_to_many_oocyte_series_establishes_real_onecell_branch():
    items = [{
        "ref": "E001",
        "source_kind": "repository",
        "source_label": "dataset_description",
        "text": "Comparison of mature and immature human oocytes; the amount differed from 1 to 100 oocytes per experiment.",
    }]
    assert MOD.deterministic_qualifying_branch_evidence(items)


def test_specific_named_unit_proteomics_is_onecell_support():
    items = [
        {
            "ref": "E001",
            "source_kind": "repository",
            "source_label": "dataset_description",
            "text": "Matured oocytes from each group were analyzed by single-oocyte proteomics.",
        }
    ]
    support = MOD.deterministic_onecell_support(items)
    assert support


def test_generic_single_cell_proteomics_title_does_not_create_specific_sample_unit():
    items = [
        {
            "ref": "E001",
            "source_kind": "repository",
            "source_label": "dataset_title",
            "text": "Single-cell proteomics of cancer cells",
        }
    ]
    signals = MOD.deterministic_sample_unit_evidence(items)
    assert not [s for s in signals if s.get("specificity") in {"specific", "procedural_generic"}]


if __name__ == "__main__":
    tests = sorted((name, value) for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name, value in tests:
        value()
        print(f"PASS {name}")
