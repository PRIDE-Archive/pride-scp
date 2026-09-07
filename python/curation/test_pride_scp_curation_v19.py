#!/usr/bin/env python3
import importlib.util
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


def test_include():
    support = [item("A single HeLa cell was processed for proteomic LC-MS analysis.")]
    decision, _ = MOD.deterministic_decision(base_result(), onecell_support=support)
    assert decision == "include"


def test_model_only_destructive_pooling_routes_to_review_without_direct_risk():
    decision, _ = MOD.deterministic_decision(
        base_result(
            true_single_cell_ms_samples_present="no",
            individual_identity_preserved_to_ms="no",
            destructive_pooling_before_ms="yes",
            pooling_stage="before_identity_preserving_processing",
        )
    )
    assert decision == "review"


def test_unreferenced_assertions_are_downgraded():
    result = base_result()
    result["evidence_refs"] = {field: [] for field in MOD.FACT_FIELDS}
    warnings = MOD.validate_evidence_refs(result, [item("single cell")])
    assert result["true_single_cell_ms_samples_present"] == "uncertain"
    assert result["pooling_stage"] == "uncertain"
    assert result["biological_sample_unit"] is None
    assert warnings


def test_decision_conflict_reviews():
    result = base_result(benchmark_only="yes")
    support = [item("A single HeLa cell was processed for proteomic LC-MS analysis.")]
    decision, _ = MOD.deterministic_decision(result, onecell_support=support)
    assert decision == "review"


def test_explicit_multiple_cell_count_blocks_include():
    result = base_result(cells_per_target_ms_sample="approximately 1,000,000 cells")
    warnings = MOD.normalize_fact_consistency(result, [])
    assert result["true_single_cell_ms_samples_present"] == "no"
    assert result["individual_identity_preserved_to_ms"] == "no"
    decision, _ = MOD.deterministic_decision(result)
    assert decision == "exclude"
    assert warnings


def test_nonqualifying_unit_class_alone_routes_to_review_not_exclude():
    result = base_result(biological_unit_class="cell_population", cells_per_target_ms_sample=None)
    warnings = MOD.normalize_fact_consistency(result, [])
    support = [item("A single HeLa cell was processed for proteomic LC-MS analysis.")]
    decision, _ = MOD.deterministic_decision(result, onecell_support=support)
    assert decision == "review"
    assert warnings


def test_model_only_pre_identity_pooling_routes_to_review():
    result = base_result(pooling_stage="before_identity_preserving_processing")
    warnings = MOD.normalize_fact_consistency(result, [])
    decision, _ = MOD.deterministic_decision(result)
    assert decision == "review"
    assert warnings


def test_post_label_pooling_remains_eligible():
    result = base_result(pooling_stage="after_identity_preserving_labeling")
    support = [item("A single HeLa cell was proteomically processed and labeled before multiplex pooling.")]
    decision, _ = MOD.deterministic_decision(result, onecell_support=support)
    assert decision == "include"


def test_pxd000265_like_multicell_transfer_is_high_specificity_risk():
    items = [item("Approximately 50-70 isolated egg cells were transferred into a 1 mL droplet of SDS-sample buffer for proteomic analysis.")]
    risks = MOD.deterministic_multicell_evidence(items)
    assert risks
    result = base_result()
    MOD.normalize_fact_consistency(result, risks)
    decision, _ = MOD.deterministic_decision(result, risks)
    assert decision == "exclude"


def test_pxd028991_like_cells_per_replicate_is_high_specificity_risk():
    items = [item("Approximately 1,000,000 FACS-isolated root-hair protoplasts were used per proteomic replicate.", label="publication:samples:methods")]
    risks = MOD.deterministic_multicell_evidence(items)
    assert risks




