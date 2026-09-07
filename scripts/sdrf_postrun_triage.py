#!/usr/bin/env python3
"""Classify SDRF annotation outcomes into actionable recovery lanes.

This tool is intentionally GT-agnostic. It consumes a completed
`sdrf_annotation_results.tsv` and, optionally, a prior resolved-SDRF audit
(`sdrf_audit_results.tsv`) to distinguish preservation/linkage regressions
from true structural problems and de-novo reconstruction blockers.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List


def read_tsv(path: Path) -> List[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y"}


def classify(row: dict[str, str], audit: dict[str, str] | None) -> tuple[str, str]:
    valid = as_bool(row.get("locally_valid", ""))
    mode = row.get("generation_mode", "")
    completeness = row.get("completeness_status", "")
    existing = mode in {"enriched_existing_sdrf", "validated_existing_sdrf"}

    if valid:
        return "ready", "locally valid annotation/draft"

    if existing:
        if audit:
            baseline_valid = as_bool(audit.get("locally_valid", ""))
            linkage = audit.get("repository_linkage_status", "")
            if baseline_valid and linkage in {"partial", "none"}:
                return (
                    "resolved_linkage_validation_regression",
                    "resolved SDRF was structurally valid in the deterministic audit; local repository linkage was partial/none",
                )
        return "existing_structural_review", "resolved SDRF remains structurally invalid or has blocking template metadata"

    by_completeness = {
        "incomplete_required_metadata": (
            "denovo_required_metadata",
            "one-cell mapping is available but required SDRF metadata remains unresolved",
        ),
        "incomplete_template_isolation_method_gap": (
            "denovo_template_gap",
            "study method is evidenced but not faithfully representable in the pinned single-cell template vocabulary",
        ),
        "incomplete_sample_to_file_or_channel_mapping": (
            "denovo_mapping",
            "sample/cell/channel mapping remains unresolved",
        ),
        "incomplete_repository_archive_contents_mapping": (
            "denovo_archive",
            "repository archive/container contents must be resolved to canonical acquisitions",
        ),
    }
    if completeness in by_completeness:
        return by_completeness[completeness]
    return "other_incomplete", f"unclassified incomplete status: {completeness or 'unknown'}"


def write_list(path: Path, accessions: Iterable[str]) -> None:
    values = sorted(set(accessions))
    path.write_text("".join(f"{x}\n" for x in values))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--annotation-results", type=Path, required=True)
    parser.add_argument("--resolved-audit-results", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    annotations = read_tsv(args.annotation_results)
    audit_by_accession: Dict[str, dict[str, str]] = {}
    if args.resolved_audit_results:
        audit_by_accession = {
            row["accession"]: row
            for row in read_tsv(args.resolved_audit_results)
            if row.get("accession")
        }

    args.output.mkdir(parents=True, exist_ok=True)
    triage_rows: list[dict[str, str]] = []
    lane_members: dict[str, list[str]] = {}

    for row in annotations:
        accession = row.get("accession", "").strip()
        audit = audit_by_accession.get(accession)
        lane, reason = classify(row, audit)
        lane_members.setdefault(lane, []).append(accession)
        triage_rows.append(
            {
                "accession": accession,
                "lane": lane,
                "reason": reason,
                "generation_mode": row.get("generation_mode", ""),
                "relation_mode": row.get("relation_mode", ""),
                "locally_valid": row.get("locally_valid", ""),
                "completeness_status": row.get("completeness_status", ""),
                "validation_errors": row.get("validation_errors", ""),
                "proposal_repairs": row.get("proposal_repairs", ""),
                "resolved_audit_locally_valid": audit.get("locally_valid", "") if audit else "",
                "repository_linkage_status": audit.get("repository_linkage_status", "") if audit else "",
            }
        )

    out_tsv = args.output / "sdrf_recovery_triage.tsv"
    fields = [
        "accession",
        "lane",
        "reason",
        "generation_mode",
        "relation_mode",
        "locally_valid",
        "completeness_status",
        "validation_errors",
        "proposal_repairs",
        "resolved_audit_locally_valid",
        "repository_linkage_status",
    ]
    with out_tsv.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, delimiter="\t", fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(triage_rows)

    for lane, accessions in lane_members.items():
        write_list(args.output / f"{lane}.txt", accessions)

    counts = Counter(row["lane"] for row in triage_rows)
    summary = {
        "annotation_results": str(args.annotation_results),
        "resolved_audit_results": str(args.resolved_audit_results) if args.resolved_audit_results else "",
        "accessions": len(triage_rows),
        "locally_valid": sum(as_bool(row.get("locally_valid", "")) for row in annotations),
        "incomplete_or_review": sum(not as_bool(row.get("locally_valid", "")) for row in annotations),
        "lane_counts": dict(sorted(counts.items())),
        "triage_tsv": str(out_tsv),
    }
    (args.output / "sdrf_recovery_triage_summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    print(json.dumps(summary, indent=2))
    for lane in sorted(counts):
        print(f"{lane}: {counts[lane]} -> {args.output / (lane + '.txt')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
