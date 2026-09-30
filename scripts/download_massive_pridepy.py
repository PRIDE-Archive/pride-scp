#!/usr/bin/env python3
"""Stage public MassIVE datasets with the pridepy backend bundled in PRIDE-SCP.

The default ``https-index`` mode deliberately bypasses MassIVE's recursive FTPS
walk.  It obtains the dataset file inventory from pridepy's GNPS2 HTTPS index
and downloads the corresponding MassIVE/ProteoSAFe HTTPS URLs.  This is useful
on HPC systems where a very large Bruker ``.d`` tree makes FTPS enumeration
slow or connection-sensitive.

This helper intentionally pins itself to pridepy 0.0.16 because it calls the
private ``MassiveProvider._list_via_https`` method.  If the container pin is
updated, review this helper at the same time.
"""

from __future__ import annotations

import argparse
import csv
import importlib.metadata
import logging
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable

from pridepy.download.massive import MassiveProvider

EXPECTED_PRIDEPY_VERSION = "0.0.16"
ACCESSION_RE = re.compile(r"^R?MSV\d{9}$", re.IGNORECASE)
VALID_CATEGORIES = {
    "RAW",
    "PEAK",
    "SEARCH",
    "RESULT",
    "SPECTRUM_LIBRARY",
    "FASTA",
    "OTHER",
}


def _read_dataset_list(path: Path) -> list[str]:
    values: list[str] = []
    with path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            # Allow a TSV with notes after the first column.
            values.append(line.split("\t", 1)[0].strip())
    return values


def _normalise_accessions(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        accession = value.strip().upper()
        if accession.startswith("RMSV"):
            accession = accession[1:]
        if not ACCESSION_RE.fullmatch(accession):
            raise ValueError(f"Invalid MassIVE accession: {value!r}")
        if accession not in seen:
            seen.add(accession)
            out.append(accession)
    if not out:
        raise ValueError("No MassIVE accessions were supplied")
    return out


def _record_category(record: dict) -> str:
    return str((record.get("fileCategory") or {}).get("value") or "OTHER").upper()


def _write_manifest(path: Path, accession: str, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(
            [
                "accession",
                "category",
                "collection",
                "relative_path",
                "url",
            ]
        )
        for record in records:
            locations = record.get("publicFileLocations") or []
            url = str(locations[0].get("value") or "") if locations else ""
            writer.writerow(
                [
                    accession,
                    _record_category(record),
                    record.get("collection") or "",
                    record.get("relativePath") or "",
                    url,
                ]
            )


def _get_records(
    provider: MassiveProvider,
    accession: str,
    transport: str,
) -> list[dict]:
    if transport == "https-index":
        version = importlib.metadata.version("pridepy")
        if version != EXPECTED_PRIDEPY_VERSION:
            raise RuntimeError(
                "https-index mode relies on pridepy's private "
                f"MassiveProvider._list_via_https API and expects "
                f"pridepy=={EXPECTED_PRIDEPY_VERSION}; found {version}"
            )
        return provider._list_via_https(accession)  # noqa: SLF001 - pinned API
    if transport == "auto":
        return provider.list_files(accession)
    raise ValueError(f"Unsupported transport: {transport}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Stage one or more public MassIVE datasets using pridepy. "
            "By default the file inventory is resolved over HTTPS rather than "
            "recursively walking the MassIVE FTPS tree."
        )
    )
    parser.add_argument(
        "--accession",
        action="append",
        default=[],
        help="MassIVE accession (MSV#########). Repeat for multiple datasets.",
    )
    parser.add_argument(
        "--dataset-list",
        type=Path,
        help="Text/TSV file with one MassIVE accession per non-comment line.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        required=True,
        help="Root directory. Each dataset is written under <root>/<MSV accession>/.",
    )
    parser.add_argument(
        "--manifest-root",
        type=Path,
        help="Optional directory for per-dataset TSV manifests.",
    )
    parser.add_argument(
        "--completion-root",
        type=Path,
        help="Optional directory for <accession>.complete marker files.",
    )
    parser.add_argument(
        "--transport",
        choices=("https-index", "auto"),
        default="https-index",
        help=(
            "https-index bypasses recursive FTPS discovery (default); auto uses "
            "pridepy's normal MassIVE FTPS-first behaviour with HTTPS fallback."
        ),
    )
    parser.add_argument(
        "--category",
        action="append",
        default=[],
        help=(
            "Restrict to a pridepy category. Repeat as needed. Valid: "
            + ", ".join(sorted(VALID_CATEGORIES))
        ),
    )
    parser.add_argument(
        "--parallel-files",
        type=int,
        choices=(1, 2, 3),
        default=3,
        help="Concurrent file downloads within one dataset (default: 3).",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Resolve records/write manifests but do not download files.",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    values = list(args.accession)
    if args.dataset_list is not None:
        values.extend(_read_dataset_list(args.dataset_list))
    try:
        accessions = _normalise_accessions(values)
    except ValueError as exc:
        logging.error("%s", exc)
        return 2

    categories = {item.upper() for item in args.category}
    invalid_categories = categories - VALID_CATEGORIES
    if invalid_categories:
        logging.error(
            "Invalid categories: %s; valid categories: %s",
            ", ".join(sorted(invalid_categories)),
            ", ".join(sorted(VALID_CATEGORIES)),
        )
        return 2

    pridepy_version = importlib.metadata.version("pridepy")
    logging.info("pridepy=%s transport=%s", pridepy_version, args.transport)

    provider = MassiveProvider()
    args.output_root.mkdir(parents=True, exist_ok=True)
    if args.manifest_root is not None:
        args.manifest_root.mkdir(parents=True, exist_ok=True)
    if args.completion_root is not None:
        args.completion_root.mkdir(parents=True, exist_ok=True)

    for accession in accessions:
        logging.info("Resolving MassIVE dataset %s", accession)
        records = _get_records(provider, accession, args.transport)
        if categories:
            records = [r for r in records if _record_category(r) in categories]
        if not records:
            raise RuntimeError(
                f"No records remain for {accession} after applying category filters"
            )

        counts = Counter(_record_category(record) for record in records)
        logging.info("%s selected_files=%d", accession, len(records))
        for category, count in sorted(counts.items()):
            logging.info("%s category=%s files=%d", accession, category, count)

        if args.manifest_root is not None:
            manifest = args.manifest_root / f"{accession}.https-manifest.tsv"
            _write_manifest(manifest, accession, records)
            logging.info("%s manifest=%s", accession, manifest)

        if args.list_only:
            continue

        destination = args.output_root / accession
        destination.mkdir(parents=True, exist_ok=True)
        provider.download_files(
            accession=accession,
            records=records,
            output_folder=str(destination),
            # False is intentional: the HTTPS transport checks existing file
            # size and can Range-resume partial files.  True would blindly skip
            # an existing path before that completeness check.
            skip_if_downloaded_already=False,
            protocol="https",
            parallel_files=args.parallel_files,
            checksum_check=False,
            flatten=False,
        )

        if args.completion_root is not None:
            marker = args.completion_root / f"{accession}.complete"
            marker.write_text(
                f"accession={accession}\nfiles={len(records)}\npridepy={pridepy_version}\n",
                encoding="utf-8",
            )
            logging.info("%s completion_marker=%s", accession, marker)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logging.error("Interrupted")
        sys.exit(130)
