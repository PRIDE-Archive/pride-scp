#!/usr/bin/env python3
"""
Stage 5: merge screened annotation JSON into catalogue CSVs.

Outputs:
  all_publication_annotations.csv
  single_cell_publication_catalogue.csv
  single_cell_dataset_catalogue.csv
  single_cell_samples.csv
  single_cell_samples_raw_candidates.csv
  low_input_benchmarks.csv
  low_input_benchmarks_raw_candidates.csv
  catalogue_qc_summary.json
  annotation_failures.csv
"""

from __future__ import annotations

import argparse
import html
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any

from pride_scp_pipeline_common import (
    join_unique,
    read_tsv,
    text_value,
    unique_nonempty,
    write_csv,
)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest_tsv")
    parser.add_argument(
        "--annotations-dir",
        default="pride_scp_annotations",
    )
    parser.add_argument(
        "--output-dir",
        default="pride_scp_catalogue",
    )
    parser.add_argument(
        "--metadata-history-dir",
        action="append",
        default=[],
        help=(
            "Optional historical annotation-backup directory. Repeatable. "
            "Historical JSON is used only as metadata recall evidence; it "
            "can never change the current SCP yes/no classification."
        ),
    )
    parser.add_argument(
        "--history-mode",
        choices=("latest", "all", "none"),
        default="latest",
        help=(
            "Historical sample-metadata recovery policy. 'latest' (default) "
            "uses the newest historical annotation version per accession/"
            "publication; 'all' uses every discovered backup; 'none' "
            "disables history recovery."
        ),
    )
    parser.add_argument(
        "--no-auto-history",
        action="store_true",
        help=(
            "Do not auto-discover sibling pre_v*_backup directories under "
            "--annotations-dir. Explicit --metadata-history-dir paths still "
            "apply unless --history-mode none is used."
        ),
    )
    return parser.parse_args()


def json_text(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def list_strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, str):
                if item:
                    out.append(item)
            elif isinstance(item, dict):
                out.append(json_text(item))
        return unique_nonempty(out)
    if isinstance(value, dict):
        return [json_text(value)]
    return [str(value)]


def flatten_annotation(
    manifest_row: dict[str, str],
    annotation: dict[str, Any],
    annotation_path: str,
) -> dict[str, Any]:
    samples = annotation.get("true_single_cell_samples", []) or []
    gate = (
        annotation.get("provenance", {})
        .get("scp_evidence_gate", {})
        or {}
    )
    sample_types = []
    organisms = []
    counts = []

    for sample in samples:
        if not isinstance(sample, dict):
            continue
        if sample.get("sample_type"):
            sample_types.append(sample["sample_type"])
        if sample.get("organism"):
            organisms.append(sample["organism"])
        for count in sample.get("cell_counts_reported", []) or []:
            counts.append(count)

    return {
        "accession": manifest_row.get("accession", ""),
        "dataset_title": manifest_row.get("dataset_title", ""),
        "pride_project_url": manifest_row.get("pride_project_url", ""),
        "pride_ftp_url": manifest_row.get("pride_ftp_url", ""),
        "publication_index": manifest_row.get("publication_index", ""),
        "publication_doi": manifest_row.get("publication_doi", ""),
        "publication_pmid": manifest_row.get("publication_pmid", ""),
        "publication_pmcid": (
            manifest_row.get("resolved_pmcid")
            or manifest_row.get("publication_pmcid", "")
        ),
        "publication_title": manifest_row.get("publication_title", ""),
        "publication_url": manifest_row.get("publication_url", ""),
        "pdf_path": manifest_row.get("pdf_path", ""),
        "scp_screen_decision": manifest_row.get(
            "scp_screen_decision",
            "",
        ),
        "scp_screen_score": manifest_row.get(
            "scp_screen_score",
            "",
        ),
        "scp_screen_reason": manifest_row.get(
            "scp_screen_reason",
            "",
        ),
        "scp_screen_evidence": manifest_row.get(
            "scp_screen_evidence",
            "",
        ),
        "annotation_path": annotation_path,
        "annotation_pipeline_version": annotation.get("annotation_pipeline_version", ""),
        "classification_policy_version": annotation.get(
            "classification_policy_version",
            "",
        ),
        "metadata_qc_version": annotation.get("metadata_qc_version", ""),
        "annotation_model": annotation.get("annotation_model", ""),
        "is_single_cell_proteomics": annotation.get("is_single_cell_proteomics", ""),
        "scp_model_classification": gate.get("model_classification", ""),
        "scp_final_classification": gate.get(
            "final_classification",
            annotation.get("is_single_cell_proteomics", ""),
        ),
        "scp_evidence_supported": gate.get("supported", ""),
        "scp_evidence_tier": gate.get("evidence_tier", ""),
        "scp_evidence_reason": gate.get("reason", ""),
        "scp_support_scope": gate.get("support_scope", ""),
        # Stage 5 v19 recomputes sample_metadata_status deterministically
        # after current + historical metadata candidates are curated.
        "sample_metadata_status": (
            "pending_stage5_qc"
            if text_value(annotation.get("is_single_cell_proteomics")).lower() == "yes"
            else "not_applicable"
        ),
        "sample_metadata_sources": "",
        "stage5_metadata_qc_version": "stage5-v19.1",
        "raw_current_true_single_cell_samples_json": json_text(samples),
        "raw_current_low_input_benchmarks_json": json_text(
            annotation.get("low_input_benchmarks", [])
        ),
        "target_dataset_label": annotation.get("target_dataset_label", ""),
        "accession_scope_kind": annotation.get("accession_scope_kind", ""),
        "sample_types": join_unique(sample_types),
        "organisms": join_unique(organisms),
        "cell_counts_reported": join_unique(counts),
        "true_single_cell_samples_json": json_text(samples),
        "low_input_benchmarks_json": json_text(
            annotation.get("low_input_benchmarks", [])
        ),
        "sample_preparation": annotation.get("sample_preparation", ""),
        "single_cell_isolation_json": json_text(
            annotation.get("single_cell_isolation", [])
        ),
        "labeling_strategy": join_unique(
            list_strings(annotation.get("labeling_strategy"))
        ),
        "mass_spectrometers": join_unique(
            list_strings(annotation.get("mass_spectrometers"))
        ),
        "acquisition_modes": join_unique(
            list_strings(annotation.get("acquisition_modes"))
        ),
        "ion_mobility_or_faims_json": json_text(
            annotation.get("ion_mobility_or_faims", [])
        ),
        "lc_configuration": annotation.get("lc_configuration", ""),
        "lc_gradient": annotation.get("lc_gradient", ""),
        "single_cell_throughput": annotation.get("single_cell_throughput", ""),
        "single_cell_proteome_depth": annotation.get("single_cell_proteome_depth", ""),
        "low_input_proteome_depth": annotation.get("low_input_proteome_depth", ""),
        "analysis_software": join_unique(
            list_strings(annotation.get("analysis_software"))
        ),
        "analysis_strategies": join_unique(
            list_strings(
                annotation.get("analysis_strategies")
                or annotation.get("library_strategy")
            )
        ),
        "catalogue_note": annotation.get("catalogue_note", ""),
        "validation_warnings_json": json_text(
            annotation.get("validation_warnings", [])
        ),
    }


