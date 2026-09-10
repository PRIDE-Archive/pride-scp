#!/usr/bin/env python3
"""Stage explicitly-audited SDRF candidates for the BigBio readiness gate.

This helper is operational only.  It searches only user-supplied roots for exact
{ACCESSION}.sdrf.tsv files, requires a companion PRIDE_SCP audit indicating local validity/source
closure, deduplicates byte-identical candidates by SHA-256, and refuses to choose among divergent
candidates.  It never creates or edits SDRF rows.
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
from collections import defaultdict
from pathlib import Path

import sdrf_bigbio_readiness as readiness


def parse_accessions(path: Path) -> list[str]:
    return readiness.collect_accessions([], path)


def trustworthy(info: readiness.CandidateInfo) -> tuple[bool, str]:
    if not info.internal_audit_path:
        return False, "no_companion_audit"
    if info.locally_valid is False:
        return False, "audit_locally_valid_false"
    if (info.internal_validation_errors or 0) > 0:
        return False, "audit_validation_errors"
    completeness = info.completeness_status.strip()
    accepted_completeness = completeness in readiness.ACCEPTED_INTERNAL_COMPLETENESS if completeness else False
    if info.locally_valid is True or accepted_completeness:
        return True, "trusted_audit"
    return False, "audit_not_source_closed_or_locally_valid"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--accessions-file", type=Path, required=True)
    p.add_argument("--search-root", type=Path, action="append", required=True,
                   help="repeatable explicit root to search recursively")
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()

    accessions = parse_accessions(args.accessions_file)
    roots = [x.resolve() for x in args.search_root]
    args.output.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, str]] = []
    staged = 0
    for accession in accessions:
        found: list[Path] = []
        for root in roots:
            if not root.is_dir():
                continue
            found.extend(p for p in root.rglob(f"{accession}.sdrf.tsv") if p.is_file())
        found = sorted(set(found))

        eligible: list[tuple[Path, readiness.CandidateInfo]] = []
        rejected: list[str] = []
        for candidate in found:
            info = readiness.load_candidate_info(candidate, roots, accession)
            ok, reason = trustworthy(info)
            if ok:
                eligible.append((candidate, info))
            else:
                rejected.append(f"{candidate}:{reason}")

        by_hash: dict[str, list[tuple[Path, readiness.CandidateInfo]]] = defaultdict(list)
        for candidate, info in eligible:
            by_hash[info.sha256].append((candidate, info))

        status = "no_trusted_candidate"
        chosen_path = ""
        sha = ""
        audit = ""
        if len(by_hash) == 1:
            sha, group = next(iter(by_hash.items()))
            candidate, info = sorted(group, key=lambda x: (len(str(x[0])), str(x[0])))[0]
            dest_dir = args.output / accession
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"{accession}.sdrf.tsv"
            shutil.copy2(candidate, dest)
            if info.internal_audit_path:
                audit_src = Path(info.internal_audit_path)
                audit_dest = dest_dir / f"{accession}.sdrf_audit.json"
                shutil.copy2(audit_src, audit_dest)
                audit = str(audit_dest)
            status = "staged_unique_trusted_candidate"
            chosen_path = str(candidate)
            staged += 1
        elif len(by_hash) > 1:
            status = "blocked_multiple_nonidentical_trusted_candidates"

        rows.append({
            "accession": accession,
            "status": status,
            "candidate_files_seen": str(len(found)),
            "trusted_candidate_files": str(len(eligible)),
            "trusted_unique_hashes": str(len(by_hash)),
            "chosen_source": chosen_path,
            "sha256": sha,
            "staged_audit": audit,
            "rejected_candidates": " | ".join(rejected),
        })
        print(f"{accession}\t{status}\tseen={len(found)}\ttrusted={len(eligible)}\thashes={len(by_hash)}")

    tsv = args.output / "candidate_stage.tsv"
    with tsv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["accession"], delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)
    summary = {
        "accessions": len(accessions),
        "staged": staged,
        "blocked_or_missing": len(accessions) - staged,
        "search_roots": [str(x) for x in roots],
        "output": str(args.output),
    }
    (args.output / "candidate_stage_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
