#!/usr/bin/env python3
"""Build the GT-independent PRIDE production catalogue from frozen discovery + v19 outputs.

This script intentionally has no GT/reference inputs. It joins only:
  * primary-PRIDE discovery candidates;
  * semantic candidate diagnostics; and
  * frozen v19 curation outputs.

Automated `include` rows form the machine-generated catalogue. `review` rows remain
explicit review work; they are never silently promoted or treated as negatives.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

FACT_FIELDS = [
    "biological_sample_unit",
    "biological_unit_class",
    "true_single_cell_ms_samples_present",
    "cells_per_target_ms_sample",
    "individual_identity_preserved_to_ms",
    "destructive_pooling_before_ms",
    "pooling_stage",
    "benchmark_only",
    "adjacent_single_cell_only",
    "reanalysis_only",
    "mixed_design",
]

OUTPUT_FIELDS = [
    "accession",
    "dataset_title",
    "dataset_description",
    "discovery_score",
    "discovery_tier",
    "semantic_priority",
    "evidence_profile",
    "positive_lanes",
    "specific_scp_labels",
    "method_labels",
    "broad_context_labels",
    "adjacent_labels",
    "negative_context_labels",
    "curation_pipeline_version",
    "curation_model",
    "run_status",
    "curation_decision",
    "decision_reason",
    "evidence_sufficiency",
    *FACT_FIELDS,
    "evidence_item_count",
    "deterministic_evidence_item_count",
    "publication_annotation_count",
    "validation_warning_count",
    "project_json_path",
    "files_json_path",
    "sdrf_path",
    "curation_result_path",
    "source_review_status",
    "source_review_decision",
    "source_review_notes",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates-jsonl", required=True)
    parser.add_argument("--candidate-diagnostics", required=True)
    parser.add_argument("--curation-summary", required=True)
    parser.add_argument("--curation-version", required=True)
    parser.add_argument("--output-dir", required=True)
    return parser.parse_args()


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in OUTPUT_FIELDS})


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).strip()


def main() -> None:
    args = parse_args()
    candidates_path = Path(args.candidates_jsonl).resolve()
    diagnostics_path = Path(args.candidate_diagnostics).resolve()
    curation_path = Path(args.curation_summary).resolve()
    output_dir = Path(args.output_dir).resolve()

    candidates = read_jsonl(candidates_path)
    diagnostics = read_tsv(diagnostics_path)
    curation = read_tsv(curation_path)

    candidate_map = {text(row.get("accession")).upper(): row for row in candidates}
    diagnostic_map = {text(row.get("accession")).upper(): row for row in diagnostics}
    curation_map = {text(row.get("accession")).upper(): row for row in curation}

    if len(candidate_map) != len(candidates):
        raise SystemExit("duplicate accession in production candidate JSONL")
    if set(candidate_map) != set(diagnostic_map):
        missing_diag = sorted(set(candidate_map) - set(diagnostic_map))
        extra_diag = sorted(set(diagnostic_map) - set(candidate_map))
        raise SystemExit(f"candidate/diagnostic accession mismatch: missing={missing_diag[:10]} extra={extra_diag[:10]}")
    if set(candidate_map) != set(curation_map):
        missing_cur = sorted(set(candidate_map) - set(curation_map))
        extra_cur = sorted(set(curation_map) - set(candidate_map))
        raise SystemExit(f"candidate/curation accession mismatch: missing={missing_cur[:10]} extra={extra_cur[:10]}")

    joined: list[dict[str, Any]] = []
    for accession in sorted(candidate_map):
        if not accession.startswith("PXD"):
            raise SystemExit(f"non-PXD accession in PRIDE production candidate cohort: {accession}")
        candidate = candidate_map[accession]
        project_path = text(candidate.get("project_json_path"))
        normalized = project_path.replace("\\", "/")
        if "/registry/projects/" in normalized or "/native/" in normalized:
            raise SystemExit(f"non-primary repository record entered PRIDE production cohort: {accession}: {project_path}")
        if "/projects/" not in normalized:
            raise SystemExit(f"candidate lacks primary PRIDE project provenance: {accession}: {project_path}")

        diag = diagnostic_map[accession]
        cur = curation_map[accession]
        if text(cur.get("run_status")) != "success":
            raise SystemExit(f"curation did not succeed for {accession}: {cur.get('run_status')} {cur.get('error', '')}")

        row: dict[str, Any] = {
            "accession": accession,
            "dataset_title": candidate.get("dataset_title", ""),
            "dataset_description": candidate.get("dataset_description", ""),
            "discovery_score": candidate.get("score", ""),
            "discovery_tier": candidate.get("tier", ""),
            "semantic_priority": diag.get("semantic_priority", ""),
            "evidence_profile": diag.get("evidence_profile", ""),
            "positive_lanes": diag.get("positive_lanes", ""),
            "specific_scp_labels": diag.get("specific_scp_labels", ""),
            "method_labels": diag.get("method_labels", ""),
            "broad_context_labels": diag.get("broad_context_labels", ""),
            "adjacent_labels": diag.get("adjacent_labels", ""),
            "negative_context_labels": diag.get("negative_context_labels", ""),
            "curation_pipeline_version": args.curation_version,
            "curation_model": cur.get("model", ""),
            "run_status": cur.get("run_status", ""),
            "curation_decision": cur.get("curation_decision", ""),
            "decision_reason": cur.get("decision_reason", ""),
            "evidence_sufficiency": cur.get("evidence_sufficiency", ""),
            "evidence_item_count": cur.get("evidence_item_count", ""),
            "deterministic_evidence_item_count": cur.get("deterministic_evidence_item_count", ""),
            "publication_annotation_count": cur.get("publication_annotation_count", ""),
            "validation_warning_count": cur.get("validation_warning_count", ""),
            "project_json_path": candidate.get("project_json_path", ""),
            "files_json_path": candidate.get("files_json_path", ""),
            "sdrf_path": candidate.get("sdrf_path", ""),
            "curation_result_path": cur.get("result_path", ""),
            "source_review_status": "unreviewed",
            "source_review_decision": "",
            "source_review_notes": "",
        }
        for field in FACT_FIELDS:
            row[field] = cur.get(field, "")
        joined.append(row)

    decisions = Counter(text(row.get("curation_decision")) for row in joined)
    invalid = sorted(set(decisions) - {"include", "review", "exclude"})
    if invalid:
        raise SystemExit(f"unexpected curation decisions: {invalid}")

    includes = [row for row in joined if row["curation_decision"] == "include"]
    reviews = [row for row in joined if row["curation_decision"] == "review"]
    excludes = [row for row in joined if row["curation_decision"] == "exclude"]

    write_csv(output_dir / "pride_scp_all_curated_candidates.csv", joined)
    write_csv(output_dir / "pride_scp_catalogue_automated_includes.csv", includes)
    write_csv(output_dir / "pride_scp_review_queue.csv", reviews)
    write_csv(output_dir / "pride_scp_excluded_candidates.csv", excludes)

    summary = {
        "phase": "PRIDE_GT_independent_production_catalogue_v1",
        "gt_used_for_candidate_selection": False,
        "gt_used_for_identity": False,
        "gt_used_for_curation": False,
        "gt_used_for_catalogue_export": False,
        "candidate_count": len(joined),
        "successful_curation_count": len(joined),
        "decision_counts": dict(sorted(decisions.items())),
        "automated_catalogue_includes": len(includes),
        "review_queue": len(reviews),
        "excluded_candidates": len(excludes),
        "curation_pipeline_version": args.curation_version,
        "source_scope": "pride-primary",
        "input_sha256": {
            "candidates_jsonl": sha256(candidates_path),
            "candidate_diagnostics": sha256(diagnostics_path),
            "curation_summary": sha256(curation_path),
        },
        "note": (
            "Production catalogue generated only from primary PRIDE discovery and frozen v19 evidence-first curation. "
            "GT/reference files are not accepted as inputs. Review is preserved as an explicit unresolved state."
        ),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "pride_scp_production_catalogue_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