def test_pxd028991_live_scientific_notation_across_sentences_is_risk():
    items = [
        {
            "ref": "E002",
            "source_kind": "repository",
            "source_label": "dataset_description",
            "text": (
                "We isolated 1 x 106 root hair cells by Fluorescence-activated cell sorting (FACS) "
                "from three independent replicate roots. The extracted proteins from each sample were "
                "trypsin-digested and analyzed by LC-MS/MS."
            ),
        }
    ]
    risks = MOD.deterministic_multicell_evidence(items)
    assert risks


def test_cache_requires_pipeline_version_model_and_packet_fingerprint():
    packet = {"accession": "PXD000001", "evidence_items": [{"ref": "E001", "text": "single cell"}]}
    key = MOD.curation_cache_key(packet, "qwen2.5:3b")
    cached = {
        "curation_pipeline_version": MOD.PIPELINE_VERSION,
        "curation_cache_key": key,
        "model": "qwen2.5:3b",
        "summary": {"accession": "PXD000001"},
    }
    assert MOD.cache_is_compatible(cached, key, "qwen2.5:3b")
    cached["curation_pipeline_version"] = "v19-shadow-1"
    assert not MOD.cache_is_compatible(cached, key, "qwen2.5:3b")


def test_cache_invalidates_when_packet_changes():
    packet1 = {"accession": "PXD000001", "evidence_items": [{"ref": "E001", "text": "single cell"}]}
    packet2 = {"accession": "PXD000001", "evidence_items": [{"ref": "E001", "text": "pooled cells"}]}
    assert MOD.curation_cache_key(packet1, "qwen2.5:3b") != MOD.curation_cache_key(packet2, "qwen2.5:3b")


def test_pxd003519_like_microdissected_cells_per_sample_is_high_specificity_risk():
    items = [item("About 2,500 microdissected neurons were collected per proteomic sample.", label="publication:samples:methods")]
    risks = MOD.deterministic_multicell_evidence(items)
    assert risks


def test_post_label_tmt_pooling_not_high_specificity_risk():
    items = [item("Fourteen single cells were TMT labeled and pooled with a 200-cell carrier before LC-MS analysis.")]
    risks = MOD.deterministic_multicell_evidence(items)
    assert not risks


def test_prior_work_pooling_not_high_specificity_risk():
    items = [item("In previous single-cell RNA sequencing work, 100 cells were pooled into one tube.")]
    risks = MOD.deterministic_multicell_evidence(items)
    assert not risks


def test_generic_multi_cell_comparison_not_high_specificity_risk():
    items = [item("20 or 40 cells is not enough for robust biological averaging in real datasets.")]
    risks = MOD.deterministic_multicell_evidence(items)
    assert not risks




def test_literal_sample_unit_hallucination_is_removed():
    result = base_result(biological_sample_unit="single bovine oocyte")
    result["evidence_refs"]["biological_sample_unit"] = ["E001"]
    warnings = MOD.validate_literal_value_grounding(
        result,
        [item("Single-cell proteomics of human brain neurons after ischemia.")],
    )
    assert result["biological_sample_unit"] is None
    assert warnings


def test_literal_sample_unit_supported_identity_is_retained():
    result = base_result(biological_sample_unit="single Xenopus blastomere")
    result["evidence_refs"]["biological_sample_unit"] = ["E001"]
    warnings = MOD.validate_literal_value_grounding(
        result,
        [item("An individual Xenopus blastomere was isolated and processed for proteomic analysis.")],
    )
    assert result["biological_sample_unit"] == "single Xenopus blastomere"
    assert not warnings


def test_repository_single_cell_proteomics_title_is_onecell_support():
    items = [{
        "ref": "E001",
        "source_kind": "repository",
        "source_label": "dataset_title",
        "text": "Single cell proteomics of cancer cells treated with inhibitor",
    }]
    assert MOD.deterministic_onecell_support(items)


def test_background_single_cell_publication_text_is_not_onecell_support():
    items = [item(
        "Recently, single-cell proteomics has been widely used. We previously analyzed pooled egg cells.",
        label="publication:samples:introduction",
    )]
    assert not MOD.deterministic_onecell_support(items)


