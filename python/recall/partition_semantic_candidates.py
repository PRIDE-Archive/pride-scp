#!/usr/bin/env python3
"""
Partition Rust-discovered semantic candidates into publication-backed and
repository-only evidence modes without dropping any accession.

The input candidate JSONL is produced by `pride-scp export-python`.
The PDF manifest is the Stage-02 TSV containing `accession`, `pdf_status`,
and `pdf_path` columns.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "semantic_candidates_jsonl",
        help="Rust bridge semantic_candidates.jsonl",
    )
    parser.add_argument(
        "--pdf-manifest",
        required=True,
        help="Stage-02 publication/PDF TSV",
    )
    parser.add_argument(
        "--output-dir",
        default="work/semantic_partition",
    )
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def valid_pdf(path_text: str) -> bool:
    if not path_text:
        return False
    path = Path(path_text)
    if not path.is_file():
        return False
    try:
        with path.open("rb") as handle:
            return handle.read(5) == b"%PDF-"
    except OSError:
        return False


def load_pdf_rows(path: Path) -> dict[str, list[dict[str, str]]]:
    by_accession: dict[str, list[dict[str, str]]] = defaultdict(list)
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        for row in reader:
            accession = (row.get("accession") or row.get("pxd_accession") or "").strip().upper()
            if not accession.startswith("PXD"):
                continue
            status = (row.get("pdf_status") or "").strip().lower()
            pdf_path = (row.get("pdf_path") or "").strip()
            if status not in {"downloaded", "already_exists"} or not valid_pdf(pdf_path):
                continue
            by_accession[accession].append(dict(row))
    return by_accession


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> None:
    args = parse_args()
    candidates = load_jsonl(Path(args.semantic_candidates_jsonl))
    pdf_rows = load_pdf_rows(Path(args.pdf_manifest))
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)

    publication_backed: list[dict[str, Any]] = []
    repository_only: list[dict[str, Any]] = []

    for candidate in candidates:
        accession = str(candidate.get("accession", "")).upper()
        matching = pdf_rows.get(accession, [])
        if matching:
            enriched = dict(candidate)
            enriched["semantic_evidence_mode"] = "publication_backed"
            enriched["publication_pdf_count"] = len(matching)
            enriched["publication_pdfs"] = [
                {
                    "pdf_path": row.get("pdf_path", ""),
                    "publication_title": row.get("publication_title", ""),
                    "publication_doi": row.get("publication_doi", ""),
                    "publication_index": row.get("publication_index", ""),
                }
                for row in matching
            ]
            publication_backed.append(enriched)
        else:
            enriched = dict(candidate)
            enriched["semantic_evidence_mode"] = "repository_only"
            enriched["publication_pdf_count"] = 0
            enriched["publication_pdfs"] = []
            repository_only.append(enriched)

    publication_backed.sort(key=lambda row: str(row.get("accession", "")))
    repository_only.sort(key=lambda row: str(row.get("accession", "")))

    write_jsonl(output / "publication_backed_candidates.jsonl", publication_backed)
    write_jsonl(output / "repository_only_candidates.jsonl", repository_only)

    for filename, rows in [
        ("publication_backed_accessions.txt", publication_backed),
        ("repository_only_accessions.txt", repository_only),
    ]:
        (output / filename).write_text(
            "".join(f"{row.get('accession', '')}\n" for row in rows),
            encoding="utf-8",
        )

    summary = {
        "candidates": len(candidates),
        "publication_backed": len(publication_backed),
        "repository_only": len(repository_only),
        "candidate_loss": len(candidates) - len(publication_backed) - len(repository_only),
        "note": "Every semantic candidate is assigned exactly one evidence mode; no candidate is rejected.",
    }
    (output / "semantic_partition_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