PUBLICATION_FIELDS = [
    "accession",
    "dataset_title",
    "pride_project_url",
    "pride_ftp_url",
    "publication_index",
    "publication_doi",
    "publication_pmid",
    "publication_pmcid",
    "publication_title",
    "publication_url",
    "pdf_path",
    "scp_screen_decision",
    "scp_screen_score",
    "scp_screen_reason",
    "scp_screen_evidence",
    "annotation_path",
    "annotation_pipeline_version",
    "classification_policy_version",
    "metadata_qc_version",
    "stage5_metadata_qc_version",
    "annotation_model",
    "is_single_cell_proteomics",
    "scp_model_classification",
    "scp_final_classification",
    "scp_evidence_supported",
    "scp_evidence_tier",
    "scp_evidence_reason",
    "scp_support_scope",
    "sample_metadata_status",
    "sample_metadata_sources",
    "raw_sample_candidate_count",
    "target_dataset_label",
    "accession_scope_kind",
    "sample_types",
    "organisms",
    "cell_counts_reported",
    "true_single_cell_samples_json",
    "raw_current_true_single_cell_samples_json",
    "low_input_benchmarks_json",
    "raw_current_low_input_benchmarks_json",
    "sample_preparation",
    "single_cell_isolation_json",
    "labeling_strategy",
    "mass_spectrometers",
    "acquisition_modes",
    "ion_mobility_or_faims_json",
    "lc_configuration",
    "lc_gradient",
    "single_cell_throughput",
    "single_cell_proteome_depth",
    "low_input_proteome_depth",
    "analysis_software",
    "analysis_strategies",
    "catalogue_note",
    "validation_warnings_json",
]


def merge_field(rows: list[dict[str, Any]], field: str) -> str:
    return join_unique(row.get(field, "") for row in rows)


def dataset_sample_metadata_status(rows: list[dict[str, Any]]) -> str:
    statuses = {
        text_value(row.get("sample_metadata_status"))
        for row in rows
        if text_value(row.get("sample_metadata_status"))
    }

    if not statuses:
        return "not_reliably_extracted"
    if statuses == {"validated_current"}:
        return "validated_current"
    if statuses == {"validated_current_plus_history"}:
        return "validated_current_plus_history"
    if statuses == {"recovered_from_history"}:
        return "recovered_from_history"
    if statuses == {"title_derived"}:
        return "title_derived"
    if statuses <= {"validated_current", "validated_current_plus_history"}:
        return "validated_current_plus_history"
    if "validated_current" in statuses or "validated_current_plus_history" in statuses:
        return "partial"
    if "recovered_from_history" in statuses:
        return "recovered_from_history"
    if "title_derived" in statuses:
        return "title_derived"
    if "not_reliably_extracted" in statuses:
        return "not_reliably_extracted"
    return "not_applicable"


def dataset_summary(accession: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "accession": accession,
        "dataset_title": merge_field(rows, "dataset_title"),
        "pride_project_url": merge_field(rows, "pride_project_url"),
        "pride_ftp_url": merge_field(rows, "pride_ftp_url"),
        "publication_dois": merge_field(rows, "publication_doi"),
        "publication_titles": merge_field(rows, "publication_title"),
        "publication_urls": merge_field(rows, "publication_url"),
        "n_supporting_publications": len(rows),
        "annotation_pipeline_versions": merge_field(
            rows,
            "annotation_pipeline_version",
        ),
        "classification_policy_versions": merge_field(
            rows,
            "classification_policy_version",
        ),
        "metadata_qc_versions": merge_field(rows, "metadata_qc_version"),
        "stage5_metadata_qc_versions": merge_field(
            rows,
            "stage5_metadata_qc_version",
        ),
        "annotation_models": merge_field(rows, "annotation_model"),
        "scp_evidence_tiers": merge_field(rows, "scp_evidence_tier"),
        "scp_evidence_reasons": merge_field(rows, "scp_evidence_reason"),
        "scp_support_scopes": merge_field(rows, "scp_support_scope"),
        "sample_metadata_status": dataset_sample_metadata_status(rows),
        "sample_metadata_sources": merge_field(rows, "sample_metadata_sources"),
        "curated_sample_count": sum(
            len(parse_json_list(row.get("true_single_cell_samples_json")))
            for row in rows
        ),
        "raw_sample_candidate_count": sum(
            int(row.get("raw_sample_candidate_count") or 0)
            for row in rows
        ),
        "low_input_benchmark_count": sum(
            len(parse_json_list(row.get("low_input_benchmarks_json")))
            for row in rows
        ),
        "target_dataset_labels": merge_field(rows, "target_dataset_label"),
        "sample_types": merge_field(rows, "sample_types"),
        "organisms": merge_field(rows, "organisms"),
        "cell_counts_reported": merge_field(rows, "cell_counts_reported"),
        "sample_preparation": merge_field(rows, "sample_preparation"),
        "single_cell_isolation_json": merge_field(rows, "single_cell_isolation_json"),
        "labeling_strategy": merge_field(rows, "labeling_strategy"),
        "mass_spectrometers": merge_field(rows, "mass_spectrometers"),
        "acquisition_modes": merge_field(rows, "acquisition_modes"),
        "ion_mobility_or_faims_json": merge_field(rows, "ion_mobility_or_faims_json"),
        "lc_configuration": merge_field(rows, "lc_configuration"),
        "lc_gradient": merge_field(rows, "lc_gradient"),
        "single_cell_throughput": merge_field(rows, "single_cell_throughput"),
        "single_cell_proteome_depth": merge_field(rows, "single_cell_proteome_depth"),
        "low_input_proteome_depth": merge_field(rows, "low_input_proteome_depth"),
        "analysis_software": merge_field(rows, "analysis_software"),
        "analysis_strategies": merge_field(rows, "analysis_strategies"),
        "catalogue_notes": merge_field(rows, "catalogue_note"),
        "source_annotation_paths": merge_field(rows, "annotation_path"),
    }