def test_direct_individual_blastomere_proteomics_is_onecell_support():
    items = [item(
        "An individual Xenopus blastomere was isolated into a tube and processed for proteomic LC-MS analysis.",
        label="publication:samples:methods",
    )]
    assert MOD.deterministic_onecell_support(items)


def test_pxd003519_like_cell_enriched_sample_is_review_ambiguity():
    items = [item(
        "Proteomics Analysis—Sample Preparation—A total of 30 uL of each sample enriched in human neurons or BBB structures was reduced and digested.",
        label="publication:preparation:methods",
    )]
    ambiguity = MOD.deterministic_sample_linkage_ambiguity(items)
    assert ambiguity
    result = base_result(biological_sample_unit="individual human neuron")
    support = [item("An individual human neuron was isolated for proteomic analysis.", label="publication:samples:methods")]
    decision, _ = MOD.deterministic_decision(result, onecell_support=support, ambiguity_signals=ambiguity)
    assert decision == "review"


def test_pxd015175_like_fertilization_combination_is_not_direct_multicell_ms_risk():
    items = [item(
        "Eggs and minced testes were combined for a 20-minute fertilization. Individual blastomeres were later isolated and processed for proteomic LC-MS analysis.",
        label="publication:samples:methods",
    )]
    assert not MOD.deterministic_multicell_evidence(items)
    assert MOD.deterministic_onecell_support(items)


def test_model_negative_identity_without_authoritative_exclusion_routes_review():
    result = base_result(
        true_single_cell_ms_samples_present="no",
        individual_identity_preserved_to_ms="no",
        destructive_pooling_before_ms="yes",
        pooling_stage="before_identity_preserving_processing",
    )
    decision, _ = MOD.deterministic_decision(result, risk_signals=[])
    assert decision == "review"



# v19-shadow-2.3 regression tests

def test_contradictory_no_pooling_plus_preidentity_stage_normalizes_to_none_and_can_include():
    result = base_result(
        destructive_pooling_before_ms="no",
        pooling_stage="before_identity_preserving_processing",
    )
    warnings = MOD.normalize_fact_consistency(result, [])
    assert result["pooling_stage"] == "none"
    support = [item("An individual Xenopus blastomere was isolated and processed for proteomic LC-MS analysis.")]
    decision, _ = MOD.deterministic_decision(result, onecell_support=support)
    assert decision == "include"
    assert warnings


def test_direct_benchmark_lysate_defined_ratios_excludes_without_onecell_branch():
    items = [{
        "ref": "E001",
        "source_kind": "repository",
        "source_label": "dataset_description",
        "text": "SILAC labelled HeLa cell lysates were mixed in defined ratios and analysed by DIA to serve as a benchmark.",
    }]
    signals = MOD.deterministic_benchmark_only_evidence(items)
    assert signals
    result = base_result(
        true_single_cell_ms_samples_present="yes",
        biological_sample_unit="HeLa cell",
        benchmark_only="no",
    )
    decision, _ = MOD.deterministic_decision(result, onecell_support=[], benchmark_signals=signals)
    assert decision == "exclude"


def test_model_benchmark_only_cannot_exclude_direct_single_oocyte_branch():
    result = base_result(
        biological_sample_unit="single human oocyte",
        biological_unit_class="cell_population",
        benchmark_only="yes",
        true_single_cell_ms_samples_present="no",
    )
    support = [
        item("Even starting from a single oocyte, proteins were identified by LC-MS/MS.", ref="E001"),
        item("Samples containing single oocytes were processed for proteome analysis.", ref="E002"),
    ]
    decision, _ = MOD.deterministic_decision(result, onecell_support=support, benchmark_signals=[])
    assert decision == "review"


def test_taxon_only_sample_unit_cannot_include_repository_title_only_case():
    result = base_result(biological_sample_unit="Homo sapiens")
    support = [
        item("Single cell proteomics of cancer cells", label="repository:dataset_title", ref="E001"),
        item("single cell proteomics", label="repository:dataset_description", ref="E002"),
    ]
    decision, _ = MOD.deterministic_decision(result, onecell_support=support)
    assert decision == "review"


