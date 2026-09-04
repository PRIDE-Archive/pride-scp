#!/usr/bin/env python3
"""Evaluation-only stratification of the GT-blind MassIVE M2-C source audit."""
from __future__ import annotations
import argparse, csv, json
from collections import Counter
from pathlib import Path


def read_tsv(path: Path):
    with path.open(encoding="utf-8", newline="") as h:
        return list(csv.DictReader(h, delimiter="\t"))


def write_tsv(path: Path, rows, fields):
    with path.open("w", encoding="utf-8", newline="") as h:
        w=csv.DictWriter(h, fieldnames=fields, delimiter="\t", extrasaction="ignore"); w.writeheader(); w.writerows(rows)


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--source-audit", type=Path, required=True)
    ap.add_argument("--delta-audit", type=Path, required=True)
    ap.add_argument("--output-dir", type=Path, required=True)
    args=ap.parse_args(); args.output_dir.mkdir(parents=True, exist_ok=True)
    source={r.get("candidate_accession","").upper():r for r in read_tsv(args.source_audit)}
    delta={r.get("candidate_accession","").upper():r for r in read_tsv(args.delta_audit)}
    rows=[]
    for acc in sorted(source):
        row=dict(source[acc]); d=delta.get(acc,{})
        row["gt_status_evaluation_only"]=d.get("gt_status_evaluation_only","")
        row["matched_frozen_gt_accessions_evaluation_only"]=d.get("matched_frozen_gt_accessions_evaluation_only","")
        rows.append(row)
    fields=list(rows[0].keys()) if rows else ["candidate_accession"]
    write_tsv(args.output_dir/"massive_native_m2c_source_audit_with_gt.tsv", rows, fields)
    nongt=[r for r in rows if r.get("gt_status_evaluation_only")=="absent_from_frozen_gt"]
    write_tsv(args.output_dir/"massive_native_m2c_non_gt_source_review_queue.tsv", nongt, fields)
    def stats(sub):
        return {
            "rows":len(sub),
            "packet_status_counts":dict(Counter(r.get("packet_status","") for r in sub)),
            "semantic_priority_counts":dict(Counter(r.get("semantic_priority","") for r in sub)),
            "source_review_priority_counts":dict(Counter(r.get("source_review_priority","") for r in sub)),
            "with_trusted_pxd_alias":sum(bool(r.get("trusted_pxd_aliases")) for r in sub),
            "with_publication_identifier":sum(bool(r.get("publication_dois") or r.get("publication_urls")) for r in sub),
            "with_sample_or_method_evidence":sum(int(r.get("sample_metadata_items") or 0)+int(r.get("ms_method_items") or 0)>0 for r in sub),
            "with_metadata_file_hint":sum(int(r.get("metadata_file_items") or 0)>0 for r in sub),
        }
    gt=[r for r in rows if r.get("gt_status_evaluation_only")=="frozen_gt_positive"]
    summary={
        "evaluation_only":True,
        "runtime_bridge_gt_used":False,
        "all_new_native_candidates":stats(rows),
        "frozen_gt_positive":stats(gt),
        "absent_from_frozen_gt":stats(nongt),
        "note":"GT labels are joined only after source evidence normalization and do not affect evidence extraction or review priority.",
    }
    (args.output_dir/"massive_native_m2c_source_audit_evaluation_summary.json").write_text(json.dumps(summary,indent=2,sort_keys=True)+"\n")
    print(json.dumps(summary,indent=2,sort_keys=True))
if __name__=="__main__": main()
