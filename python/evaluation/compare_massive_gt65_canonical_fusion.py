#!/usr/bin/env python3
"""Compare M2-C1 GT65 decisions with final bounded M2-C2 fusion decisions."""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
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


def counts(rows: list[dict[str, str]]) -> dict[str, int]:
    c = Counter(text(r.get("curation_decision")) or "missing" for r in rows)
    return {key: c.get(key, 0) for key in ("include", "review", "exclude", "missing")}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline-audit", type=Path, required=True)
    ap.add_argument("--fusion-audit", type=Path, required=True)
    ap.add_argument("--output-dir", type=Path, required=True)
    ap.add_argument("--expect-gt-rows", type=int, default=65)
    args = ap.parse_args()

    baseline = read_tsv(args.baseline_audit)
    fusion = read_tsv(args.fusion_audit)
    if len(baseline) != args.expect_gt_rows or len(fusion) != args.expect_gt_rows:
        raise SystemExit(
            f"M2-C2 comparison guard failed: baseline={len(baseline)} fusion={len(fusion)} expected={args.expect_gt_rows}"
        )
    base_by_gt = {text(r.get("gt_accession")): r for r in baseline}
    new_by_gt = {text(r.get("gt_accession")): r for r in fusion}
    if set(base_by_gt) != set(new_by_gt):
        raise SystemExit("M2-C2 comparison GT accession sets differ")

    rows: list[dict[str, Any]] = []
    transitions: Counter[str] = Counter()
    affected_transitions: Counter[str] = Counter()
    for gt in sorted(base_by_gt):
        old = base_by_gt[gt]; new = new_by_gt[gt]
        old_dec = text(old.get("curation_decision")) or "missing"
        new_dec = text(new.get("curation_decision")) or "missing"
        transition = f"{old_dec}->{new_dec}"
        transitions[transition] += 1
        affected = text(new.get("canonical_fusion_applied")) == "yes"
        if affected:
            affected_transitions[transition] += 1
        rows.append({
            "gt_accession": gt,
            "representative_accession": text(new.get("representative_accession")),
            "recovery_route": text(new.get("recovery_route")),
            "canonical_fusion_applied": text(new.get("canonical_fusion_applied")),
            "fused_native_accessions": text(new.get("fused_native_accessions")),
            "baseline_decision": old_dec,
            "fusion_decision": new_dec,
            "decision_transition": transition,
            "baseline_reason": text(old.get("decision_reason")),
            "fusion_reason": text(new.get("decision_reason")),
            "baseline_evidence_items": text(old.get("evidence_item_count")),
            "fusion_evidence_items": text(new.get("evidence_item_count")),
            "baseline_deterministic_items": text(old.get("deterministic_evidence_item_count")),
            "fusion_deterministic_items": text(new.get("deterministic_evidence_item_count")),
            "fusion_validation_warnings": text(new.get("validation_warning_count")),
        })

    affected_rows = [r for r in fusion if text(r.get("canonical_fusion_applied")) == "yes"]
    unaffected_rows = [r for r in fusion if text(r.get("canonical_fusion_applied")) != "yes"]
    summary = {
        "phase": "M2-C2_canonical_evidence_fusion_comparison",
        "gt_rows": len(fusion),
        "baseline_decision_counts": counts(baseline),
        "fusion_decision_counts": counts(fusion),
        "decision_transition_counts": dict(sorted(transitions.items())),
        "affected_gt_rows": len(affected_rows),
        "affected_decision_counts": counts(affected_rows),
        "affected_transition_counts": dict(sorted(affected_transitions.items())),
        "unaffected_gt_rows": len(unaffected_rows),
        "unaffected_decision_counts": counts(unaffected_rows),
        "hard_false_negatives": sum(text(r.get("curation_decision")) == "exclude" for r in fusion),
        "note": "M2-C2 reruns only PXD representatives changed by canonical evidence fusion; unchanged native rows reuse M2-C1 results.",
    }
    out = args.output_dir.resolve(); out.mkdir(parents=True, exist_ok=True)
    (out / "massive_gt65_canonical_fusion_comparison_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    fields = list(rows[0].keys()) if rows else ["gt_accession"]
    write_tsv(out / "massive_gt65_canonical_fusion_decision_changes.tsv", rows, fields)
    write_tsv(
        out / "massive_gt65_canonical_fusion_changed_decisions.tsv",
        [r for r in rows if r["baseline_decision"] != r["fusion_decision"]],
        fields,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