def test_strong_direct_onecell_support_can_override_noisy_cell_population_class():
    result = base_result(
        biological_sample_unit="single cell",
        biological_unit_class="cell_population",
    )
    support = [
        item("A single cell was processed for proteomic LC-MS analysis.", ref="E001"),
        item("Individual cells were isolated into wells and analyzed by LC-MS/MS.", ref="E002"),
    ]
    decision, _ = MOD.deterministic_decision(result, onecell_support=support)
    assert decision == "include"



# v19-shadow-2.4 regression tests

def test_mixed_design_bulk_arm_does_not_veto_direct_single_cell_branch():
    items = [
        item(
            "Single cell proteomics: single cells were dispensed into individual nanowells and processed for protein digestion.",
            label="publication:samples:methods",
            ref="E001",
        ),
        item(
            "For the bulk germ-layer proteome, 100,000 cells per replicate were collected in protein LoBind tubes.",
            label="publication:preparation:methods",
            ref="E002",
        ),
    ]
    risks = MOD.deterministic_multicell_evidence(items)
    branches = MOD.deterministic_qualifying_branch_evidence(items)
    assert risks
    assert branches
    result = base_result(biological_sample_unit="single cell")
    warnings = MOD.normalize_fact_consistency(result, risks, branches)
    assert result["mixed_design"] == "yes"
    decision, _ = MOD.deterministic_decision(
        result,
        risk_signals=risks,
        onecell_support=MOD.deterministic_onecell_support(items),
        qualifying_branch_signals=branches,
    )
    assert decision == "include"
    assert warnings


def test_multicell_risk_without_real_onecell_branch_still_excludes():
    items = [
        {
            "ref": "E001",
            "source_kind": "repository",
            "source_label": "dataset_description",
            "text": (
                "We isolated 1 x 10^6 root hair cells by FACS. "
                "The extracted proteins from each sample were digested and analyzed by LC-MS/MS."
            ),
        }
    ]
    risks = MOD.deterministic_multicell_evidence(items)
    branches = MOD.deterministic_qualifying_branch_evidence(items)
    assert risks
    assert not branches
    result = base_result()
    MOD.normalize_fact_consistency(result, risks, branches)
    decision, _ = MOD.deterministic_decision(result, risk_signals=risks, qualifying_branch_signals=branches)
    assert decision == "exclude"


def test_near_single_cell_spatial_resolution_is_nonqualifying_without_onecell_branch():
    items = [
        item(
            "MALDI-IMS and LCM-LC-MS/MS achieved near-single-cell resolution in spatial proteomics of tissue regions.",
            label="publication:samples:results",
        )
    ]
    signals = MOD.deterministic_nonqualifying_input_evidence(items)
    assert signals
    decision, _ = MOD.deterministic_decision(
        base_result(),
        nonqualifying_signals=signals,
        qualifying_branch_signals=[],
    )
    assert decision == "exclude"


def test_exact_single_cell_resolution_is_not_near_single_cell_spatial_negative():
    items = [
        {
            "ref": "E001",
            "source_kind": "repository",
            "source_label": "dataset_description",
            "text": "New workflow for spatial bottom-up proteomics at single-cell resolution.",
        }
    ]
    assert not MOD.deterministic_nonqualifying_input_evidence(items)


def test_commercial_cell_digest_input_is_nonqualifying_without_real_cell_branch():
    items = [
        {
            "ref": "E001",
            "source_kind": "repository",
            "source_label": "dataset_description",
            "text": (
                "The system identified proteins when 0.25 ng of a commercial HeLa cell digest "
                "was available in the sample vial and 0.1 ng was injected."
            ),
        }
    ]
    signals = MOD.deterministic_nonqualifying_input_evidence(items)
    branches = MOD.deterministic_qualifying_branch_evidence(items)
    assert signals
    assert not branches
    decision, _ = MOD.deterministic_decision(
        base_result(),
        nonqualifying_signals=signals,
        qualifying_branch_signals=branches,
    )
    assert decision == "exclude"


