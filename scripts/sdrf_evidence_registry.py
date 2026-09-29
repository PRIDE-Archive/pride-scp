#!/usr/bin/env python3
"""Build provenance-hardened evidence records for the PRIDE-SCP SDRF harness.

v2.2 keeps immutable source lineage when evidence is copied, parsed or materialized.
Presence in a workspace never upgrades trust. Candidate-derived ancestry fails closed, while
safe derivatives of independently sourced artifacts retain independence through parent SHA
lineage.
"""
from __future__ import annotations

import argparse
import csv
import json
import mimetypes
import tempfile
from pathlib import Path
from typing import Any

from sdrf_annotation_state import VERSION as STATE_VERSION, sha256_file

VERSION = "pride-scp-sdrf-evidence-registry-v2.2.0"

PATH_KEYS = ("local_path", "path", "source_path", "artifact_path", "file_path")
ACCESSION_KEYS = ("accession", "project_accession", "pxd")
SHA_KEYS = ("artifact_sha256", "source_sha256", "sha256", "file_sha256")
SOURCE_KIND_KEYS = ("source_kind", "source_class", "artifact_type", "type")
SOURCE_PROVIDER_KEYS = ("source_provider", "provider", "repository")
SOURCE_LOCATOR_KEYS = (
    "source_locator",
    "source_url",
    "url",
    "original_url",
    "download_url",
    "repository_url",
)
TRUST_KEYS = ("trust_class", "trusted_source", "source_trust")
BLOCKER_FIELD_KEYS = ("blocker_field", "field", "target_field", "sdrf_field")
PARENT_SHA_KEYS = ("parent_artifact_sha256", "parent_sha256")
DERIVATION_KEYS = ("derivation_operation", "operation", "producer_stage")
RETRIEVED_AT_KEYS = ("retrieved_at", "retrieval_time", "downloaded_at")
RETRIEVAL_METHOD_KEYS = ("retrieval_method", "fetch_method", "download_method")
ORIGINAL_FILENAME_KEYS = ("original_filename", "source_filename", "remote_filename")
MEDIA_TYPE_KEYS = ("media_type", "mime_type", "content_type")
SIZE_KEYS = ("artifact_size_bytes", "byte_size", "size_bytes")

TRUSTED_EXTERNAL_CLASSES = {"trusted_independent"}
TRUSTED_DEPOSITED_CLASSES = {"trusted_deposited", "trusted_local_deposited_sdrf"}
TRUSTED_SUPPLEMENT_CLASSES = {"trusted_publication_supplement"}
TRUSTED_CLASSES = TRUSTED_EXTERNAL_CLASSES | TRUSTED_DEPOSITED_CLASSES | TRUSTED_SUPPLEMENT_CLASSES

# These operations preserve the scientific source identity; they do not themselves invent values.
SAFE_DERIVATIONS = {
    "copy",
    "materialize",
    "download_copy",
    "archive_member",
    "decompress",
    "parse",
    "parse_table",
    "extract_table",
    "normalize_schema",
}


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
        return [dict(row) for row in csv.DictReader(fh, delimiter="\t")]


def first(row: dict[str, str], keys: tuple[str, ...]) -> str:
    lower = {str(k).lower(): str(v or "").strip() for k, v in row.items()}
    for key in keys:
        value = lower.get(key.lower(), "")
        if value:
            return value
    return ""


def normalize_accession(row: dict[str, str]) -> str:
    value = first(row, ACCESSION_KEYS).upper()
    return value if value.startswith("PXD") else ""


def candidate_sha_index(path: Path | None) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    if not path or not path.is_file():
        return out
    for row in read_tsv(path):
        acc = normalize_accession(row)
        sha = first(row, ("candidate_sha256", "sha256")).lower()
        if acc and sha:
            out.setdefault(acc, set()).add(sha)
    return out


def _primary_independence_class(trust: str, locator: str) -> str:
    if not locator:
        return ""
    if trust in TRUSTED_DEPOSITED_CLASSES:
        return "deposited_repository"
    if trust in TRUSTED_SUPPLEMENT_CLASSES:
        return "publication_supplement"
    if trust in TRUSTED_EXTERNAL_CLASSES:
        return "independent_external"
    return ""


def _media_type(row: dict[str, str], path: Path | None) -> str:
    explicit = first(row, MEDIA_TYPE_KEYS)
    if explicit:
        return explicit
    if path:
        guessed, _ = mimetypes.guess_type(str(path))
        return guessed or ""
    return ""


