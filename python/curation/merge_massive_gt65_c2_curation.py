#!/usr/bin/env python3
"""Merge unchanged M2-C1 native results with M2-C2 rerun PXD results."""
from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Any


def text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline-summary", type=Path, required=True)
    ap.add_argument("--affected-summary", type=Path, required=True)
    ap.add_argument("--affected-accessions", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--expect-total", type=int, default=63)
    ap.add_argument("--expect-affected", type=int, default=25)
    args = ap.parse_args()

    baseline = read_tsv(args.baseline_summary)
    affected_rows = read_tsv(args.affected_summary)
    affected = {line.strip().upper() for line in args.affected_accessions.read_text(encoding="utf-8").splitlines() if line.strip()}
    if len(affected) != args.expect_affected:
        raise SystemExit(f"M2-C2 merge guard failed: expected {args.expect_affected} affected accessions, observed {len(affected)}")
    replacement = {text(row.get("accession")).upper(): row for row in affected_rows}
    missing = sorted(affected - set(replacement))
    if missing:
        raise SystemExit("M2-C2 merge missing rerun summaries: " + ", ".join(missing))

    merged: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in baseline:
        accession = text(row.get("accession")).upper()
        merged.append(replacement[accession] if accession in affected else row)
        seen.add(accession)
    if len(merged) != args.expect_total:
        raise SystemExit(f"M2-C2 merge guard failed: expected {args.expect_total} merged rows, observed {len(merged)}")
    if not affected.issubset(seen):
        raise SystemExit("M2-C2 affected candidate absent from baseline: " + ", ".join(sorted(affected - seen)))

    fields = list(baseline[0].keys()) if baseline else list(affected_rows[0].keys())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        writer.writeheader(); writer.writerows(merged)
    print(f"merged {len(merged)} curation summaries: reused={len(merged)-len(affected)} rerun={len(affected)}")


if __name__ == "__main__":
    main()