def test_single_cell_like_amount_is_nonqualifying_without_real_cell_branch():
    items = [
        item(
            "Benchmark experiments used mixed-species single cell-like amounts with a maximum 300 pg input.",
            label="publication:samples:methods",
        )
    ]
    signals = MOD.deterministic_nonqualifying_input_evidence(items)
    assert signals
    decision, _ = MOD.deterministic_decision(base_result(), nonqualifying_signals=signals)
    assert decision == "exclude"


def test_noncell_benchmark_control_does_not_veto_separate_real_single_cell_branch():
    items = [
        item(
            "A commercial HeLa cell digest was injected at 250 pg as a low-input control.",
            label="publication:samples:methods",
            ref="E001",
        ),
        item(
            "Individual cell isolation, lysis and digestion were performed in a 384-well plate for single-cell proteomics.",
            label="publication:preparation:methods",
            ref="E002",
        ),
    ]
    signals = MOD.deterministic_nonqualifying_input_evidence(items)
    branches = MOD.deterministic_qualifying_branch_evidence(items)
    assert signals
    assert branches
    decision, _ = MOD.deterministic_decision(
        base_result(),
        onecell_support=MOD.deterministic_onecell_support(items),
        nonqualifying_signals=signals,
        qualifying_branch_signals=branches,
    )
    assert decision == "include"


def test_single_cell_proteomics_phrase_alone_is_not_qualifying_branch():
    items = [
        {
            "ref": "E001",
            "source_kind": "repository",
            "source_label": "dataset_description",
            "text": "This platform is ready for single cell proteomics and improves sample loading.",
        }
    ]
    assert not MOD.deterministic_qualifying_branch_evidence(items)



def test_subcellular_sampling_from_identified_cell_is_qualifying_branch():
    items = [
        item(
            "Using capillary microsampling to collect the proteome from identified cells in a cleavage-stage embryo, subcellular proteomics was performed.",
            label="publication:samples:discussion",
            ref="E001",
        ),
        item(
            "We tested fast subcellular proteomics in the D11 cell and conducted bottom-up proteomics on the single-cell proteome.",
            label="publication:samples:discussion",
            ref="E002",
        ),
    ]
    assert MOD.deterministic_qualifying_branch_evidence(items)


# v19-shadow-2.5 biological-sample-unit normalization regression tests

def test_cell_type_nouns_are_recognized_as_cell_like_units():
    for value in (
        "murine hepatocyte",
        "single cardiomyocyte",
        "human brain neutrophil",
        "Murine macrophages",
        "individual Escherichia coli bacterium",
    ):
        assert MOD.sample_unit_has_cell_noun(value), value
    for value in ("Homo sapiens", "mouse", "Xenopus embryo", "brain organoid"):
        assert not MOD.sample_unit_has_cell_noun(value), value


def test_specific_sample_unit_is_extracted_from_direct_methods_evidence():
    items = [
        item(
            "Single HeLa cells were isolated into nanowells and digested for LC-MS/MS.",
            label="publication:samples:methods",
            ref="E001",
        )
    ]
    signals = MOD.deterministic_sample_unit_evidence(items)
    assert any(signal["sample_unit"].lower() == "single hela cells" for signal in signals)


def test_single_cell_method_phrase_is_not_a_sample_unit():
    items = [
        item(
            "This single-cell proteomics workflow improves sensitivity at single-cell resolution.",
            label="publication:samples:methods",
            ref="E001",
        )
    ]
    assert not MOD.deterministic_sample_unit_evidence(items)


