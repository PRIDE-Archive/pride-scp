#!/usr/bin/env python3
"""Build a content-addressed provenance registry for PRIDE-SCP SDRF evidence artifacts.

The registry is deliberately conservative.  A local file path or generalized-graph copy does
not establish source independence.  Positive independence requires an explicit trusted source
class/locator from the supplied manifests, while exact equality to a candidate is recorded as a
circularity warning.
"""
from __future__ import annotations

import argparse
import csv
import json
import tempfile
from pathlib import Path
from typing import Any

from sdrf_annotation_state import EvidenceRecord, VERSION as STATE_VERSION, sha256_file

VERSION = "pride-scp-sdrf-evidence-registry-v2.1.0"

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

TRUSTED_CLASSES = {
    "trusted_independent",
    "trusted_deposited",
    "trusted_local_deposited_sdrf",
    "trusted_publication_supplement",
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


def infer_independence(row: dict[str, str], trust: str, locator: str) -> bool:
    explicit = str(row.get("is_independent") or "").strip().lower()
    if explicit in {"true", "1", "yes"}:
        return True
    if explicit in {"false", "0", "no"}:
        return False
    if trust in TRUSTED_CLASSES and locator:
        return True
    return False


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
            if path and path.is_file():
                actual_sha = sha256_file(path)
                byte_size = path.stat().st_size
                if declared_sha and declared_sha != actual_sha:
                    raise ValueError(
                        f"declared SHA mismatch for {path}: {declared_sha} != {actual_sha}"
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
            derivation = first(row, DERIVATION_KEYS)
            independent = infer_independence(row, trust, locator)

            candidate_equal = artifact_sha in candidate_shas.get(acc, set())
            if candidate_equal and not independent:
                provenance_status = "circular_or_unproven_self_evidence"
            elif independent:
                provenance_status = "independent_source_provenance_present"
            elif locator:
                provenance_status = "locator_present_but_trust_unproven"
            else:
                provenance_status = "source_provenance_unknown"

            key = (acc, artifact_sha, field_name)
            existing = records.get(key)
            item = {
                "accession": acc,
                "artifact_sha256": artifact_sha,
                "declared_sha256": declared_sha,
                "sha_verified": str(bool(actual_sha)).lower(),
                "blocker_field": field_name,
                "source_kind": kind,
                "source_provider": provider,
                "source_locator": locator,
                "local_path": local_path,
                "byte_size": byte_size,
                "parent_artifact_sha256": parent_sha,
                "derivation_operation": derivation,
                "trust_class": trust,
                "is_independent": str(independent).lower(),
                "provenance_status": provenance_status,
                "candidate_hash_equal": str(candidate_equal).lower(),
                "manifest_path": str(manifest),
            }
            # Prefer the row carrying stronger provenance if duplicate content appears.
            if existing is None or (independent and existing["is_independent"] != "true"):
                records[key] = item

    rows = sorted(records.values(), key=lambda r: (r["accession"], r["artifact_sha256"], r["blocker_field"]))
    summary = {
        "version": VERSION,
        "state_primitives_version": STATE_VERSION,
        "source_manifests": [str(x) for x in source_manifests],
        "candidate_manifest": str(candidate_manifest or ""),
        "records": len(rows),
        "accessions": len({r["accession"] for r in rows}),
        "independent_records": sum(r["is_independent"] == "true" for r in rows),
        "candidate_hash_equal_records": sum(r["candidate_hash_equal"] == "true" for r in rows),
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
        artifact = root / "PXD900001.sdrf.tsv"
        artifact.write_text("source name\tcomment[data file]\na\ta.raw\n", encoding="utf-8")
        sha = sha256_file(artifact)
        candidates = root / "candidates.tsv"
        candidates.write_text(
            "accession\tcandidate_sha256\nPXD900001\t" + sha + "\n",
            encoding="utf-8",
        )
        manifest = root / "sources.tsv"
        manifest.write_text(
            "accession\tlocal_path\ttrust_class\tsource_locator\n"
            f"PXD900001\t{artifact}\tuntrusted_or_unknown\t\n",
            encoding="utf-8",
        )
        rows, summary = build_registry([manifest], candidates)
        assert summary["records"] == 1
        assert rows[0]["candidate_hash_equal"] == "true"
        assert rows[0]["is_independent"] == "false"
        assert rows[0]["provenance_status"] == "circular_or_unproven_self_evidence"

        manifest.write_text(
            "accession\tlocal_path\ttrust_class\tsource_locator\n"
            f"PXD900001\t{artifact}\ttrusted_deposited\thttps://example.org/source.tsv\n",
            encoding="utf-8",
        )
        rows, _ = build_registry([manifest], candidates)
        assert rows[0]["is_independent"] == "true"
        assert rows[0]["provenance_status"] == "independent_source_provenance_present"
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
        "accession", "artifact_sha256", "declared_sha256", "sha_verified", "blocker_field", "source_kind", "source_provider",
        "source_locator", "local_path", "byte_size", "parent_artifact_sha256",
        "derivation_operation", "trust_class", "is_independent", "provenance_status",
        "candidate_hash_equal", "manifest_path",
    ]
    write_tsv(args.output / "evidence_registry.tsv", rows, fields)
    (args.output / "evidence_registry_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