DATASET_FIELDS = [
    "accession",
    "dataset_title",
    "pride_project_url",
    "pride_ftp_url",
    "publication_dois",
    "publication_titles",
    "publication_urls",
    "n_supporting_publications",
    "annotation_pipeline_versions",
    "classification_policy_versions",
    "metadata_qc_versions",
    "stage5_metadata_qc_versions",
    "annotation_models",
    "scp_evidence_tiers",
    "scp_evidence_reasons",
    "scp_support_scopes",
    "sample_metadata_status",
    "sample_metadata_sources",
    "curated_sample_count",
    "raw_sample_candidate_count",
    "low_input_benchmark_count",
    "target_dataset_labels",
    "sample_types",
    "organisms",
    "cell_counts_reported",
    "sample_preparation",
    "single_cell_isolation_json",
    "labeling_strategy",
    "mass_spectrometers",
    "acquisition_modes",
    "ion_mobility_or_faims_json",
    "lc_configuration",
    "lc_gradient",
    "single_cell_throughput",
    "single_cell_proteome_depth",
    "low_input_proteome_depth",
    "analysis_software",
    "analysis_strategies",
    "catalogue_notes",
    "source_annotation_paths",
]



# ---------------------------------------------------------------------------
# Stage-5 v19.1 deterministic metadata QC
# ---------------------------------------------------------------------------

STAGE5_METADATA_QC_VERSION = "stage5-v19.1"

PXD_RE = re.compile(r"\bPXD\d{6,}\b", re.I)
DOI_NORMALIZE_RE = re.compile(r"^https?://(?:dx\.)?doi\.org/", re.I)
MASS_AMOUNT_RE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:pg|ng|ug|µg|μg|mg)\b",
    re.I,
)
CONCENTRATION_RE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:pg|ng|ug|µg|μg|mg)\s*"
    r"(?:/\s*(?:u|µ|μ)?l|(?:u|µ|μ)l\s*[−–-]?\s*1\b|per\s+(?:u|µ|μ)?l)",
    re.I,
)
AMOUNT_ONLY_RE = re.compile(
    r"^[~<>≈∼]?\s*\d+(?:[.,]\d+)?\s*(?:pg|ng|ug|µg|μg|mg)\s*$",
    re.I,
)
DENSITY_ONLY_RE = re.compile(
    r"^[~<>≈∼]?\s*\d+(?:[.,]\d+)?\s*(?:[×x]\s*10\s*\^?\s*\d+)?"
    r"\s*cells?\s*(?:/|per)\s*(?:ml|µl|μl|ul)\s*$",
    re.I,
)

REFERENCE_SAMPLE_RE = re.compile(
    r"\b(?:et\s+al\.?|doi\b|journal\b|volume\b|issue\b|pages?\b|"
    r"published\b|publisher\b|an\s+automated\b|workflow\s+for\b|"
    r"proteomics\s+sample\s+preparation\b|quantitative\s+multiplexed\b)"
    r"|^[A-Z]\.\s+",
    re.I,
)

SCIENTIFIC_ABBREVIATION_SAMPLE_RE = re.compile(
    r"^[A-Z]\.\s*[a-z][a-z.-]+(?:\s+(?:cells?|bacteria|bacterium))?\b",
)
PROCEDURAL_SAMPLE_RE = re.compile(
    r"^(?:FACS|flow[- ]cytometry)[- ]sorted\s+cell\s+populations?$"
    r"|^sorted\s+cell\s+populations?$"
    r"|^cell\s+populations?$"
    r"|^(?:stage[- ]?\d+\s+)?embryos?$",
    re.I,
)
GENERIC_SAMPLE_RE = re.compile(
    r"^(?:single\s+)?(?:individual\s+)?cells?$"
    r"|^(?:human|mouse|murine|rat|yeast)?\s*cell\s+lines?$"
    r"|^samples?$|^sample\s+types?$|^roots?$|^shoots?$|^tissues?$|^organs?$",
    re.I,
)
CONDITION_ONLY_RE = re.compile(
    r"^(?:WT|wild[- ]type|mutant|control|cas[-A-Za-z0-9_.]+)\b"
    r"|\bp[A-Z0-9_.-]+:[A-Z0-9_.-]+\b"
    r"|\b(?:GFP|mCherry|RFP|YFP)\b",
    re.I,
)
BIOLOGICAL_UNIT_RE = re.compile(
    r"\b(?:cells?|oocytes?|neurons?|blastomeres?|bacteri(?:um|a)|"
    r"sperm(?:\s+cells?)?|egg\s+cells?|hair\s+cells?|progenitor\s+cells?|"
    r"hepatocytes?|lymphocytes?|monocytes?|macrophages?|fibroblasts?)\b",
    re.I,
)

KNOWN_CELL_LINE_ORGANISM = {
    "hela": "Homo sapiens",
    "a549": "Homo sapiens",
    "h460": "Homo sapiens",
    "jurkat": "Homo sapiens",
    "mm 1s": "Homo sapiens",
    "hucct 1": "Homo sapiens",
    "rbe": "Homo sapiens",
    "egi 1": "Homo sapiens",
    "mia paca2": "Homo sapiens",
    "panc 1": "Homo sapiens",
    "aspc 1": "Homo sapiens",
    "oci aml8227": "Homo sapiens",
    "nhc": "Homo sapiens",
    "u87": "Homo sapiens",
}

ORGANISM_ALIASES = (
    ("Homo sapiens", re.compile(r"\b(?:Homo\s+sapiens|human)\b", re.I)),
    ("Mus musculus", re.compile(r"\b(?:Mus\s+musculus|mouse|murine)\b", re.I)),
    ("Oryza sativa", re.compile(r"\b(?:Oryza\s+sativa|rice)\b", re.I)),
    ("Arabidopsis thaliana", re.compile(r"\bArabidopsis\s+thaliana\b", re.I)),
    ("Xenopus laevis", re.compile(r"\bXenopus\s+laevis\b", re.I)),
    (
        "Saccharomyces cerevisiae",
        re.compile(r"\b(?:Saccharomyces\s+cerevisiae|yeast)\b", re.I),
    ),
    (
        "Escherichia coli",
        re.compile(r"\b(?:Escherichia\s+coli|E\.\s*coli)\b", re.I),
    ),
    (
        "Caenorhabditis elegans",
        re.compile(r"\b(?:Caenorhabditis\s+elegans|C\.\s*elegans)\b", re.I),
    ),
    ("Danio rerio", re.compile(r"\b(?:Danio\s+rerio|zebrafish)\b", re.I)),
    ("Rattus norvegicus", re.compile(r"\b(?:Rattus\s+norvegicus|rat)\b", re.I)),
)