def test_generic_single_cell_normalization_requires_repeated_methods_support():
    result = base_result(biological_sample_unit=None)
    items = [
        item("Single cells were isolated into individual wells.", label="publication:samples:methods", ref="E001"),
        item("Single cells were lysed and digested in the wells.", label="publication:preparation:methods", ref="E002"),
    ]
    signals = MOD.deterministic_sample_unit_evidence(items)
    warnings = MOD.normalize_biological_sample_unit(
        result,
        signals,
        onecell_support=[{"ref": "E001"}, {"ref": "E002"}],
    )
    assert MOD.sample_unit_has_cell_noun(result["biological_sample_unit"])
    assert warnings
    assert "E001" in result["evidence_refs"]["biological_sample_unit"] or "E002" in result["evidence_refs"]["biological_sample_unit"]


def test_generic_repository_single_cell_wording_does_not_fill_sample_unit():
    result = base_result(biological_sample_unit=None)
    items = [
        {
            "ref": "E001",
            "source_kind": "repository",
            "source_label": "dataset_description",
            "text": "Benchmarking multiplexed single cell measurements with an abundant carrier.",
        }
    ]
    signals = MOD.deterministic_sample_unit_evidence(items)
    MOD.normalize_biological_sample_unit(
        result,
        signals,
        onecell_support=[{"ref": "E001"}, {"ref": "E002"}],
    )
    assert result["biological_sample_unit"] is None


def test_sample_unit_normalization_is_blocked_by_direct_nonqualifying_evidence():
    result = base_result(biological_sample_unit=None)
    items = [
        item("Single HeLa cells were discussed in the benchmark.", label="publication:samples:methods", ref="E001")
    ]
    signals = MOD.deterministic_sample_unit_evidence(items)
    MOD.normalize_biological_sample_unit(
        result,
        signals,
        onecell_support=[{"ref": "E001"}, {"ref": "E002"}],
        nonqualifying_signals=[{"ref": "E003", "reason": "commercial_or_standard_digest_input"}],
    )
    assert result["biological_sample_unit"] is None


def test_existing_specific_cell_type_unit_is_preserved():
    result = base_result(biological_sample_unit="human hepatocyte")
    signals = [{"ref": "E001", "sample_unit": "single cell", "specificity": "generic", "source_label": "publication:samples:methods"}]
    warnings = MOD.normalize_biological_sample_unit(result, signals, onecell_support=[{"ref": "E001"}, {"ref": "E002"}])
    assert result["biological_sample_unit"] == "human hepatocyte"
    assert warnings == []

def test_write_tsv_handles_heterogeneous_summary_rows():
    import csv
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "summary.tsv"
        MOD.write_tsv(
            path,
            [
                {"accession": "PXD000001", "run_status": "success", "curation_decision": "include"},
                {"accession": "PXD000002", "run_status": "error", "error": "boom"},
            ],
        )
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle, delimiter="\t"))
        assert len(rows) == 2
        assert rows[0]["curation_decision"] == "include"
        assert rows[1]["error"] == "boom"
        assert "error" in rows[0]


def test_write_tsv_empty_rows_writes_a_header():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "summary.tsv"
        MOD.write_tsv(path, [])
        header = path.read_text(encoding="utf-8").splitlines()[0]
        assert header == "accession\trun_status\tcuration_decision\tdecision_reason\terror"


# v19-shadow-2.6 evidence-enrichment regression tests

def test_single_zygote_lcms_is_onecell_support():
    items = [item(
        "Single zygotes were dispensed into 384-well plates, lysed, digested and analyzed by LC-MS/MS.",
        label="publication:preparation:methods",
    )]
    assert MOD.deterministic_onecell_support(items)
    assert MOD.deterministic_qualifying_branch_evidence(items)


def test_direct_qualifying_branch_overrides_model_only_benchmark_conflict():
    result = base_result(
        biological_sample_unit="single human oocyte",
        benchmark_only="yes",
        true_single_cell_ms_samples_present="no",
        biological_unit_class="cell_population",
    )
    branch = [item(
        "A single human oocyte was isolated into a well and processed for proteomic LC-MS/MS.",
        label="publication:preparation:methods",
    )]
    decision, _ = MOD.deterministic_decision(
        result,
        qualifying_branch_signals=branch,
        onecell_support=branch,
    )
    assert decision == "include"


