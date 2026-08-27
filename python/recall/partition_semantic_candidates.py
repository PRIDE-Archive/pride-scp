#!/usr/bin/env python3
"""Partition recall candidates by available publication content without loss.

v0.1.7 accepts generic publication content: validated PDFs or normalized
full-text artifacts produced by Stage 03.  A candidate is publication-backed
when at least one usable publication-content artifact exists; otherwise it is
repository-only.  No candidate is rejected.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("semantic_candidates_jsonl")
    parser.add_argument(
        "--content-manifest",
        default="",
        help="Stage-03 publication content TSV (preferred in v0.1.7).",
    )
    parser.add_argument(
        "--pdf-manifest",
        default="",
        help="Backward-compatible Stage-02 PDF TSV.",
    )
    parser.add_argument("--output-dir", default="work/semantic_partition")
    args = parser.parse_args()
    if not args.content_manifest and not args.pdf_manifest:
        parser.error("one of --content-manifest or --pdf-manifest is required")
    return args


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def valid_pdf(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with path.open("rb") as handle:
            return handle.read(5) == b"%PDF-"
    except OSError:
        return False


def valid_text(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 500:
        return False
    try:
        return len(path.read_text(encoding="utf-8").strip()) >= 500
    except (OSError, UnicodeError):
        return False


def usable_content(row: dict[str, str]) -> tuple[bool, str, str]:
    status = (row.get("publication_content_status") or "").strip().lower()
    kind = (row.get("publication_content_kind") or "").strip().lower()
    path_text = (row.get("publication_content_path") or "").strip()
    if status == "available" and path_text:
        path = Path(path_text)
        if kind == "pdf" and valid_pdf(path):
            return True, "pdf", str(path.resolve())
        if kind in {"fulltext_xml", "fulltext_html", "text", "fulltext_text"} and valid_text(path):
            return True, kind, str(path.resolve())

    # Backward compatibility with v0.1.4-v0.1.6 PDF-only manifests.
    pdf_status = (row.get("pdf_status") or "").strip().lower()
    pdf_path_text = (row.get("pdf_path") or "").strip()
    if pdf_status in {"downloaded", "already_exists"} and pdf_path_text:
        path = Path(pdf_path_text)
        if valid_pdf(path):
            return True, "pdf", str(path.resolve())
    return False, "", ""


def load_content_rows(path: Path) -> dict[str, list[dict[str, str]]]:
    by_accession: dict[str, list[dict[str, str]]] = defaultdict(list)
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        for row in reader:
            accession = (row.get("accession") or row.get("pxd_accession") or "").strip().upper()
            if not accession.startswith("PXD"):
                continue
            ok, kind, resolved_path = usable_content(row)
            if not ok:
                continue
            enriched = dict(row)
            enriched["_usable_content_kind"] = kind
            enriched["_usable_content_path"] = resolved_path
            by_accession[accession].append(enriched)
    return by_accession


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> None:
    args = parse_args()
    candidates = load_jsonl(Path(args.semantic_candidates_jsonl))
    manifest = Path(args.content_manifest or args.pdf_manifest)
    content_rows = load_content_rows(manifest)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)

    publication_backed: list[dict[str, Any]] = []
    repository_only: list[dict[str, Any]] = []
    content_kind_counts: Counter[str] = Counter()

    for candidate in candidates:
        accession = str(candidate.get("accession", "")).upper()
        matching = content_rows.get(accession, [])
        if matching:
            enriched = dict(candidate)
            enriched["semantic_evidence_mode"] = "publication_backed"
            enriched["publication_content_count"] = len(matching)
            contents = []
            pdfs = []
            for row in matching:
                kind = row.get("_usable_content_kind", "")
                path = row.get("_usable_content_path", "")
                content_kind_counts[kind] += 1
                item = {
                    "content_kind": kind,
                    "content_path": path,
                    "content_source": row.get("publication_content_source", ""),
                    "publication_title": row.get("publication_title", ""),
                    "publication_doi": row.get("publication_doi", ""),
                    "publication_index": row.get("publication_index", ""),
                }
                contents.append(item)
                if kind == "pdf":
                    pdfs.append({
                        "pdf_path": path,
                        "publication_title": row.get("publication_title", ""),
                        "publication_doi": row.get("publication_doi", ""),
                        "publication_index": row.get("publication_index", ""),
                    })
            enriched["publication_contents"] = contents
            # Retained for Stage-05/backward-compatible downstream readers.
            enriched["publication_pdf_count"] = len(pdfs)
            enriched["publication_pdfs"] = pdfs
            publication_backed.append(enriched)
        else:
            enriched = dict(candidate)
            enriched["semantic_evidence_mode"] = "repository_only"
            enriched["publication_content_count"] = 0
            enriched["publication_contents"] = []
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
        "publication_content_artifacts": sum(content_kind_counts.values()),
        "publication_content_kinds": dict(sorted(content_kind_counts.items())),
        "note": "Every semantic candidate is assigned exactly one evidence mode; no candidate is rejected.",
    }
    (output / "semantic_partition_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
