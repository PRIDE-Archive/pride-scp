#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "python/catalogue/build_pride_production_catalogue.py"


def write_tsv(path: Path, rows: list[dict[str, object]]) -> None:
    fields = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        candidates = root / "candidates.jsonl"
        diagnostics = root / "diagnostics.tsv"
        curation = root / "curation.tsv"
        out = root / "catalogue"
        candidate_rows = [
            {
                "accession": "PXD900001",
                "dataset_title": "Single-cell proteomics test",
                "dataset_description": "one cell by LC-MS",
                "score": 90,
                "tier": "strong",
                "project_json_path": str(root / "snapshot/projects/PXD900001.json"),
                "files_json_path": "",
                "sdrf_path": "",
            },
            {
                "accession": "PXD900002",
                "dataset_title": "Ambiguous test",
                "dataset_description": "single cell context",
                "score": 12,
                "tier": "weak",
                "project_json_path": str(root / "snapshot/projects/PXD900002.json"),
                "files_json_path": "",
                "sdrf_path": "",
            },
        ]
        candidates.write_text("\n".join(json.dumps(x) for x in candidate_rows) + "\n", encoding="utf-8")
        write_tsv(
            diagnostics,
            [
                {"accession": "PXD900001", "semantic_priority": "A_specific", "evidence_profile": "specific"},
                {"accession": "PXD900002", "semantic_priority": "C_broad", "evidence_profile": "broad_only"},
            ],
        )
        write_tsv(
            curation,
            [
                {
                    "accession": "PXD900001",
                    "run_status": "success",
                    "curation_decision": "include",
                    "decision_reason": "grounded qualifying one-cell branch",
                    "model": "qwen2.5:3b",
                    "evidence_sufficiency": "sufficient",
                    "true_single_cell_ms_samples_present": "yes",
                },
                {
                    "accession": "PXD900002",
                    "run_status": "success",
                    "curation_decision": "review",
                    "decision_reason": "insufficient grounding",
                    "model": "qwen2.5:3b",
                    "evidence_sufficiency": "insufficient",
                    "true_single_cell_ms_samples_present": "uncertain",
                },
            ],
        )
        subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--candidates-jsonl",
                str(candidates),
                "--candidate-diagnostics",
                str(diagnostics),
                "--curation-summary",
                str(curation),
                "--curation-version",
                "v19-shadow-2.7.1",
                "--output-dir",
                str(out),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        summary = json.loads((out / "pride_scp_production_catalogue_summary.json").read_text())
        assert summary["candidate_count"] == 2
        assert summary["automated_catalogue_includes"] == 1
        assert summary["review_queue"] == 1
        assert summary["gt_used_for_curation"] is False
        assert sum(1 for _ in csv.DictReader((out / "pride_scp_catalogue_automated_includes.csv").open())) == 1
        assert sum(1 for _ in csv.DictReader((out / "pride_scp_review_queue.csv").open())) == 1
    print("PRIDE production catalogue regression tests passed")


if __name__ == "__main__":
    main()