def _resolve_parent_class(
    record: dict[str, Any],
    by_sha: dict[tuple[str, str], dict[str, Any]],
    candidate_shas: dict[str, set[str]],
    stack: set[tuple[str, str]],
) -> tuple[str, bool, str]:
    """Return (independence_class, independent, provenance_status)."""
    acc = record["accession"]
    artifact_sha = record["artifact_sha256"]
    trust = record["trust_class"]
    locator = record["source_locator"]
    parent_sha = record["parent_artifact_sha256"]
    derivation = record["derivation_operation"]
    candidate_equal = artifact_sha in candidate_shas.get(acc, set())

    primary = _primary_independence_class(trust, locator)
    if primary:
        return primary, True, "independent_source_provenance_present"

    if parent_sha:
        if parent_sha in candidate_shas.get(acc, set()):
            return "candidate_derived", False, "candidate_derived_ancestry"
        key = (acc, parent_sha)
        if key in stack:
            return "provenance_unknown", False, "lineage_cycle_detected"
        parent = by_sha.get(key)
        if parent is None:
            return "provenance_unknown", False, "parent_artifact_not_registered"
        parent_class, parent_independent, _ = _resolve_parent_class(
            parent,
            by_sha,
            candidate_shas,
            stack | {key},
        )
        if parent_class == "candidate_derived":
            return "candidate_derived", False, "candidate_derived_ancestry"
        if parent_independent and derivation in SAFE_DERIVATIONS:
            return "derived_from_trusted_source", True, "independent_parent_lineage_preserved"
        if parent_independent:
            return "provenance_unknown", False, "unapproved_derivation_from_independent_parent"
        return "provenance_unknown", False, "parent_provenance_not_independent"

    if candidate_equal:
        return "candidate_derived", False, "circular_or_unproven_self_evidence"
    if locator:
        return "provenance_unknown", False, "locator_present_but_trust_unproven"
    return "provenance_unknown", False, "source_provenance_unknown"