def test_separate_direct_nonqualifying_control_does_not_veto_real_onecell_branch():
    result = base_result(
        biological_sample_unit="single HeLa cell",
        benchmark_only="yes",
        true_single_cell_ms_samples_present="no",
    )
    branch = [item(
        "A single HeLa cell was isolated into a well and processed for proteomic LC-MS/MS.",
        label="publication:preparation:methods",
    )]
    nonqual = [{"ref": "E002", "reason": "commercial_or_standard_digest_input", "text": "commercial digest"}]
    decision, _ = MOD.deterministic_decision(
        result,
        qualifying_branch_signals=branch,
        onecell_support=branch,
        nonqualifying_signals=nonqual,
    )
    assert decision == "include"


def test_snapshot_extra_evidence_reads_project_files_and_sdrf():
    import json
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project = root / "PXD900001.json"
        files = root / "PXD900001_files.json"
        sdrf = root / "PXD900001.sdrf.tsv"
        project.write_text(json.dumps({
            "accession": "PXD900001",
            "sampleProcessingProtocol": "Individual hepatocytes were isolated into wells for proteomic LC-MS/MS.",
        }), encoding="utf-8")
        files.write_text(json.dumps([
            {"fileName": "single_hepatocyte_001.raw"},
            {"fileName": "notes.txt"},
        ]), encoding="utf-8")
        sdrf.write_text(
            "source name\tcharacteristics[cell type]\tcomment[data file]\n"
            "single_hepatocyte_001\thepatocyte\tsingle_hepatocyte_001.raw\n",
            encoding="utf-8",
        )
        rows = MOD._snapshot_extra_evidence({
            "project_json_path": str(project),
            "files_json_path": str(files),
            "sdrf_path": str(sdrf),
        })
        labels = {row[1] for row in rows}
        assert any("project_json" in label for label in labels)
        assert any("files_json" in label for label in labels)
        assert any("sdrf" in label for label in labels)


def test_explicit_publication_accession_mismatch_blocks_extra_evidence():
    import json
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        evidence = root / "evidence.json"
        evidence.write_text(json.dumps({
            "target_accession": "PXD900001",
            "publication_pride_accessions": ["PXD999999"],
            "publication_source_path": str(root / "paper.pdf"),
            "tasks": {
                "samples": [{"section": "methods", "text": "A single hepatocyte was processed by LC-MS/MS."}],
            },
        }), encoding="utf-8")
        annotation = {
            "target_accession": "PXD900001",
            "provenance": {"semantic_evidence_file": str(evidence)},
        }
        rows, diag = MOD._annotation_extra_evidence(annotation, "PXD900001")
        assert rows == []
        assert diag["explicit_accession_mismatch"] is True


def test_deterministic_evidence_pool_keeps_model_refs_and_adds_extra_refs():
    import json
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project = root / "project.json"
        project.write_text(json.dumps({
            "sampleProcessingProtocol": "Single cardiomyocytes were isolated into wells for proteomic LC-MS/MS."
        }), encoding="utf-8")
        base = [{"ref": "E001", "source_kind": "repository", "source_label": "dataset_title", "text": "Single-cell proteomics"}]
        items, diag = MOD.build_deterministic_evidence_items(
            {"accession": "PXD900001", "project_json_path": str(project), "files_json_path": "", "sdrf_path": ""},
            [],
            base,
        )
        assert any(x["ref"] == "E001" for x in items)
        assert any(x["ref"].startswith("D") for x in items)
        assert diag["deterministic_extra_item_count"] >= 1

if __name__ == "__main__":
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            print(f"PASS {name}")



# v19-shadow-2.6.1 mixed benchmark + real-cell branch regression tests

