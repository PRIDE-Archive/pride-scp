#!/usr/bin/env python3
"""Offline regression smoke for v0.1.8 semantic unification + Stage-05 bridge."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
UNIFY = HERE / "unify_semantic_results.py"
BRIDGE = HERE / "build_stage05_bridge.py"


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def candidate(accession: str, mode: str, priority: str) -> dict:
    return {
        "accession": accession,
        "dataset_title": f"Fixture {accession}",
        "dataset_description": "fixture evidence",
        "discovery_score": 100,
        "discovery_tier": "strong",
        "semantic_priority": priority,
        "evidence_profile": "specific" if priority == "A_specific" else "broad_only",
        "broad_only": priority == "C_broad",
        "has_negative_context": False,
        "specific_scp_labels": ["single_cell_proteomics"] if priority == "A_specific" else [],
        "method_labels": ["plexdia"] if priority == "B_method" else [],
        "broad_context_labels": ["single_cells"] if priority == "C_broad" else [],
        "adjacent_labels": [],
        "negative_context_labels": [],
        "hits": [],
        "semantic_evidence_mode": mode,
        "publication_contents": [],
        "publication_pdfs": [],
    }


def annotation(accession: str, final: str, model: str) -> dict:
    return {
        "target_accession": accession,
        "publication_source_kind": "pdf",
        "publication_source_path": "/fixture.pdf",
        "publication_doi": f"10.0000/{accession.lower()}",
        "publication_title_candidate": f"Fixture publication {accession}",
        "target_accession_mentioned_in_publication": True,
        "annotation_pipeline_version": "v18",
        "classification_policy_version": "v17",
        "metadata_qc_version": "v18",
        "annotation_model": "qwen2.5:3b",
        "is_single_cell_proteomics": final,
        "true_single_cell_samples": [],
        "low_input_benchmarks": [],
        "single_cell_isolation": [],
        "mass_spectrometers": [],
        "acquisition_modes": [],
        "ion_mobility_or_faims": [],
        "analysis_software": [],
        "analysis_strategies": [],
        "validation_warnings": [],
        "provenance": {
            "scp_evidence_gate": {
                "model_classification": model,
                "final_classification": final,
                "supported": final == "yes",
                "evidence_tier": "fixture",
                "reason": "fixture",
                "support_scope": "fixture",
            }
        },
    }


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="pride-scp-v018-") as td:
        root = Path(td)
        diag = root / "candidate_diagnostics.jsonl"
        pub = root / "publication.jsonl"
        repo = root / "repository.jsonl"
        ann_root = root / "annotations"
        triage = root / "triage.tsv"
        out = root / "unified"
        bridge = root / "bridge"

        pubs = [
            candidate("PXD900101", "publication_backed", "A_specific"),
            candidate("PXD900102", "publication_backed", "A_specific"),
            candidate("PXD900103", "publication_backed", "C_broad"),
            candidate("PXD900104", "publication_backed", "C_broad"),
        ]
        repos = [
            candidate("PXD900201", "repository_only", "A_specific"),
            candidate("PXD900202", "repository_only", "A_specific"),
            candidate("PXD900203", "repository_only", "B_method"),
            candidate("PXD900204", "repository_only", "C_broad"),
        ]
        write_jsonl(diag, pubs + repos)
        write_jsonl(pub, pubs)
        write_jsonl(repo, repos)

        ann_specs = {
            "PXD900101": ("yes", "yes"),       # include
            "PXD900102": ("no", "yes"),        # A-specific conflict -> review_high
            "PXD900103": ("no", "yes"),        # C-broad + model yes -> review_medium
            "PXD900104": ("no", "no"),         # broad/no -> likely_non_scp
        }
        for accession, (final, model) in ann_specs.items():
            folder = ann_root / accession
            folder.mkdir(parents=True, exist_ok=True)
            payload = annotation(accession, final, model)
            payload["publication_doi"] = f"10.0000/{accession.lower()}"
            (folder / f"{accession}.json").write_text(json.dumps(payload), encoding="utf-8")
            # provide matching content metadata for bridge
            for item in pubs:
                if item["accession"] == accession:
                    item["publication_contents"] = [{
                        "publication_index": "1",
                        "publication_doi": payload["publication_doi"],
                        "publication_title": payload["publication_title_candidate"],
                        "content_kind": "pdf",
                        "content_path": "/fixture.pdf",
                        "content_source": "fixture",
                    }]
        # Rewrite publication JSONL after adding content metadata.
        write_jsonl(pub, pubs)

        with triage.open("w", newline="", encoding="utf-8") as handle:
            fields = [
                "accession",
                "run_status",
                "triage_class",
                "individual_cell_measurement_evidence",
                "mass_spectrometry_proteomics_evidence",
                "reason",
                "model",
                "wall_seconds",
                "error",
            ]
            writer = csv.DictWriter(handle, fieldnames=fields)  # intentional CSV despite .tsv, matching v0.1.7
            writer.writeheader()
            writer.writerows([
                {"accession": "PXD900201", "run_status": "success", "triage_class": "possible_true_scp", "individual_cell_measurement_evidence": "direct", "mass_spectrometry_proteomics_evidence": "direct", "reason": "fixture", "model": "qwen", "wall_seconds": 1, "error": ""},
                {"accession": "PXD900202", "run_status": "success", "triage_class": "possible_true_scp", "individual_cell_measurement_evidence": "absent", "mass_spectrometry_proteomics_evidence": "absent", "reason": "fixture overcall", "model": "qwen", "wall_seconds": 1, "error": ""},
                {"accession": "PXD900203", "run_status": "success", "triage_class": "possible_true_scp", "individual_cell_measurement_evidence": "absent", "mass_spectrometry_proteomics_evidence": "absent", "reason": "fixture", "model": "qwen", "wall_seconds": 1, "error": ""},
                {"accession": "PXD900204", "run_status": "success", "triage_class": "possible_true_scp", "individual_cell_measurement_evidence": "direct", "mass_spectrometry_proteomics_evidence": "direct", "reason": "fixture broad overcall", "model": "qwen", "wall_seconds": 1, "error": ""},
            ])

        subprocess.run([
            sys.executable, str(UNIFY),
            "--candidate-diagnostics", str(diag),
            "--publication-candidates", str(pub),
            "--repository-candidates", str(repo),
            "--annotations-dir", str(ann_root),
            "--repository-triage", str(triage),
            "--output-dir", str(out),
        ], check=True, stdout=subprocess.DEVNULL)

        summary = json.loads((out / "semantic_unification_summary.json").read_text())
        assert summary["candidates"] == 8
        assert summary["candidate_loss"] == 0
        assert summary["repository_triage_overcalls_vs_own_structured_evidence"] == 2
        assert summary["route_counts"] == {
            "include_candidate": 2,
            "likely_non_scp": 2,
            "review_high": 2,
            "review_medium": 2,
        }, summary["route_counts"]

        # Final bridge must refuse unresolved review candidates.
        refused = subprocess.run([
            sys.executable, str(BRIDGE),
            "--unified-manifest", str(out / "unified_semantic_manifest.jsonl"),
            "--publication-candidates", str(pub),
            "--repository-candidates", str(repo),
            "--annotations-dir", str(ann_root),
            "--review-decisions", str(out / "review_decisions.tsv"),
            "--output-dir", str(bridge),
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert refused.returncode != 0

        # Resolve all review routes explicitly, then build final bridge.
        template_rows = list(csv.DictReader((out / "review_decisions.template.tsv").open(encoding="utf-8"), delimiter="\t"))
        with (out / "review_decisions.tsv").open("w", newline="", encoding="utf-8") as handle:
            fields = ["accession", "unified_route", "semantic_evidence_mode", "semantic_priority", "final_decision", "review_note"]
            writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", lineterminator="\n")
            writer.writeheader()
            for row in template_rows:
                row["final_decision"] = "include" if row["semantic_priority"] == "A_specific" else "exclude"
                row["review_note"] = "fixture decision"
                writer.writerow(row)

        subprocess.run([
            sys.executable, str(BRIDGE),
            "--unified-manifest", str(out / "unified_semantic_manifest.jsonl"),
            "--publication-candidates", str(pub),
            "--repository-candidates", str(repo),
            "--annotations-dir", str(ann_root),
            "--review-decisions", str(out / "review_decisions.tsv"),
            "--output-dir", str(bridge),
        ], check=True, stdout=subprocess.DEVNULL)
        bsum = json.loads((bridge / "bridge_summary.json").read_text())
        assert bsum["candidate_accessions"] == 8
        assert bsum["manifest_rows"] == 8
        assert bsum["unresolved_reviews_in_source"] == 0
        assert len(list((bridge / "status").glob("*.json"))) == 8
        repo_ann = json.loads((bridge / "annotations/PXD900201/PXD900201__repository_only.json").read_text())
        assert repo_ann["is_single_cell_proteomics"] == "yes"

    print("All v0.1.8 semantic-unification regression tests passed.")
    print("Stage-04 and repository labels are normalized before routing.")
    print("Repository triage overcalls are detected from its own structured evidence.")
    print("Review candidates cannot enter a final Stage-05 bridge without explicit decisions.")
    print("Repository-only candidates can be synthesized into Stage-05-compatible annotations after adjudication.")


if __name__ == "__main__":
    main()