def build_registry(
    source_manifests: list[Path],
    candidate_manifest: Path | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    candidate_shas = candidate_sha_index(candidate_manifest)
    records: dict[tuple[str, str, str], dict[str, Any]] = {}
    missing_files = 0

    for manifest in source_manifests:
        for row in read_tsv(manifest):
            acc = normalize_accession(row)
            local_path = first(row, PATH_KEYS)
            declared_sha = first(row, SHA_KEYS).lower()
            path = Path(local_path) if local_path else None
            if path is not None and not path.is_absolute():
                path = (manifest.parent / path).resolve()
                local_path = str(path)
            actual_sha = ""
            byte_size: int | str = ""
            declared_size_raw = first(row, SIZE_KEYS)
            declared_size = int(declared_size_raw) if declared_size_raw else None
            if path and path.is_file():
                actual_sha = sha256_file(path)
                byte_size = path.stat().st_size
                if declared_sha and declared_sha != actual_sha:
                    raise ValueError(
                        f"declared SHA mismatch for {path}: {declared_sha} != {actual_sha}"
                    )
                if declared_size is not None and declared_size != byte_size:
                    raise ValueError(
                        f"declared size mismatch for {path}: {declared_size} != {byte_size}"
                    )
            elif local_path:
                missing_files += 1
            artifact_sha = actual_sha or declared_sha
            if not acc or not artifact_sha:
                continue

            kind = first(row, SOURCE_KIND_KEYS)
            provider = first(row, SOURCE_PROVIDER_KEYS)
            locator = first(row, SOURCE_LOCATOR_KEYS)
            trust = first(row, TRUST_KEYS) or "untrusted_or_unknown"
            field_name = first(row, BLOCKER_FIELD_KEYS)
            parent_sha = first(row, PARENT_SHA_KEYS).lower()
            derivation = first(row, DERIVATION_KEYS).lower()
            retrieved_at = first(row, RETRIEVED_AT_KEYS)
            retrieval_method = first(row, RETRIEVAL_METHOD_KEYS)
            original_filename = first(row, ORIGINAL_FILENAME_KEYS) or (path.name if path else "")
            media_type = _media_type(row, path)

            key = (acc, artifact_sha, field_name)
            item = {
                "accession": acc,
                "artifact_sha256": artifact_sha,
                "declared_sha256": declared_sha,
                "sha_verified": str(bool(actual_sha)).lower(),
                "blocker_field": field_name,
                "source_kind": kind,
                "source_provider": provider,
                "source_locator": locator,
                "retrieved_at": retrieved_at,
                "retrieval_method": retrieval_method,
                "original_filename": original_filename,
                "media_type": media_type,
                "local_path": local_path,
                "byte_size": byte_size,
                "parent_artifact_sha256": parent_sha,
                "derivation_operation": derivation,
                "trust_class": trust,
                "independence_class": "",
                "is_independent": "false",
                "provenance_status": "unclassified",
                "candidate_hash_equal": str(artifact_sha in candidate_shas.get(acc, set())).lower(),
                "manifest_path": str(manifest),
            }
            existing = records.get(key)
            # Prefer a duplicate row carrying an immutable locator or explicit parent lineage.
            score = int(bool(locator)) * 4 + int(bool(parent_sha)) * 2 + int(trust in TRUSTED_CLASSES)
            old_score = -1
            if existing:
                old_score = (
                    int(bool(existing["source_locator"])) * 4
                    + int(bool(existing["parent_artifact_sha256"])) * 2
                    + int(existing["trust_class"] in TRUSTED_CLASSES)
                )
            if existing is None or score > old_score:
                records[key] = item

    # Parent lookup is content-addressed and ignores blocker field because parentage is artifact-level.
    by_sha: dict[tuple[str, str], dict[str, Any]] = {}
    for item in records.values():
        key = (item["accession"], item["artifact_sha256"])
        current = by_sha.get(key)
        if current is None or item["source_locator"]:
            by_sha[key] = item

    for item in records.values():
        cls, independent, status = _resolve_parent_class(
            item,
            by_sha,
            candidate_shas,
            {(item["accession"], item["artifact_sha256"])},
        )
        item["independence_class"] = cls
        item["is_independent"] = str(independent).lower()
        item["provenance_status"] = status

    rows = sorted(records.values(), key=lambda r: (r["accession"], r["artifact_sha256"], r["blocker_field"]))
    class_counts: dict[str, int] = {}
    for row in rows:
        class_counts[row["independence_class"]] = class_counts.get(row["independence_class"], 0) + 1
    summary = {
        "version": VERSION,
        "state_primitives_version": STATE_VERSION,
        "source_manifests": [str(x) for x in source_manifests],
        "candidate_manifest": str(candidate_manifest or ""),
        "records": len(rows),
        "accessions": len({r["accession"] for r in rows}),
        "independent_records": sum(r["is_independent"] == "true" for r in rows),
        "candidate_hash_equal_records": sum(r["candidate_hash_equal"] == "true" for r in rows),
        "candidate_derived_records": sum(r["independence_class"] == "candidate_derived" for r in rows),
        "independence_class_counts": dict(sorted(class_counts.items())),
        "missing_local_files": missing_files,
    }
    return rows, summary


def write_tsv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, delimiter="\t", lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def self_test() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        candidate = root / "candidate.sdrf.tsv"
        candidate.write_text("source name\tcomment[data file]\na\ta.raw\n", encoding="utf-8")
        candidate_sha = sha256_file(candidate)
        candidates = root / "candidates.tsv"
        candidates.write_text(
            "accession\tcandidate_sha256\nPXD900001\t" + candidate_sha + "\n",
            encoding="utf-8",
        )

        # Candidate bytes without external provenance must fail closed.
        m1 = root / "m1.tsv"
        m1.write_text(
            "accession\tlocal_path\ttrust_class\tsource_locator\n"
            f"PXD900001\t{candidate}\tuntrusted_or_unknown\t\n",
            encoding="utf-8",
        )
        rows, _ = build_registry([m1], candidates)
        assert rows[0]["independence_class"] == "candidate_derived"
        assert rows[0]["is_independent"] == "false"

        # An independently fetched deposited artifact may happen to have identical bytes.
        m1.write_text(
            "accession\tlocal_path\ttrust_class\tsource_locator\tsource_provider\n"
            f"PXD900001\t{candidate}\ttrusted_deposited\thttps://example.org/source.tsv\tPRIDE\n",
            encoding="utf-8",
        )
        rows, _ = build_registry([m1], candidates)
        assert rows[0]["independence_class"] == "deposited_repository"
        assert rows[0]["is_independent"] == "true"

        # A parser derivative preserves independence through parent SHA lineage.
        parent = root / "parent.tsv"
        parent.write_text("raw\tvalue\na.raw\tx\n", encoding="utf-8")
        child = root / "child.tsv"
        child.write_text("raw\tvalue\tnormalized\na.raw\tx\t1\n", encoding="utf-8")
        psha = sha256_file(parent)
        m2 = root / "m2.tsv"
        m2.write_text(
            "accession\tlocal_path\ttrust_class\tsource_locator\tparent_artifact_sha256\tderivation_operation\n"
            f"PXD900002\t{parent}\ttrusted_independent\thttps://example.org/parent.tsv\t\t\n"
            f"PXD900002\t{child}\ttrusted_independent\t\t{psha}\tparse_table\n",
            encoding="utf-8",
        )
        rows, summary = build_registry([m2], None)
        child_row = next(r for r in rows if r["local_path"] == str(child))
        assert child_row["independence_class"] == "derived_from_trusted_source"
        assert child_row["is_independent"] == "true"
        assert summary["independent_records"] == 2
    print("sdrf_evidence_registry self-test: PASS")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-manifest", action="append", type=Path, default=[])
    p.add_argument("--candidate-manifest", type=Path)
    p.add_argument("--output", type=Path)
    p.add_argument("--self-test", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()
    if args.self_test:
        self_test()
        return 0
    if not args.source_manifest or args.output is None:
        raise SystemExit("--source-manifest and --output are required unless --self-test")
    rows, summary = build_registry(args.source_manifest, args.candidate_manifest)
    args.output.mkdir(parents=True, exist_ok=True)
    fields = [
        "accession", "artifact_sha256", "declared_sha256", "sha_verified", "blocker_field",
        "source_kind", "source_provider", "source_locator", "retrieved_at", "retrieval_method",
        "original_filename", "media_type", "local_path", "byte_size", "parent_artifact_sha256",
        "derivation_operation", "trust_class", "independence_class", "is_independent",
        "provenance_status", "candidate_hash_equal", "manifest_path",
    ]
    write_tsv(args.output / "evidence_registry.tsv", rows, fields)
    (args.output / "evidence_registry_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