def parse_json_list(value: Any) -> list[Any]:
    if value in (None, "", []):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def normalize_doi(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = DOI_NORMALIZE_RE.sub("", value.strip())
    return text.rstrip(".,;)").lower()


def version_number(value: Any) -> int:
    if not isinstance(value, str):
        return -1
    match = re.search(r"(?:^|\b)v(\d+)(?:\b|$)", value, re.I)
    return int(match.group(1)) if match else -1


def normalize_sample_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = html.unescape(value)
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[\u2010\u2011\u2012\u2013\u2014\u2212]", "-", text)
    text = re.sub(r"\s+", " ", text).strip(" ,;:.\t\n")
    text = re.sub(r"^ABSTRACT\s*:\s*", "", text, flags=re.I)
    text = re.sub(r"^\d[\d,]*\s+(?=(?:single\s+)?(?:cells?|oocytes?|neurons?|blastomeres?))", "", text, flags=re.I)
    text = re.sub(r"^(?:one|two|three|four|five)\s+(?:different\s+)?(?=(?:neurons?|cells?|oocytes?|blastomeres?))", "", text, flags=re.I)
    text = re.sub(r"^single\s+(?=(?:HeLa|A549|H460|Jurkat|oocytes?|neurons?|cells?))", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()

    aliases = {
        "hela": "HeLa cells",
        "hela cell": "HeLa cells",
        "hela cells": "HeLa cells",
        "a549": "A549 cells",
        "a549 cell": "A549 cells",
        "a549 cells": "A549 cells",
        "a459": "A549 cells",
        "a459 cells": "A549 cells",
        "h460": "H460 cells",
        "h460 cells": "H460 cells",
        "jurkat": "Jurkat cells",
        "jurkat cells": "Jurkat cells",
        "single oocytes": "oocytes",
        "single oocyte": "oocytes",
        "oocyte": "oocytes",
        "progenitor-enriched single cells": "progenitor-enriched cells",
    }
    return aliases.get(text.lower(), text)


def sample_key(value: Any) -> str:
    text = normalize_sample_text(value).lower()
    text = re.sub(r"\b(?:single|individual|cells?|cell\s+line)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def sample_qc(value: Any) -> tuple[str | None, str]:
    name = normalize_sample_text(value)
    if not name:
        return None, "empty"
    if len(name) > 110 or len(name.split()) > 12:
        return None, "sentence_like_or_too_long"
    if PXD_RE.search(name):
        return None, "repository_identifier"
    if (
        REFERENCE_SAMPLE_RE.search(name)
        and not SCIENTIFIC_ABBREVIATION_SAMPLE_RE.match(name)
    ):
        return None, "reference_or_method_fragment"
    if PROCEDURAL_SAMPLE_RE.fullmatch(name):
        return None, "procedural_or_source_material_label"
    if GENERIC_SAMPLE_RE.fullmatch(name):
        return None, "generic_non_sample_label"
    if AMOUNT_ONLY_RE.fullmatch(name) or DENSITY_ONLY_RE.fullmatch(name):
        return None, "quantity_or_density_not_sample"
    if re.search(r"\b\d+(?:[.,]\d+)?\s*(?:pg|ng|ug|µg|μg|mg)\b", name, re.I):
        return None, "input_amount_not_sample"
    if ";" in name:
        return None, "compound_or_reference_fragment"
    if CONDITION_ONLY_RE.search(name) and BIOLOGICAL_UNIT_RE.search(name) is None:
        return None, "condition_or_genotype_only"

    key = sample_key(name)
    has_biological_unit = BIOLOGICAL_UNIT_RE.search(name) is not None

    if (
        len(key) <= 4
        and key not in KNOWN_CELL_LINE_ORGANISM
        and not has_biological_unit
    ):
        return None, "cryptic_short_label"

    if not has_biological_unit and key not in KNOWN_CELL_LINE_ORGANISM:
        return None, "no_biological_cell_unit"

    return name, "accepted"


def canonical_organism(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = re.sub(r"\s+", " ", value.strip())
    if not text:
        return ""
    lower = text.lower()
    bad = (
        "cell", "tissue", "plasma", "serum", "blood", "control",
        "sample", "digest", "proteome", "patient", "organoid",
    )
    if any(token in lower for token in bad):
        return ""
    for canonical, pattern in ORGANISM_ALIASES:
        if pattern.fullmatch(text) or pattern.search(text):
            return canonical
    if re.fullmatch(r"[A-Z][a-z]+\s+[a-z][A-Za-z0-9._-]+", text):
        return text
    return ""


def organism_hits(text: Any) -> set[str]:
    if not isinstance(text, str):
        return set()
    return {
        canonical
        for canonical, pattern in ORGANISM_ALIASES
        if pattern.search(text)
    }


def known_cell_line_organism(sample_name: str) -> str:
    key = sample_key(sample_name)
    if key in KNOWN_CELL_LINE_ORGANISM:
        return KNOWN_CELL_LINE_ORGANISM[key]

    padded = f" {key} "
    for cell_line, organism in KNOWN_CELL_LINE_ORGANISM.items():
        if f" {cell_line} " in padded:
            return organism

    if "hps" in key:
        return "Homo sapiens"
    return ""


def resolve_sample_organism(
    sample_name: str,
    raw_organisms: list[str],
    *,
    dataset_title: str,
    publication_title: str,
) -> tuple[str, str, str]:
    claims = {
        canonical_organism(value)
        for value in raw_organisms
        if canonical_organism(value)
    }

    known = known_cell_line_organism(sample_name)
    if known:
        note = ""
        if claims and claims != {known}:
            note = "conflicting_annotation_organism_overridden_by_known_cell_line"
        return known, "known_cell_line", note

    sample_hits = organism_hits(sample_name)
    if len(sample_hits) == 1:
        organism = next(iter(sample_hits))
        return organism, "sample_name", ""

    title_hits = organism_hits(f"{dataset_title} {publication_title}")
    if len(title_hits) == 1:
        organism = next(iter(title_hits))
        note = ""
        if claims and claims != {organism}:
            note = "conflicting_annotation_organism_overridden_by_title"
        return organism, "dataset_or_publication_title", note

    if len(claims) == 1:
        return next(iter(claims)), "unanimous_annotation_claim", ""

    if len(claims) > 1:
        return "", "unresolved", "conflicting_annotation_organisms_removed"

    return "", "unresolved", ""


def valid_input_amount(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = re.sub(r"\s+", " ", value.strip())
    if not MASS_AMOUNT_RE.search(text):
        return False
    if CONCENTRATION_RE.search(text):
        return False
    return True


def benchmark_qc(sample: Any, input_amount: Any) -> tuple[bool, str]:
    sample_text = normalize_sample_text(sample)
    amount = re.sub(r"\s+", " ", str(input_amount or "").strip())

    if not valid_input_amount(amount):
        return False, "missing_or_non_total_input_amount"
    if not sample_text:
        return False, "missing_sample_material"
    if AMOUNT_ONLY_RE.fullmatch(sample_text):
        return False, "sample_is_only_input_amount"
    if DENSITY_ONLY_RE.fullmatch(sample_text):
        return False, "sample_is_culture_density"
    if REFERENCE_SAMPLE_RE.search(sample_text):
        return False, "reference_or_method_fragment"
    return True, "accepted"


def explicit_title_organism(row: dict[str, Any]) -> str:
    """
    Return an organism only when the authoritative dataset/publication title
    contains exactly one explicit organism signal.
    """
    title_text = (
        f"{text_value(row.get('dataset_title'))} "
        f"{text_value(row.get('publication_title'))}"
    )
    hits = organism_hits(title_text)
    if len(hits) == 1:
        return next(iter(hits))
    return ""


def derive_title_sample_candidates(
    row: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Very conservative metadata fallback for an already-confirmed SCP row.

    This is used only when current + historical annotation metadata produced
    zero accepted biological samples. It never changes SCP classification.

    Supported title-derived units are intentionally narrow:
      * oocytes
      * neurons, only with explicit "single neuron" wording
      * hepatocytes, only in explicit single-cell MS/proteomics context

    Generic "cells", "in vivo cells", method titles, or general SCP wording do
    not generate a sample.
    """
    if text_value(row.get("is_single_cell_proteomics")).lower() != "yes":
        return []

    dataset_title = text_value(row.get("dataset_title"))
    publication_title = text_value(row.get("publication_title"))
    combined = f"{dataset_title} {publication_title}"
    lower = combined.lower()

    has_single_cell_context = re.search(
        r"\bsingle[- ]cell\b|\bsingle\s+neuron\b",
        lower,
        re.I,
    ) is not None

    has_proteomics_ms_context = re.search(
        r"\bproteom(?:e|es|ics|ic)\b|"
        r"\bmass\s+spectrom(?:etry|etric)\b|"
        r"\bms/ms\b|"
        r"\bmulti[- ]omics\b",
        lower,
        re.I,
    ) is not None

    if not (has_single_cell_context and has_proteomics_ms_context):
        return []

    organism = explicit_title_organism(row)
    candidates = []

    def add(sample_type: str, rule: str, raw_organism: str = "") -> None:
        candidates.append(
            {
                "accession": row["accession"],
                "publication_doi": row["publication_doi"],
                "publication_title": row["publication_title"],
                "dataset_title": row["dataset_title"],
                "source_kind": "title_derived",
                "candidate_origin": rule,
                "source_annotation_version": STAGE5_METADATA_QC_VERSION,
                "source_annotation_path": "",
                "raw_sample_type": sample_type,
                "raw_organism": raw_organism,
                "raw_cell_counts_reported": "",
            }
        )

    # Explicit oocyte biological unit. "Human oocyte" also yields organism.
    if re.search(r"\boocytes?\b", lower, re.I):
        add(
            "oocytes",
            "authoritative_title_oocyte",
            organism,
        )
        return candidates

    # Require the direct phrase "single neuron"; do not infer neurons merely
    # from a neuroscience context.
    if re.search(r"\bsingle\s+neurons?\b", lower, re.I):
        add(
            "neurons",
            "authoritative_title_single_neuron",
            organism,
        )
        return candidates

    # Hepatocyte fallback is accepted only when the title itself describes
    # single-cell MS/proteomics rather than generic liver biology.
    if (
        re.search(r"\bhepatocytes?\b", lower, re.I)
        and re.search(
            r"\bsingle[- ]cell\b.{0,100}"
            r"(?:mass\s+spectrom(?:etry|etric)|proteom)",
            lower,
            re.I,
        )
    ):
        add(
            "hepatocytes",
            "authoritative_title_hepatocyte",
            organism,
        )
        return candidates

    return []


def discover_history_dirs(
    annotations_root: Path,
    explicit_dirs: list[str],
    *,
    auto: bool,
) -> list[Path]:
    dirs: list[Path] = []

    for value in explicit_dirs:
        path = Path(value).expanduser()
        if path.is_dir():
            dirs.append(path.resolve())

    if auto and annotations_root.is_dir():
        for path in annotations_root.iterdir():
            name = path.name.lower()
            if path.is_dir() and name.startswith("pre_v") and "backup" in name:
                dirs.append(path.resolve())

    seen = set()
    unique = []
    for path in dirs:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return sorted(unique)


def load_history_index(
    history_dirs: list[Path],
    *,
    mode: str,
) -> tuple[dict[tuple[str, str], list[dict[str, Any]]], list[dict[str, Any]]]:
    if mode == "none":
        return {}, []

    all_records: list[dict[str, Any]] = []

    for root in history_dirs:
        for path in sorted(root.rglob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            accession = text_value(data.get("target_accession")).upper()
            if not accession or not accession.startswith("PXD"):
                continue
            doi = normalize_doi(data.get("publication_doi"))
            all_records.append(
                {
                    "accession": accession,
                    "publication_doi": doi,
                    "version": text_value(data.get("annotation_pipeline_version")),
                    "version_number": version_number(data.get("annotation_pipeline_version")),
                    "path": str(path.resolve()),
                    "annotation": data,
                }
            )

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in all_records:
        grouped[(record["accession"], record["publication_doi"])].append(record)

    selected: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for key, records in grouped.items():
        records.sort(
            key=lambda item: (
                item["version_number"],
                item["path"],
            ),
            reverse=True,
        )
        if mode == "latest":
            newest = records[0]["version_number"]
            selected[key] = [
                item for item in records
                if item["version_number"] == newest
            ]
        else:
            selected[key] = records

    return selected, all_records


def matching_history_records(
    index: dict[tuple[str, str], list[dict[str, Any]]],
    accession: str,
    publication_doi: str,
) -> list[dict[str, Any]]:
    doi = normalize_doi(publication_doi)
    exact = index.get((accession.upper(), doi), [])
    if exact:
        return exact
    # DOI-less fallback is only used when exactly one historical publication
    # exists for the accession, avoiding cross-publication contamination.
    candidates = []
    for (acc, _doi), records in index.items():
        if acc == accession.upper():
            candidates.extend(records)
    if len(candidates) == 1:
        return candidates
    return []


def append_sample_candidates(
    candidates: list[dict[str, Any]],
    *,
    row: dict[str, Any],
    annotation: dict[str, Any],
    source_kind: str,
    source_path: str,
    source_version: str,
) -> None:
    def add(raw_name, raw_organism, counts, origin):
        candidates.append(
            {
                "accession": row["accession"],
                "publication_doi": row["publication_doi"],
                "publication_title": row["publication_title"],
                "dataset_title": row["dataset_title"],
                "source_kind": source_kind,
                "candidate_origin": origin,
                "source_annotation_version": source_version,
                "source_annotation_path": source_path,
                "raw_sample_type": raw_name or "",
                "raw_organism": raw_organism or "",
                "raw_cell_counts_reported": join_unique(counts or []),
            }
        )

    for sample in annotation.get("true_single_cell_samples", []) or []:
        if isinstance(sample, dict):
            add(
                sample.get("sample_type"),
                sample.get("organism"),
                sample.get("cell_counts_reported", []) or [],
                "true_single_cell_samples",
            )

    for group in annotation.get("single_cell_isolation", []) or []:
        if not isinstance(group, dict):
            continue
        for sample_name in group.get("samples", []) or []:
            add(sample_name, "", [], "single_cell_isolation")

    # A small model sometimes misfiles a genuine biological cell label as a
    # low-input benchmark while leaving input_amount empty. Such rows cannot
    # be valid low-input benchmarks, so expose them as sample candidates for
    # deterministic QC instead of silently discarding them.
    for benchmark in annotation.get("low_input_benchmarks", []) or []:
        if not isinstance(benchmark, dict):
            continue
        if valid_input_amount(benchmark.get("input_amount")):
            continue
        add(
            benchmark.get("sample"),
            benchmark.get("organism"),
            [],
            "misfiled_low_input_without_amount",
        )


def curate_sample_candidates(
    row: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, str]:
    accepted_by_key: dict[str, dict[str, Any]] = {}
    raw_rows = []

    for candidate in candidates:
        curated_name, reason = sample_qc(candidate.get("raw_sample_type"))
        raw = dict(candidate)
        raw["curated_sample_type"] = curated_name or ""
        raw["candidate_outcome"] = "accepted" if curated_name else "rejected"
        raw["qc_reason"] = reason
        raw_rows.append(raw)

        if not curated_name:
            continue

        key = sample_key(curated_name)
        entry = accepted_by_key.setdefault(
            key,
            {
                "sample_type": curated_name,
                "raw_organisms": [],
                "cell_counts_reported": [],
                "source_kinds": [],
                "source_versions": [],
                "source_paths": [],
                "origins": [],
            },
        )
        if candidate.get("raw_organism"):
            entry["raw_organisms"].append(candidate["raw_organism"])
        counts = [
            item.strip()
            for item in str(candidate.get("raw_cell_counts_reported") or "").split(";")
            if item.strip()
        ]
        entry["cell_counts_reported"].extend(counts)
        entry["source_kinds"].append(candidate["source_kind"])
        entry["source_versions"].append(candidate["source_annotation_version"])
        entry["source_paths"].append(candidate["source_annotation_path"])
        entry["origins"].append(candidate["candidate_origin"])

    curated = []
    for key, entry in sorted(accepted_by_key.items(), key=lambda item: item[1]["sample_type"].lower()):
        organism, resolution, organism_note = resolve_sample_organism(
            entry["sample_type"],
            entry["raw_organisms"],
            dataset_title=row["dataset_title"],
            publication_title=row["publication_title"],
        )
        source_kinds = unique_nonempty(entry["source_kinds"])
        source_versions = unique_nonempty(entry["source_versions"])
        source_paths = unique_nonempty(entry["source_paths"])
        origins = unique_nonempty(entry["origins"])
        counts = unique_nonempty(entry["cell_counts_reported"])

        curated.append(
            {
                "sample_type": entry["sample_type"],
                "organism": organism,
                "cell_counts_reported": counts,
                "organism_resolution": resolution,
                "sample_metadata_sources": join_unique(source_kinds),
                "source_annotation_versions": join_unique(source_versions),
                "source_annotation_paths": join_unique(source_paths),
                "candidate_origins": join_unique(origins),
                "qc_notes": organism_note,
            }
        )

    if not curated:
        status = "not_reliably_extracted"
        sources = ""
    else:
        used_kinds = {
            item
            for sample in curated
            for item in sample["sample_metadata_sources"].split("; ")
            if item
        }
        has_current = any(kind.startswith("current") for kind in used_kinds)
        has_history = any(kind.startswith("history") for kind in used_kinds)
        has_title = "title_derived" in used_kinds
        if has_current and has_history:
            status = "validated_current_plus_history"
        elif has_current:
            status = "validated_current"
        elif has_history:
            status = "recovered_from_history"
        elif has_title:
            status = "title_derived"
        else:
            status = "not_reliably_extracted"
        sources = join_unique(sorted(used_kinds))

    return curated, raw_rows, status, sources


def curate_benchmarks(
    row: dict[str, Any],
    annotation: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    curated = []
    raw_rows = []

    for index, benchmark in enumerate(annotation.get("low_input_benchmarks", []) or [], start=1):
        if not isinstance(benchmark, dict):
            continue
        sample = benchmark.get("sample", "")
        amount = benchmark.get("input_amount", "")
        accepted, reason = benchmark_qc(sample, amount)
        organism, _, _ = resolve_sample_organism(
            sample,
            [benchmark.get("organism", "")],
            dataset_title=row["dataset_title"],
            publication_title=row["publication_title"],
        )

        raw_rows.append(
            {
                "accession": row["accession"],
                "publication_doi": row["publication_doi"],
                "publication_title": row["publication_title"],
                "raw_benchmark_index": index,
                "raw_sample": sample,
                "raw_organism": benchmark.get("organism", ""),
                "raw_input_amount": amount,
                "candidate_outcome": "accepted" if accepted else "rejected",
                "qc_reason": reason,
                "annotation_pipeline_version": row["annotation_pipeline_version"],
                "annotation_path": row["annotation_path"],
            }
        )

        if accepted:
            curated.append(
                {
                    "sample": normalize_sample_text(sample),
                    "organism": organism,
                    "input_amount": amount,
                    "qc_status": "validated_deterministic",
                }
            )

    return curated, raw_rows

def main():
    args = parse_args()
    manifest_rows = read_tsv(Path(args.manifest_tsv))
    output_dir = Path(args.output_dir)
    annotations_root = Path(args.annotations_dir)
    status_dir = annotations_root / "status"
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest_lookup = {
        row_number: row
        for row_number, row in enumerate(manifest_rows)
    }

    annotation_rows = []
    failures = []
    current_annotations: dict[str, dict[str, Any]] = {}

    status_files = sorted(status_dir.glob("*.json")) if status_dir.exists() else []

    for status_path in status_files:
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except Exception as exc:
            failures.append(
                {
                    "status_file": str(status_path),
                    "accession": "",
                    "publication_doi": "",
                    "status": "invalid_status_json",
                    "error": f"{type(exc).__name__}: {exc}",
                    "log_path": "",
                }
            )
            continue

        manifest_row_number = status.get("manifest_row_number")
        row = manifest_lookup.get(manifest_row_number, {})
        annotation_path = Path(status.get("annotation_path", ""))

        if status.get("status") not in {"success", "already_exists"}:
            failures.append(
                {
                    "status_file": str(status_path),
                    "accession": status.get("accession", ""),
                    "publication_doi": status.get("publication_doi", ""),
                    "status": status.get("status", ""),
                    "error": status.get("error", ""),
                    "log_path": status.get("log_path", ""),
                }
            )
            continue

        if not annotation_path.is_file():
            failures.append(
                {
                    "status_file": str(status_path),
                    "accession": status.get("accession", ""),
                    "publication_doi": status.get("publication_doi", ""),
                    "status": "missing_annotation_json",
                    "error": str(annotation_path),
                    "log_path": status.get("log_path", ""),
                }
            )
            continue

        try:
            annotation = json.loads(annotation_path.read_text(encoding="utf-8"))
        except Exception as exc:
            failures.append(
                {
                    "status_file": str(status_path),
                    "accession": status.get("accession", ""),
                    "publication_doi": status.get("publication_doi", ""),
                    "status": "invalid_annotation_json",
                    "error": f"{type(exc).__name__}: {exc}",
                    "log_path": status.get("log_path", ""),
                }
            )
            continue

        resolved_path = str(annotation_path.resolve())
        current_annotations[resolved_path] = annotation
        annotation_rows.append(
            flatten_annotation(row, annotation, resolved_path)
        )

    annotation_rows.sort(
        key=lambda x: (x["accession"], x["publication_index"])
    )

    positives = [
        row
        for row in annotation_rows
        if text_value(row.get("is_single_cell_proteomics")).lower() == "yes"
    ]

    # Historical annotations are metadata recall only. Current v17/v18 yes/no
    # decisions above remain the sole classification authority.
    history_dirs = discover_history_dirs(
        annotations_root,
        args.metadata_history_dir,
        auto=not args.no_auto_history,
    )
    if args.history_mode == "none":
        history_dirs = []

    history_index, history_records = load_history_index(
        history_dirs,
        mode=args.history_mode,
    )

    sample_rows = []
    raw_sample_rows = []
    benchmark_rows = []
    raw_benchmark_rows = []

    for row in positives:
        current_annotation = current_annotations[row["annotation_path"]]
        candidates: list[dict[str, Any]] = []

        append_sample_candidates(
            candidates,
            row=row,
            annotation=current_annotation,
            source_kind="current",
            source_path=row["annotation_path"],
            source_version=row["annotation_pipeline_version"],
        )

        for history in matching_history_records(
            history_index,
            row["accession"],
            row["publication_doi"],
        ):
            append_sample_candidates(
                candidates,
                row=row,
                annotation=history["annotation"],
                source_kind="history",
                source_path=history["path"],
                source_version=history["version"],
            )

        curated_samples, raw_candidates, status, sources = curate_sample_candidates(
            row,
            candidates,
        )

        if not curated_samples:
            title_candidates = derive_title_sample_candidates(row)
            if title_candidates:
                candidates.extend(title_candidates)
                (
                    curated_samples,
                    raw_candidates,
                    status,
                    sources,
                ) = curate_sample_candidates(
                    row,
                    candidates,
                )

        raw_sample_rows.extend(raw_candidates)

        row["sample_metadata_status"] = status
        row["sample_metadata_sources"] = sources
        row["stage5_metadata_qc_version"] = STAGE5_METADATA_QC_VERSION
        row["raw_sample_candidate_count"] = len(candidates)
        row["sample_types"] = join_unique(
            sample["sample_type"] for sample in curated_samples
        )
        row["organisms"] = join_unique(
            sample["organism"] for sample in curated_samples
        )
        row["cell_counts_reported"] = join_unique(
            count
            for sample in curated_samples
            for count in sample["cell_counts_reported"]
        )
        row["true_single_cell_samples_json"] = json_text(curated_samples)

        for sample_index, sample in enumerate(curated_samples, start=1):
            sample_rows.append(
                {
                    "accession": row["accession"],
                    "publication_doi": row["publication_doi"],
                    "publication_title": row["publication_title"],
                    "sample_index": sample_index,
                    "sample_type": sample["sample_type"],
                    "organism": sample["organism"],
                    "organism_resolution": sample["organism_resolution"],
                    "cell_counts_reported": join_unique(sample["cell_counts_reported"]),
                    "sample_metadata_status": status,
                    "sample_metadata_sources": sample["sample_metadata_sources"],
                    "source_annotation_versions": sample["source_annotation_versions"],
                    "candidate_origins": sample["candidate_origins"],
                    "qc_notes": sample["qc_notes"],
                    "scp_evidence_tier": row["scp_evidence_tier"],
                    "current_annotation_path": row["annotation_path"],
                    "source_annotation_paths": sample["source_annotation_paths"],
                }
            )

        curated_benchmarks, raw_benchmarks = curate_benchmarks(
            row,
            current_annotation,
        )
        raw_benchmark_rows.extend(raw_benchmarks)
        row["low_input_benchmarks_json"] = json_text(curated_benchmarks)

        for benchmark_index, benchmark in enumerate(curated_benchmarks, start=1):
            benchmark_rows.append(
                {
                    "accession": row["accession"],
                    "publication_doi": row["publication_doi"],
                    "publication_title": row["publication_title"],
                    "benchmark_index": benchmark_index,
                    "sample": benchmark["sample"],
                    "organism": benchmark["organism"],
                    "input_amount": benchmark["input_amount"],
                    "qc_status": benchmark["qc_status"],
                    "annotation_path": row["annotation_path"],
                }
            )

    # Non-positive rows never receive single-cell sample metadata.
    for row in annotation_rows:
        if text_value(row.get("is_single_cell_proteomics")).lower() != "yes":
            row["stage5_metadata_qc_version"] = STAGE5_METADATA_QC_VERSION
            row["sample_metadata_status"] = "not_applicable"
            row["sample_metadata_sources"] = ""
            row["raw_sample_candidate_count"] = 0

    # Write publication-level outputs only after deterministic metadata QC so
    # their sample fields match the normalized sample table.
    write_csv(
        output_dir / "all_publication_annotations.csv",
        annotation_rows,
        PUBLICATION_FIELDS,
    )
    write_csv(
        output_dir / "single_cell_publication_catalogue.csv",
        positives,
        PUBLICATION_FIELDS,
    )

    grouped = defaultdict(list)
    for row in positives:
        grouped[row["accession"]].append(row)

    dataset_rows = [
        dataset_summary(accession, rows)
        for accession, rows in sorted(grouped.items())
    ]
    write_csv(
        output_dir / "single_cell_dataset_catalogue.csv",
        dataset_rows,
        DATASET_FIELDS,
    )

    write_csv(
        output_dir / "single_cell_samples.csv",
        sample_rows,
        [
            "accession",
            "publication_doi",
            "publication_title",
            "sample_index",
            "sample_type",
            "organism",
            "organism_resolution",
            "cell_counts_reported",
            "sample_metadata_status",
            "sample_metadata_sources",
            "source_annotation_versions",
            "candidate_origins",
            "qc_notes",
            "scp_evidence_tier",
            "current_annotation_path",
            "source_annotation_paths",
        ],
    )

    write_csv(
        output_dir / "single_cell_samples_raw_candidates.csv",
        raw_sample_rows,
        [
            "accession",
            "publication_doi",
            "publication_title",
            "dataset_title",
            "source_kind",
            "candidate_origin",
            "source_annotation_version",
            "source_annotation_path",
            "raw_sample_type",
            "raw_organism",
            "raw_cell_counts_reported",
            "curated_sample_type",
            "candidate_outcome",
            "qc_reason",
        ],
    )

    write_csv(
        output_dir / "low_input_benchmarks.csv",
        benchmark_rows,
        [
            "accession",
            "publication_doi",
            "publication_title",
            "benchmark_index",
            "sample",
            "organism",
            "input_amount",
            "qc_status",
            "annotation_path",
        ],
    )

    write_csv(
        output_dir / "low_input_benchmarks_raw_candidates.csv",
        raw_benchmark_rows,
        [
            "accession",
            "publication_doi",
            "publication_title",
            "raw_benchmark_index",
            "raw_sample",
            "raw_organism",
            "raw_input_amount",
            "candidate_outcome",
            "qc_reason",
            "annotation_pipeline_version",
            "annotation_path",
        ],
    )

    write_csv(
        output_dir / "annotation_failures.csv",
        failures,
        [
            "status_file",
            "accession",
            "publication_doi",
            "status",
            "error",
            "log_path",
        ],
    )

    screened_out = [
        {
            "accession": row.get("accession", ""),
            "dataset_title": row.get("dataset_title", ""),
            "publication_doi": row.get("publication_doi", ""),
            "publication_title": row.get("publication_title", ""),
            "pdf_path": row.get("pdf_path", ""),
            "scp_screen_decision": row.get("scp_screen_decision", ""),
            "scp_screen_score": row.get("scp_screen_score", ""),
            "scp_screen_reason": row.get("scp_screen_reason", ""),
            "scp_screen_evidence": row.get("scp_screen_evidence", ""),
        }
        for row in manifest_rows
        if row.get("scp_screen_decision") in {
            "reject",
            "not_applicable",
            "screen_error",
        }
    ]

    write_csv(
        output_dir / "screened_out_publications.csv",
        screened_out,
        [
            "accession",
            "dataset_title",
            "publication_doi",
            "publication_title",
            "pdf_path",
            "scp_screen_decision",
            "scp_screen_score",
            "scp_screen_reason",
            "scp_screen_evidence",
        ],
    )

    missing_sample_metadata = sum(
        1
        for row in dataset_rows
        if row.get("sample_metadata_status") == "not_reliably_extracted"
    )
    recovered_history = sum(
        1
        for row in dataset_rows
        if row.get("sample_metadata_status") in {
            "recovered_from_history",
            "partial",
        }
        or "history" in text_value(row.get("sample_metadata_sources"))
    )
    title_derived_datasets = sum(
        1
        for row in dataset_rows
        if row.get("sample_metadata_status") == "title_derived"
        or "title_derived" in text_value(row.get("sample_metadata_sources"))
    )

    qc_summary = {
        "stage5_metadata_qc_version": STAGE5_METADATA_QC_VERSION,
        "history_mode": args.history_mode,
        "history_dirs": [str(path) for path in history_dirs],
        "historical_annotation_jsons_scanned": len(history_records),
        "successful_annotation_rows": len(annotation_rows),
        "single_cell_publication_rows": len(positives),
        "single_cell_datasets": len(dataset_rows),
        "curated_sample_rows": len(sample_rows),
        "raw_sample_candidates": len(raw_sample_rows),
        "datasets_without_reliable_sample_metadata": missing_sample_metadata,
        "datasets_using_historical_sample_metadata": recovered_history,
        "datasets_using_title_derived_sample_metadata": title_derived_datasets,
        "curated_low_input_benchmarks": len(benchmark_rows),
        "raw_low_input_benchmark_candidates": len(raw_benchmark_rows),
        "annotation_failures": len(failures),
        "screened_out_manifest_rows": len(screened_out),
        "classification_note": (
            "Historical annotations are metadata-only recall sources and never "
            "alter current is_single_cell_proteomics classification."
        ),
    }
    (output_dir / "catalogue_qc_summary.json").write_text(
        json.dumps(qc_summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"Successful annotation rows: {len(annotation_rows):,}")
    print(f"Single-cell publication rows: {len(positives):,}")
    print(f"Single-cell datasets: {len(dataset_rows):,}")
    print(f"Curated sample rows: {len(sample_rows):,}")
    print(f"Raw sample candidates audited: {len(raw_sample_rows):,}")
    print(
        "Datasets without reliable sample metadata: "
        f"{missing_sample_metadata:,}"
    )
    print(
        "Datasets using historical sample metadata: "
        f"{recovered_history:,}"
    )
    print(
        "Datasets using title-derived sample metadata: "
        f"{title_derived_datasets:,}"
    )
    print(f"Curated low-input benchmarks: {len(benchmark_rows):,}")
    print(f"Raw low-input benchmark candidates audited: {len(raw_benchmark_rows):,}")
    print(f"Metadata history directories used: {len(history_dirs):,}")
    print(f"Failures: {len(failures):,}")
    print(f"Screened-out manifest rows: {len(screened_out):,}")
    print(f"Saved catalogue files under: {output_dir}")


if __name__ == "__main__":
    main()