def test_single_cells_were_lysed_is_high_specificity_qualifying_branch():
    items = [
        {
            "ref": "E001",
            "source_kind": "repository",
            "source_label": "dataset_description",
            "text": (
                "Following embryo dissociation, the single cells were lysed in 1 uL DDM. "
                "The single-cell proteome was denatured and digested before CE-MS analysis."
            ),
        }
    ]
    branches = MOD.deterministic_qualifying_branch_evidence(items)
    assert branches


def test_single_hela_cell_digest_noun_is_not_processing_branch():
    items = [
        {
            "ref": "E001",
            "source_kind": "repository",
            "source_label": "dataset_description",
            "text": "A commercial single HeLa cell digest standard was injected at 250 pg for benchmarking.",
        }
    ]
    assert not MOD.deterministic_qualifying_branch_evidence(items)


def test_real_single_cell_branch_coexists_with_single_cell_equivalent_benchmark():
    items = [
        {
            "ref": "E001",
            "source_kind": "repository",
            "source_label": "dataset_description",
            "text": (
                "The strategy was validated on HeLa proteome digest at single-cell equivalent amounts. "
                "Following embryo dissociation, the single cells were lysed in 1 uL DDM and digested for CE-MS."
            ),
        }
    ]
    branch = MOD.deterministic_qualifying_branch_evidence(items)
    nonqual = MOD.deterministic_nonqualifying_input_evidence(items)
    assert branch
    assert nonqual
    result = base_result(
        biological_sample_unit="Xenopus laevis embryo",
        biological_unit_class="single_cell",
        mixed_design="no",
    )
    MOD.normalize_fact_consistency(result, [], branch, nonqual)
    assert result["mixed_design"] == "yes"
    decision, _ = MOD.deterministic_decision(
        result,
        onecell_support=MOD.deterministic_onecell_support(items),
        qualifying_branch_signals=branch,
        nonqualifying_signals=nonqual,
    )
    assert decision == "include"


def test_sample_unit_normalization_allowed_for_separate_real_branch_in_mixed_design():
    items = [
        {
            "ref": "E001",
            "source_kind": "repository",
            "source_label": "dataset_description",
            "text": "The HeLa proteome digest was measured at a single-cell equivalent amount.",
        },
        {
            "ref": "E002",
            "source_kind": "repository",
            "source_label": "sample_processing_protocol",
            "text": "Following embryo dissociation, the single cells were lysed and digested for CE-MS.",
        },
    ]
    result = base_result(biological_sample_unit="Xenopus laevis embryo")
    branch = MOD.deterministic_qualifying_branch_evidence(items)
    nonqual = MOD.deterministic_nonqualifying_input_evidence(items)
    signals = MOD.deterministic_sample_unit_evidence(items)
    warnings = MOD.normalize_biological_sample_unit(
        result,
        signals,
        onecell_support=MOD.deterministic_onecell_support(items),
        nonqualifying_signals=nonqual,
        qualifying_branch_signals=branch,
    )
    assert result["biological_sample_unit"].lower() == "single cells"
    assert warnings


def test_nonqualifying_input_still_blocks_sample_unit_without_real_branch():
    items = [
        {
            "ref": "E001",
            "source_kind": "repository",
            "source_label": "dataset_description",
            "text": "A commercial HeLa digest was analyzed at single-cell-like amounts.",
        },
        {
            "ref": "E002",
            "source_kind": "repository",
            "source_label": "dataset_description",
            "text": "Single cells are discussed as the intended future application.",
        },
    ]
    result = base_result(biological_sample_unit=None)
    signals = MOD.deterministic_sample_unit_evidence(items)
    MOD.normalize_biological_sample_unit(
        result,
        signals,
        onecell_support=MOD.deterministic_onecell_support(items),
        nonqualifying_signals=MOD.deterministic_nonqualifying_input_evidence(items),
        qualifying_branch_signals=MOD.deterministic_qualifying_branch_evidence(items),
    )
    assert result["biological_sample_unit"] is None
