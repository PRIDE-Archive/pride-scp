#!/usr/bin/env python3
"""Adapt cached publication manifests into provenance-hardened SDRF evidence sources.

This module does not extract SDRF field claims from manuscript text.  It only makes already
materialized, accession-linked publication content visible to the evidence registry.  Duplicate
materializations of the same scientific publication are collapsed by canonical publication
identity (DOI, then PMCID, then PMID), while the selected artifact remains content-addressed.
"""
from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path
from typing import Any

VERSION = "pride-scp-sdrf-publication-evidence-v0.1.0"

FIELDS = [
    "accession", "source_kind", "source_provider", "source_locator", "source_identity",
    "publication_doi", "publication_pmid", "publication_pmcid", "publication_identity_status",
    "local_path", "trust_class", "blocker_field", "derivation_operation", "retrieval_method",
    "original_filename", "media_type",
]


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
        return [dict(row) for row in csv.DictReader(fh, delimiter="\t")]


def norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_doi(value: Any) -> str:
    value = norm(value).lower()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value)
    return value.removeprefix("doi:").strip()


def normalize_pmcid(value: Any) -> str:
    value = norm(value).upper()
    if value.isdigit():
        value = "PMC" + value
    return value


def normalize_pmid(value: Any) -> str:
    return re.sub(r"\D", "", norm(value))


def publication_identity(doi: str, pmcid: str, pmid: str) -> str:
    if doi:
        return f"doi:{doi}"
    if pmcid:
        return f"pmcid:{pmcid}"
    if pmid:
        return f"pmid:{pmid}"
    return ""


def source_locator(doi: str, pmcid: str, pmid: str, row: dict[str, str]) -> str:
    if doi:
        return f"https://doi.org/{doi}"
    if pmcid:
        return f"https://europepmc.org/articles/{pmcid}"
    if pmid:
        return f"https://europepmc.org/article/MED/{pmid}"
    return norm(row.get("publication_url"))


def content_path(row: dict[str, str], manifest: Path) -> Path | None:
    for key in (
        "publication_content_text_path",
        "publication_content_path",
        "content_text_path",
        "text_path",
    ):
        raw = norm(row.get(key))
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = (manifest.parent / path).resolve()
        if path.is_file():
            return path
    return None


def identity_status(row: dict[str, str]) -> tuple[str, bool]:
    external = norm(row.get("external_recovery_status")).lower()
    if external:
        return f"external_recovery:{external}", external == "accepted"

    selected = norm(row.get("selection_status") or row.get("selected_status")).lower()
    if selected:
        return f"local_selection:{selected}", selected in {"accepted", "selected"}

    publication_status = norm(row.get("publication_status")).lower()
    if publication_status == "publication_found":
        return "publication_found_with_stable_identifier", True
    return "publication_identity_unverified", False


def build_publication_source_rows(manifests: list[Path]) -> tuple[list[dict[str, str]], dict[str, Any]]:
    selected: dict[tuple[str, str], tuple[tuple[int, int, int], dict[str, str]]] = {}
    skipped_missing_content = 0
    skipped_unverified_identity = 0
    skipped_missing_identifier = 0
    duplicate_materializations = 0

    for manifest in manifests:
        for row in read_tsv(manifest):
            accession = norm(row.get("accession") or row.get("project_accession")).upper()
            if not accession.startswith("PXD"):
                continue
            path = content_path(row, manifest)
            if path is None:
                skipped_missing_content += 1
                continue
            doi = normalize_doi(row.get("publication_doi") or row.get("doi"))
            pmcid = normalize_pmcid(row.get("publication_pmcid") or row.get("pmcid"))
            pmid = normalize_pmid(row.get("publication_pmid") or row.get("pmid"))
            identity = publication_identity(doi, pmcid, pmid)
            if not identity:
                skipped_missing_identifier += 1
                continue
            status, trusted = identity_status(row)
            if not trusted:
                skipped_unverified_identity += 1
                continue
            provider = norm(row.get("publication_content_source")) or "cached_publication_manifest"
            locator = source_locator(doi, pmcid, pmid, row)
            item = {
                "accession": accession,
                "source_kind": "publication_fulltext",
                "source_provider": provider,
                "source_locator": locator,
                "source_identity": identity,
                "publication_doi": doi,
                "publication_pmid": pmid,
                "publication_pmcid": pmcid,
                "publication_identity_status": status,
                "local_path": str(path),
                "trust_class": "trusted_independent",
                # Publication presence alone is not blocker-field support.  A later claim
                # extractor must emit field-scoped evidence before a resolver may use it.
                "blocker_field": "",
                "derivation_operation": "materialize",
                "retrieval_method": provider,
                "original_filename": path.name,
                "media_type": "text/plain" if path.suffix.lower() == ".txt" else "application/xml",
            }
            # Prefer explicit accepted recovery, then richer stable identifiers, then the
            # larger materialization.  This is deterministic and does not imply extra evidence.
            score = (
                int(status == "external_recovery:accepted"),
                sum(bool(x) for x in (doi, pmcid, pmid)),
                path.stat().st_size,
            )
            key = (accession, identity)
            if key in selected:
                duplicate_materializations += 1
            if key not in selected or score > selected[key][0]:
                selected[key] = (score, item)

    rows = [entry[1] for _, entry in sorted(selected.items())]
    summary = {
        "version": VERSION,
        "source_manifests": [str(path) for path in manifests],
        "records": len(rows),
        "accessions": len({row["accession"] for row in rows}),
        "publication_identities": len({(row["accession"], row["source_identity"]) for row in rows}),
        "duplicate_materializations_collapsed": duplicate_materializations,
        "skipped_missing_content": skipped_missing_content,
        "skipped_unverified_identity": skipped_unverified_identity,
        "skipped_missing_identifier": skipped_missing_identifier,
        "field_claims_emitted": 0,
    }
    return rows, summary


def write_tsv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publication-manifest", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows, _ = build_publication_source_rows([path.resolve() for path in args.publication_manifest])
    write_tsv(args.output, rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
