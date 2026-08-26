#!/usr/bin/env python3
"""Small live network smoke test for known Europe-PMC-backed SCP papers."""

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
import tempfile
from pathlib import Path

CASES = [
    ("PXD037527", "10.1002/anie.202303415"),
    ("PXD043473", "10.1021/jasms.3c00242"),
    ("PXD049412", "10.1038/s41592-024-02559-1"),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-output", default="")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[2]
    stage = root / "python" / "stages" / "02_download_publication_pdfs.py"

    if args.keep_output:
        work = Path(args.keep_output)
        work.mkdir(parents=True, exist_ok=True)
        cleanup = None
    else:
        cleanup = tempfile.TemporaryDirectory()
        work = Path(cleanup.name)

    input_tsv = work / "known_oa_publications.tsv"
    output_tsv = work / "known_oa_publications_with_pdfs.tsv"
    pdf_dir = work / "pdfs"

    fields = [
        "accession",
        "publication_status",
        "publication_index",
        "publication_doi",
        "publication_pmid",
        "publication_pmcid",
        "publication_title",
        "publication_is_open_access",
        "publication_pdf_candidate_url",
    ]
    with input_tsv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        for accession, doi in CASES:
            writer.writerow(
                {
                    "accession": accession,
                    "publication_status": "publication_found",
                    "publication_index": "1",
                    "publication_doi": doi,
                }
            )

    subprocess.run(
        [
            sys.executable,
            str(stage),
            str(input_tsv),
            "--pdf-dir",
            str(pdf_dir),
            "--output",
            str(output_tsv),
            "--manual-queue",
            str(work / "manual_pdf_queue.tsv"),
            "--workers",
            "3",
            "--retry-unresolved",
        ],
        check=True,
        cwd=str(root),
    )

    with output_tsv.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    success = [
        row for row in rows if row.get("pdf_status") in {"downloaded", "already_exists"}
    ]
    print(f"Live known-OA smoke: {len(success)}/{len(CASES)} usable PDFs")
    for row in rows:
        print(
            row.get("accession"),
            row.get("pdf_status"),
            row.get("resolved_pmcid"),
            row.get("pdf_source"),
        )
        if row.get("pdf_status") not in {"downloaded", "already_exists"}:
            print("  ", row.get("pdf_error", ""))
    if len(success) != len(CASES):
        raise SystemExit(1)
    if args.keep_output:
        print(f"Kept smoke-test output: {work}")
    if cleanup is not None:
        cleanup.cleanup()


if __name__ == "__main__":
    main()
