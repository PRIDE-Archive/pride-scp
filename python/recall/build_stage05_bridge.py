#!/usr/bin/env python3
"""Build a Stage-05-compatible annotation/status bridge from unified decisions.

The bridge is intentionally gated: review candidates require an explicit
include/exclude decision before a *final* bridge is emitted.  This prevents the
recall-oriented semantic routes from silently becoming final catalogue labels.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
from pathlib import Path
from typing import Any

REVIEW_ROUTES = {"review_high", "review_medium", "review_low"}
VALID_FINAL = {"include", "exclude", "uncertain"}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--unified-manifest",
        default="work/python/semantic_unification/unified_semantic_manifest.jsonl",
    )
    p.add_argument(
        "--publication-candidates",
        default="work/python/semantic_partition/publication_backed_candidates.jsonl",
    )
    p.add_argument(
        "--repository-candidates",
        default="work/python/semantic_partition/repository_only_candidates.jsonl",
    )
    p.add_argument(
        "--annotations-dir",
        default="work/python/pride_scp_annotations/annotations",
    )
    p.add_argument(
        "--review-decisions",
        default="work/python/semantic_unification/review_decisions.tsv",
        help="TSV containing accession,final_decision[,review_note].",
    )
    p.add_argument(
        "--output-dir",
        default="work/python/stage05_bridge",
    )
    p.add_argument(
        "--allow-provisional",
        action="store_true",
        help=(
            "Build a smoke-test bridge even when review decisions are unresolved. "
            "Unresolved review candidates are encoded as no/excluded. Do not use "
            "this mode for the final catalogue."
        ),
    )
    return p.parse_args()


def text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    out = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def read_decisions(path: Path) -> dict[str, dict[str, str]]:
    if not path.is_file():
        return {}
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    out: dict[str, dict[str, str]] = {}
    for row in rows:
        accession = text(row.get("accession")).upper()
        decision = text(row.get("final_decision")).lower()
        if not accession or not decision:
            continue
        if decision not in VALID_FINAL:
            raise ValueError(
                f"Invalid final_decision for {accession}: {decision!r}; expected include/exclude/uncertain"
            )
        if accession in out:
            raise ValueError(f"Duplicate review decision for {accession}")
        out[accession] = {
            "final_decision": decision,
            "review_note": text(row.get("review_note")),
        }
    return out


def find_annotations(root: Path, accession: str) -> list[Path]:
    folder = root / accession
    if not folder.is_dir():
        return []
    return sorted(
        p
        for p in folder.glob("*.json")
        if p.is_file() and ".work" not in p.parts
    )


def candidate_final_decision(row: dict[str, Any], overrides: dict[str, dict[str, str]]) -> tuple[str | None, str]:
    accession = text(row.get("accession")).upper()
    if accession in overrides:
        o = overrides[accession]
        if o["final_decision"] == "uncertain":
            return None, o.get("review_note", "") or "independent QC remained uncertain"
        return o["final_decision"], o.get("review_note", "")
    route = text(row.get("unified_route"))
    if route == "include_candidate":
        return "include", "provisional include route; downstream QC still required"
    if route == "likely_non_scp":
        return "exclude", "provisional likely-non-SCP route"
    if route in REVIEW_ROUTES:
        return None, "secondary review decision required"
    raise ValueError(f"Unknown unified route for {accession}: {route!r}")


def publication_metadata(candidate: dict[str, Any], annotation: dict[str, Any]) -> dict[str, str]:
    contents = candidate.get("publication_contents") or []
    doi = text(annotation.get("publication_doi"))
    title = text(annotation.get("publication_title_candidate"))
    chosen: dict[str, Any] = {}
    if contents:
        if doi:
            for item in contents:
                if text(item.get("publication_doi")).lower() == doi.lower():
                    chosen = item
                    break
        if not chosen:
            chosen = contents[0]
    return {
        "publication_index": text(chosen.get("publication_index")) or "1",
        "publication_doi": doi or text(chosen.get("publication_doi")),
        "publication_pmid": "",
        "publication_pmcid": "",
        "publication_title": title or text(chosen.get("publication_title")),
        "publication_url": "",
        "pdf_path": text(chosen.get("content_path")) if text(chosen.get("content_kind")) == "pdf" else "",
        "publication_content_kind": text(chosen.get("content_kind")),
        "publication_content_path": text(chosen.get("content_path")),
    }


def bridge_gate(row: dict[str, Any], decision: str, review_note: str) -> dict[str, Any]:
    return {
        "model_classification": text(row.get("stage04_model_classification"))
        or text(row.get("repository_triage_class")),
        "final_classification": "yes" if decision == "include" else "no",
        "supported": decision == "include",
        "reason": (
            f"semantic-unification-v1: {text(row.get('unified_route_reason'))}"
            + (f" Review: {review_note}" if review_note else "")
        ),
        "evidence": "",
        "support_scope": "semantic_unification",
        "evidence_tier": text(row.get("unified_route")),
    }


def patch_publication_annotation(
    annotation: dict[str, Any],
    row: dict[str, Any],
    decision: str,
    review_note: str,
) -> dict[str, Any]:
    out = json.loads(json.dumps(annotation))
    original_class = text(out.get("is_single_cell_proteomics"))
    out["is_single_cell_proteomics"] = "yes" if decision == "include" else "no"
    out["classification_policy_version"] = "semantic-unification-v1"
    out.setdefault("validation_warnings", [])
    out["validation_warnings"].append(
        "Stage-05 bridge classification is controlled by semantic-unification-v1; "
        f"original Stage-04 classification was {original_class!r}."
    )
    provenance = out.setdefault("provenance", {})
    provenance["pre_unification_scp_evidence_gate"] = provenance.get("scp_evidence_gate", {})
    provenance["scp_evidence_gate"] = bridge_gate(row, decision, review_note)
    provenance["semantic_unification"] = {
        "unified_route": row.get("unified_route"),
        "unified_route_reason": row.get("unified_route_reason"),
        "review_flags": row.get("review_flags", []),
        "review_note": review_note,
        "final_decision": decision,
        "semantic_evidence_mode": row.get("semantic_evidence_mode"),
        "semantic_priority": row.get("semantic_priority"),
    }
    return out


def synthesize_repository_annotation(
    candidate: dict[str, Any],
    row: dict[str, Any],
    decision: str,
    review_note: str,
) -> dict[str, Any]:
    warning = (
        "Repository-only candidate: publication-derived sample/workflow metadata is unavailable; "
        "classification derives from recall discovery, repository semantic evidence, and explicit review routing."
    )
    return {
        "target_accession": row["accession"],
        "publication_source_kind": "repository_only",
        "publication_source_path": "",
        "target_accession_mentioned_in_publication": False,
        "publication_doi": "",
        "publication_doi_source": "",
        "pdf_detected_primary_doi": "",
        "publication_title_candidate": "",
        "publication_title_source": "",
        "publication_pride_accessions": [],
        "target_dataset_label": None,
        "accession_scope_kind": "repository_only",
        "publication_accession_labels": {},
        "annotation_pipeline_version": "recall-unified-v0.1.8",
        "classification_policy_version": "semantic-unification-v1",
        "metadata_qc_version": "repository-only-v1",
        "annotation_model": "qwen2.5:3b+deterministic-unification",
        "cpu_threads": None,
        "is_single_cell_proteomics": "yes" if decision == "include" else "no",
        "true_single_cell_samples": [],
        "low_input_benchmarks": [],
        "sample_preparation": None,
        "single_cell_isolation": [],
        "labeling_strategy": None,
        "mass_spectrometers": [],
        "acquisition_modes": [],
        "ion_mobility_or_faims": [],
        "lc_configuration": None,
        "lc_gradient": None,
        "single_cell_throughput": None,
        "single_cell_proteome_depth": None,
        "low_input_proteome_depth": None,
        "analysis_software": [],
        "analysis_strategies": [],
        "catalogue_note": (
            "Repository-only semantic candidate. Dataset title: "
            + text(candidate.get("dataset_title"))
        ),
        "validation_warnings": [warning],
        "provenance": {
            "scp_evidence_gate": bridge_gate(row, decision, review_note),
            "semantic_unification": {
                "unified_route": row.get("unified_route"),
                "unified_route_reason": row.get("unified_route_reason"),
                "review_flags": row.get("review_flags", []),
                "review_note": review_note,
                "final_decision": decision,
                "semantic_evidence_mode": "repository_only",
                "semantic_priority": row.get("semantic_priority"),
                "repository_triage_class": row.get("repository_triage_class"),
                "repository_individual_cell_evidence": row.get("repository_individual_cell_evidence"),
                "repository_ms_proteomics_evidence": row.get("repository_ms_proteomics_evidence"),
                "repository_normalized_evidence": row.get("repository_normalized_evidence"),
                "repository_triage_consistency": row.get("repository_triage_consistency"),
                "repository_triage_reason": row.get("repository_triage_reason"),
                "discovery_hits": candidate.get("hits", []),
            },
        },
        "inference_stats": {},
    }


def manifest_row(candidate: dict[str, Any], meta: dict[str, str]) -> dict[str, str]:
    accession = text(candidate.get("accession"))
    return {
        "accession": accession,
        "dataset_title": text(candidate.get("dataset_title")),
        "pride_project_url": f"https://www.ebi.ac.uk/pride/archive/projects/{accession}",
        "pride_ftp_url": "",
        "publication_index": meta.get("publication_index", "0"),
        "publication_doi": meta.get("publication_doi", ""),
        "publication_pmid": meta.get("publication_pmid", ""),
        "publication_pmcid": meta.get("publication_pmcid", ""),
        "resolved_pmcid": meta.get("publication_pmcid", ""),
        "publication_title": meta.get("publication_title", ""),
        "publication_url": meta.get("publication_url", ""),
        "pdf_path": meta.get("pdf_path", ""),
        "publication_content_kind": meta.get("publication_content_kind", ""),
        "publication_content_path": meta.get("publication_content_path", ""),
        "scp_screen_decision": "",
        "scp_screen_score": "",
        "scp_screen_reason": "",
        "scp_screen_evidence": "",
    }


MANIFEST_FIELDS = [
    "accession",
    "dataset_title",
    "pride_project_url",
    "pride_ftp_url",
    "publication_index",
    "publication_doi",
    "publication_pmid",
    "publication_pmcid",
    "resolved_pmcid",
    "publication_title",
    "publication_url",
    "pdf_path",
    "publication_content_kind",
    "publication_content_path",
    "scp_screen_decision",
    "scp_screen_score",
    "scp_screen_reason",
    "scp_screen_evidence",
]


def write_manifest(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    args = parse_args()
    unified_rows = read_jsonl(Path(args.unified_manifest))
    unified = {text(r.get("accession")): r for r in unified_rows}
    pub_candidates = {text(r.get("accession")): r for r in read_jsonl(Path(args.publication_candidates))}
    repo_candidates = {text(r.get("accession")): r for r in read_jsonl(Path(args.repository_candidates))}
    candidate_lookup = {**pub_candidates, **repo_candidates}
    decisions = read_decisions(Path(args.review_decisions))

    unresolved = []
    final: dict[str, tuple[str, str]] = {}
    for accession, row in sorted(unified.items()):
        decision, note = candidate_final_decision(row, decisions)
        if decision is None:
            unresolved.append(accession)
            if args.allow_provisional:
                decision = "exclude"
                note = "PROVISIONAL ONLY: unresolved secondary review encoded as exclude for bridge smoke test"
            else:
                continue
        final[accession] = (decision, note)

    if unresolved and not args.allow_provisional:
        raise SystemExit(
            "Refusing to build final Stage-05 bridge: "
            f"{len(unresolved)} secondary-review candidates still lack explicit decisions.\n"
            f"Fill {args.review_decisions} using the generated review_decisions.template.tsv.\n"
            "Use --allow-provisional only for a smoke test; it excludes unresolved reviews."
        )

    output_dir = Path(args.output_dir)
    if output_dir.exists():
        shutil.rmtree(output_dir)
    annotations_out = output_dir / "annotations"
    status_out = output_dir / "status"
    annotations_out.mkdir(parents=True, exist_ok=True)
    status_out.mkdir(parents=True, exist_ok=True)

    manifest_rows: list[dict[str, str]] = []
    status_rows: list[dict[str, Any]] = []
    inclusion_count = 0
    exclusion_count = 0

    for accession in sorted(unified):
        row = unified[accession]
        candidate = candidate_lookup[accession]
        decision, review_note = final[accession]
        if decision == "include":
            inclusion_count += 1
        else:
            exclusion_count += 1

        if accession in pub_candidates:
            source_paths = find_annotations(Path(args.annotations_dir), accession)
            if not source_paths:
                raise RuntimeError(f"No Stage-04 annotation found for {accession}")
            for pub_i, source_path in enumerate(source_paths, start=1):
                source_annotation = json.loads(source_path.read_text(encoding="utf-8"))
                patched = patch_publication_annotation(source_annotation, row, decision, review_note)
                acc_dir = annotations_out / accession
                acc_dir.mkdir(parents=True, exist_ok=True)
                out_path = acc_dir / source_path.name
                out_path.write_text(
                    json.dumps(patched, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                meta = publication_metadata(candidate, source_annotation)
                meta["publication_index"] = meta.get("publication_index") or str(pub_i)
                mrow = manifest_row(candidate, meta)
                manifest_row_number = len(manifest_rows)
                manifest_rows.append(mrow)
                status = {
                    "job_index": manifest_row_number,
                    "manifest_row_number": manifest_row_number,
                    "job_id": f"{accession}__unified_pub{pub_i}",
                    "accession": accession,
                    "publication_doi": mrow["publication_doi"],
                    "publication_title": mrow["publication_title"],
                    "publication_index": mrow["publication_index"],
                    "status": "success",
                    "annotation_path": str(out_path.resolve()),
                    "log_path": "",
                    "returncode": 0,
                    "wall_seconds": 0.0,
                    "semantic_unification_decision": decision,
                }
                status_path = status_out / f"{accession}__unified_pub{pub_i}.json"
                status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")
                status_rows.append(status)
        else:
            annotation = synthesize_repository_annotation(candidate, row, decision, review_note)
            acc_dir = annotations_out / accession
            acc_dir.mkdir(parents=True, exist_ok=True)
            out_path = acc_dir / f"{accession}__repository_only.json"
            out_path.write_text(json.dumps(annotation, indent=2, ensure_ascii=False), encoding="utf-8")
            mrow = manifest_row(candidate, {
                "publication_index": "0",
                "publication_doi": "",
                "publication_pmid": "",
                "publication_pmcid": "",
                "publication_title": "",
                "publication_url": "",
                "pdf_path": "",
                "publication_content_kind": "repository_only",
                "publication_content_path": "",
            })
            manifest_row_number = len(manifest_rows)
            manifest_rows.append(mrow)
            status = {
                "job_index": manifest_row_number,
                "manifest_row_number": manifest_row_number,
                "job_id": f"{accession}__repository_only",
                "accession": accession,
                "publication_doi": "",
                "publication_title": "",
                "publication_index": "0",
                "status": "success",
                "annotation_path": str(out_path.resolve()),
                "log_path": "",
                "returncode": 0,
                "wall_seconds": 0.0,
                "semantic_unification_decision": decision,
            }
            status_path = status_out / f"{accession}__repository_only.json"
            status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")
            status_rows.append(status)

    write_manifest(output_dir / "manifest.tsv", manifest_rows)
    summary = {
        "candidate_accessions": len(unified),
        "manifest_rows": len(manifest_rows),
        "status_rows": len(status_rows),
        "final_includes": inclusion_count,
        "final_excludes": exclusion_count,
        "unresolved_reviews_in_source": len(unresolved),
        "allow_provisional": args.allow_provisional,
        "note": (
            "Repository-only annotations are intentionally metadata-sparse. "
            "The unified semantic manifest remains the classification provenance authority."
        ),
    }
    (output_dir / "bridge_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    print("\nStage 05 command:")
    print(
        "python python/stages/05_merge_pride_scp_catalogue.py "
        f"{output_dir / 'manifest.tsv'} "
        f"--annotations-dir {output_dir} "
        "--output-dir work/python/pride_scp_catalogue_recall"
    )


if __name__ == "__main__":
    main()
