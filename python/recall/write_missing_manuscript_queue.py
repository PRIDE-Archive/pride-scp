#!/usr/bin/env python3
"""Write a human-friendly queue of PXD accessions still missing manuscripts.

The queue is generated *after* PDF and full-text XML recovery so it contains
only publications for which richer manuscript evidence is still unavailable.
It also writes a manual PDF manifest template that can explicitly link one
local PDF to one or multiple PXD accessions.
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
    parser.add_argument("content_manifest", help="Stage-03 publication-content TSV")
    parser.add_argument("--output-dir", default="work/python/manual_manuscripts")
    parser.add_argument(
        "--manual-pdf-dir",
        default="manual_pdfs",
        help="Directory where manually downloaded PDFs should be stored.",
    )
    return parser.parse_args()


def text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def publication_group_key(row: dict[str, str]) -> str:
    doi = text(row.get("resolved_doi") or row.get("publication_doi")).lower()
    if doi:
        return "doi:" + doi
    pmid = text(row.get("resolved_pmid") or row.get("publication_pmid"))
    if pmid:
        return "pmid:" + pmid
    pmcid = text(row.get("resolved_pmcid") or row.get("publication_pmcid")).upper()
    if pmcid:
        return "pmcid:" + pmcid
    title = text(row.get("publication_title")).lower()
    if title:
        return "title:" + title
    accession = text(row.get("accession")).upper()
    index = text(row.get("publication_index"))
    return f"row:{accession}:{index}"


def suggested_filename(row: dict[str, str]) -> str:
    supplied = text(row.get("manual_pdf_suggested_filename"))
    if supplied:
        return supplied
    doi = text(row.get("resolved_doi") or row.get("publication_doi"))
    if doi:
        safe = "".join(ch if ch.isalnum() or ch in "._+-" else "_" for ch in doi)
        return safe[:120] + ".pdf"
    pmid = text(row.get("resolved_pmid") or row.get("publication_pmid"))
    if pmid:
        return f"PMID_{pmid}.pdf"
    accession = text(row.get("accession")).upper() or "publication"
    return accession + ".pdf"


def main() -> None:
    args = parse_args()
    rows = load_rows(Path(args.content_manifest))
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    manual_dir = Path(args.manual_pdf_dir)

    unresolved_groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    missing_pdf_groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    no_publication_accessions: set[str] = set()

    for row in rows:
        accession = text(row.get("accession")).upper()
        if not accession.startswith("PXD"):
            continue
        publication_status = text(row.get("publication_status"))
        if publication_status != "publication_found":
            no_publication_accessions.add(accession)
            continue
        key = publication_group_key(row)
        pdf_status = text(row.get("pdf_status")).lower()
        pdf_path = text(row.get("pdf_path"))
        if pdf_status not in {"downloaded", "already_exists"} or not pdf_path:
            missing_pdf_groups[key].append(row)
        content_status = text(row.get("publication_content_status")).lower()
        content_path = text(row.get("publication_content_path"))
        if content_status == "available" and content_path:
            continue
        unresolved_groups[key].append(row)

    queue_rows: list[dict[str, Any]] = []
    missing_accessions: set[str] = set()
    missing_pdf_accessions: set[str] = set()
    missing_pdf_rows: list[dict[str, Any]] = []
    template_rows: list[dict[str, Any]] = []

    for key, group in missing_pdf_groups.items():
        rep = group[0]
        accessions = sorted({text(row.get("accession")).upper() for row in group if text(row.get("accession")).upper().startswith("PXD")})
        missing_pdf_accessions.update(accessions)
        missing_pdf_rows.append({
            "accessions": ";".join(accessions),
            "publication_key": key,
            "publication_title": text(rep.get("publication_title")),
            "publication_doi": text(rep.get("resolved_doi") or rep.get("publication_doi")),
            "publication_pmid": text(rep.get("resolved_pmid") or rep.get("publication_pmid")),
            "publication_pmcid": text(rep.get("resolved_pmcid") or rep.get("publication_pmcid")),
            "pdf_status": text(rep.get("pdf_status")),
            "pdf_error": text(rep.get("pdf_error")),
            "xml_fulltext_available": "Y" if text(rep.get("publication_content_kind")).lower() == "fulltext_xml" and text(rep.get("publication_content_status")).lower() == "available" else "N",
            "article_url": text(rep.get("manual_article_url") or rep.get("publication_url")),
            "suggested_filename": suggested_filename(rep),
        })

    for key, group in unresolved_groups.items():
        rep = group[0]
        accessions = sorted({text(row.get("accession")).upper() for row in group if text(row.get("accession")).upper().startswith("PXD")})
        missing_accessions.update(accessions)
        filename = suggested_filename(rep)
        doi = text(rep.get("resolved_doi") or rep.get("publication_doi"))
        pmid = text(rep.get("resolved_pmid") or rep.get("publication_pmid"))
        pmcid = text(rep.get("resolved_pmcid") or rep.get("publication_pmcid"))
        article_url = text(rep.get("manual_article_url") or rep.get("publication_url"))
        queue_rows.append({
            "accessions": ";".join(accessions),
            "accession_count": len(accessions),
            "publication_key": key,
            "publication_title": text(rep.get("publication_title")),
            "publication_doi": doi,
            "publication_pmid": pmid,
            "publication_pmcid": pmcid,
            "pdf_status": text(rep.get("pdf_status")),
            "publication_content_status": text(rep.get("publication_content_status")),
            "reason": text(rep.get("publication_content_error") or rep.get("pdf_error")),
            "article_url": article_url,
            "suggested_filename": filename,
            "save_to": str(manual_dir / filename),
            "manual_action": "download PDF and save at save_to; then rerun scripts/run_python_publication_enrichment.sh",
        })
        for accession in accessions:
            template_rows.append({
                "accession": accession,
                "publication_doi": doi,
                "publication_pmid": pmid,
                "publication_pmcid": pmcid,
                "pdf_path": filename,
                "note": "manual manuscript mapping",
            })

    queue_rows.sort(key=lambda row: (row["accessions"], row["publication_key"]))
    template_rows.sort(key=lambda row: (row["accession"], row["publication_doi"], row["pdf_path"]))

    queue_fields = [
        "accessions",
        "accession_count",
        "publication_key",
        "publication_title",
        "publication_doi",
        "publication_pmid",
        "publication_pmcid",
        "pdf_status",
        "publication_content_status",
        "reason",
        "article_url",
        "suggested_filename",
        "save_to",
        "manual_action",
    ]
    write_tsv(output / "missing_manuscript_publications.tsv", queue_rows, queue_fields)

    missing_pdf_rows.sort(key=lambda row: (row["accessions"], row["publication_key"]))
    write_tsv(
        output / "missing_pdf_publications.tsv",
        missing_pdf_rows,
        [
            "accessions", "publication_key", "publication_title",
            "publication_doi", "publication_pmid", "publication_pmcid",
            "pdf_status", "pdf_error", "xml_fulltext_available",
            "article_url", "suggested_filename",
        ],
    )
    (output / "missing_pdf_accessions.txt").write_text(
        "".join(accession + "\n" for accession in sorted(missing_pdf_accessions)),
        encoding="utf-8",
    )

    (output / "missing_manuscript_accessions.txt").write_text(
        "".join(accession + "\n" for accession in sorted(missing_accessions)),
        encoding="utf-8",
    )
    (output / "missing_publication_linkage_accessions.txt").write_text(
        "".join(accession + "\n" for accession in sorted(no_publication_accessions)),
        encoding="utf-8",
    )

    manifest_fields = [
        "accession",
        "publication_doi",
        "publication_pmid",
        "publication_pmcid",
        "pdf_path",
        "note",
    ]
    write_tsv(output / "manual_pdf_manifest.template.tsv", template_rows, manifest_fields)

    readme = f"""Manual manuscript queue generated from:\n  {Path(args.content_manifest)}\n\nMissing publication manuscripts: {len(queue_rows)} unique publications\nAffected PXD accessions: {len(missing_accessions)}\nPXD accessions with no linked publication metadata: {len(no_publication_accessions)}\n\nWhere to save PDFs\n------------------\nSave manually downloaded PDF files in:\n  {manual_dir}\n\nPreferred workflow\n------------------\n1. Open missing_manuscript_publications.tsv.\n2. Search by DOI/title/article_url.\n3. Save the PDF using the row's suggested_filename under the manual PDF directory.\n4. For explicit PXD-to-PDF linking, copy manual_pdf_manifest.template.tsv to:\n     {manual_dir / 'manual_pdf_manifest.tsv'}\n   The same pdf_path may appear on multiple rows when one paper maps to multiple PXD accessions.\n5. Rerun scripts/run_python_publication_enrichment.sh.\n\nThe Stage-02 resolver validates PDF magic bytes before accepting a manual file.\n"""
    (output / "README.txt").write_text(readme, encoding="utf-8")

    summary = {
        "missing_pdf_publications": len(missing_pdf_rows),
        "missing_pdf_accessions": len(missing_pdf_accessions),
        "missing_manuscript_publications": len(queue_rows),
        "missing_manuscript_accessions": len(missing_accessions),
        "missing_publication_linkage_accessions": len(no_publication_accessions),
        "manual_pdf_dir": str(manual_dir),
        "queue": str(output / "missing_manuscript_publications.tsv"),
        "accession_list": str(output / "missing_manuscript_accessions.txt"),
        "manifest_template": str(output / "manual_pdf_manifest.template.tsv"),
    }
    (output / "missing_manuscript_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
