#!/usr/bin/env python3
"""Build an evaluation-only MassIVE GT65 curation benchmark candidate set.

GT is used only to select the already-discovered identities to benchmark.  The
candidate JSONL written for v19 intentionally contains no GT labels or expected
curation decisions.  Runtime identity and evidence come only from accepted
source-derived discovery/bridge outputs.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any


def text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    rows.append(value)
    return rows


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def score(row: dict[str, Any]) -> int:
    try:
        return int(float(row.get("score") or row.get("discovery_score") or 0))
    except Exception:
        return 0


def split_accessions(value: str) -> list[str]:
    return [part.strip().upper() for part in (value or "").split(";") if part.strip()]


def sanitized_candidate(row: dict[str, Any]) -> dict[str, Any]:
    """Remove evaluation/GT-shaped fields before passing a candidate to v19."""
    banned_prefixes = ("gt_", "evaluation_", "matched_frozen_gt")
    out = {
        key: value for key, value in row.items()
        if not any(str(key).lower().startswith(prefix) for prefix in banned_prefixes)
    }
    return out


def choose_representative(
    matched: list[str],
    accepted: dict[str, dict[str, Any]],
    native_bridge: dict[str, dict[str, Any]],
    route: str,
) -> tuple[str, dict[str, Any], str]:
    available = [acc for acc in matched if acc in accepted]
    if not available:
        raise ValueError(f"no matched accepted candidate among: {matched}")

    native_ready = [acc for acc in available if acc.startswith("MSV") and acc in native_bridge]
    if route in {"native_massive", "native_and_pxd"} and native_ready:
        chosen = max(native_ready, key=lambda acc: (score(native_bridge[acc]), acc))
        return chosen, native_bridge[chosen], "native_msv_with_bridge"

    pxd = [acc for acc in available if acc.startswith("PXD")]
    if route == "pxd_alias" and pxd:
        chosen = max(pxd, key=lambda acc: (score(accepted[acc]), acc))
        return chosen, accepted[chosen], "pxd_candidate"

    # Generic source-derived fallback: prefer a bridge-equipped native record,
    # otherwise the highest-scoring accepted candidate.  This is not a GT rule.
    if native_ready:
        chosen = max(native_ready, key=lambda acc: (score(native_bridge[acc]), acc))
        return chosen, native_bridge[chosen], "native_msv_with_bridge"
    chosen = max(available, key=lambda acc: (score(accepted[acc]), acc.startswith("PXD"), acc))
    return chosen, accepted[chosen], "pxd_candidate" if chosen.startswith("PXD") else "native_msv_without_bridge"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--accepted-candidates-jsonl", type=Path, required=True)
    ap.add_argument("--native-bridge-candidates-jsonl", type=Path, required=True)
    ap.add_argument("--recall-audit-tsv", type=Path, required=True)
    ap.add_argument("--output-dir", type=Path, required=True)
    ap.add_argument("--expect-gt-rows", type=int, default=65)
    args = ap.parse_args()

    accepted_rows = load_jsonl(args.accepted_candidates_jsonl)
    accepted = {text(row.get("accession")).upper(): row for row in accepted_rows if text(row.get("accession"))}
    bridge_rows = load_jsonl(args.native_bridge_candidates_jsonl)
    native_bridge = {text(row.get("accession")).upper(): row for row in bridge_rows if text(row.get("accession"))}
    recall = read_tsv(args.recall_audit_tsv)
    recovered = [row for row in recall if text(row.get("recovered")).lower() == "yes"]
    if len(recall) != args.expect_gt_rows or len(recovered) != args.expect_gt_rows:
        raise SystemExit(
            f"GT65 benchmark guard failed: expected {args.expect_gt_rows} recovered rows, "
            f"observed recall_rows={len(recall)} recovered={len(recovered)}"
        )

    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    unique_candidates: dict[str, dict[str, Any]] = {}
    route_counts: Counter[str] = Counter()
    representation_counts: Counter[str] = Counter()

    for row in recovered:
        matched = split_accessions(text(row.get("matched_candidates")))
        route = text(row.get("recovery_route"))
        try:
            rep, candidate, rep_class = choose_representative(matched, accepted, native_bridge, route)
        except ValueError as exc:
            raise SystemExit(f"GT65 benchmark representative selection failed for {row.get('gt_accession')}: {exc}") from exc
        clean = sanitized_candidate(candidate)
        clean["accession"] = rep
        # Explicitly remove any accidental benchmark metadata before curation.
        for key in list(clean):
            if key.lower().startswith(("gt_", "evaluation_", "matched_frozen_gt")):
                clean.pop(key, None)
        unique_candidates.setdefault(rep, clean)
        route_counts[route] += 1
        representation_counts[rep_class] += 1
        manifest.append({
            "gt_accession": text(row.get("gt_accession")),
            "recovery_route": route,
            "matched_candidates": "; ".join(matched),
            "representative_accession": rep,
            "representation_class": rep_class,
            "representative_score": score(clean),
            "representative_tier": text(clean.get("tier") or clean.get("discovery_tier")),
            "native_evidence_attached": "yes" if text(clean.get("native_evidence_path")) else "no",
            "trusted_pxd_aliases": "; ".join(clean.get("trusted_pxd_aliases") or []) if isinstance(clean.get("trusted_pxd_aliases"), list) else text(clean.get("trusted_pxd_aliases")),
            "dataset_title": text(row.get("dataset_title")),
            "evaluation_only": "yes",
        })

    benchmark_path = out / "massive_gt65_benchmark_candidates.jsonl"
    with benchmark_path.open("w", encoding="utf-8") as handle:
        for accession in sorted(unique_candidates):
            handle.write(json.dumps(unique_candidates[accession], ensure_ascii=False) + "\n")

    fields = [
        "gt_accession", "recovery_route", "matched_candidates", "representative_accession",
        "representation_class", "representative_score", "representative_tier",
        "native_evidence_attached", "trusted_pxd_aliases", "dataset_title", "evaluation_only",
    ]
    write_tsv(out / "massive_gt65_benchmark_manifest.tsv", manifest, fields)

    summary = {
        "evaluation_only": True,
        "gt_rows": len(manifest),
        "expected_gt_rows": args.expect_gt_rows,
        "unique_curation_candidates": len(unique_candidates),
        "recovery_route_counts": dict(sorted(route_counts.items())),
        "representation_class_counts": dict(sorted(representation_counts.items())),
        "rows_with_native_evidence_attached": sum(r["native_evidence_attached"] == "yes" for r in manifest),
        "gt_labels_written_to_curation_candidates": False,
        "benchmark_candidates_jsonl": str(benchmark_path),
        "note": "GT selects benchmark rows only. Candidate identity/evidence are copied from accepted source-derived outputs; GT labels are not written into the v19 input JSONL.",
    }
    (out / "massive_gt65_benchmark_build_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
