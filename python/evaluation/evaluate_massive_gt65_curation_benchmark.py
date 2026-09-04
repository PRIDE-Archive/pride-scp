#!/usr/bin/env python3
"""Evaluate frozen v19 curation decisions across all 65 MassIVE GT rows."""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)


def summarize_group(rows: list[dict[str, Any]]) -> dict[str, Any]:
    decisions = Counter(text(r.get("curation_decision")) or "missing" for r in rows)
    return {
        "rows": len(rows),
        "decision_counts": dict(sorted(decisions.items())),
        "hard_false_negatives": decisions.get("exclude", 0),
        "reviews": decisions.get("review", 0),
        "includes": decisions.get("include", 0),
        "errors": sum(text(r.get("run_status")) == "error" for r in rows),
        "with_native_evidence_attached": sum(text(r.get("native_evidence_attached")) == "yes" for r in rows),
        "with_publication_annotations": sum(int(float(r.get("publication_annotation_count") or 0)) > 0 for r in rows),
        "with_nonempty_packet": sum(int(float(r.get("evidence_item_count") or 0)) > 0 for r in rows),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--curation-summary", type=Path, required=True)
    ap.add_argument("--output-dir", type=Path, required=True)
    ap.add_argument("--expect-gt-rows", type=int, default=65)
    args = ap.parse_args()

    manifest = read_tsv(args.manifest)
    cur = {text(r.get("accession")).upper(): r for r in read_tsv(args.curation_summary)}
    if len(manifest) != args.expect_gt_rows:
        raise SystemExit(f"GT65 curation evaluation guard failed: expected {args.expect_gt_rows} manifest rows, observed {len(manifest)}")

    audit: list[dict[str, Any]] = []
    missing: list[str] = []
    for row in manifest:
        rep = text(row.get("representative_accession")).upper()
        c = cur.get(rep)
        if c is None:
            missing.append(rep)
            c = {}
        audit.append({
            **row,
            "run_status": text(c.get("run_status")) or "missing",
            "curation_decision": text(c.get("curation_decision")) or "missing",
            "decision_reason": text(c.get("decision_reason")),
            "evidence_sufficiency": text(c.get("evidence_sufficiency")),
            "evidence_item_count": text(c.get("evidence_item_count")) or "0",
            "deterministic_evidence_item_count": text(c.get("deterministic_evidence_item_count")) or "0",
            "publication_annotation_count": text(c.get("publication_annotation_count")) or "0",
            "validation_warning_count": text(c.get("validation_warning_count")) or "0",
            "result_path": text(c.get("result_path")),
        })

    out = args.output_dir.resolve(); out.mkdir(parents=True, exist_ok=True)
    fields = list(audit[0].keys()) if audit else ["gt_accession"]
    write_tsv(out / "massive_gt65_curation_decision_audit.tsv", audit, fields)
    write_tsv(out / "massive_gt65_hard_false_negatives.tsv", [r for r in audit if r["curation_decision"] == "exclude"], fields)
    write_tsv(out / "massive_gt65_reviews.tsv", [r for r in audit if r["curation_decision"] == "review"], fields)

    by_route: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_rep: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in audit:
        by_route[text(row.get("recovery_route"))].append(row)
        by_rep[text(row.get("representation_class"))].append(row)

    overall = summarize_group(audit)
    summary = {
        "evaluation_only": True,
        "expected_gt_rows": args.expect_gt_rows,
        "gt_rows_evaluated": len(audit),
        "missing_curation_representatives": sorted(set(missing)),
        "overall": overall,
        "by_recovery_route": {key: summarize_group(rows) for key, rows in sorted(by_route.items())},
        "by_representation_class": {key: summarize_group(rows) for key, rows in sorted(by_rep.items())},
        "note": "Hard false negatives are GT-positive rows receiving an automated exclude. Review is not counted as a hard false negative. GT is evaluation-only and was not supplied to v19 inference.",
    }
    (out / "massive_gt65_curation_benchmark_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
