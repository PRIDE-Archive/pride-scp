#!/usr/bin/env python3
"""Stage audited SDRF candidates for the BigBio readiness gate.

Operational helper only. It searches explicit roots for exact {ACCESSION}.sdrf.tsv candidates,
requires an existing PRIDE_SCP per-accession audit proving local validity/source closure, deduplicates
byte-identical candidates by SHA-256, and refuses divergent trusted candidates. It never creates or
edits SDRF rows and never uses GT labels/annotations as scientific truth.

The helper understands both historical audit layouts used by PRIDE_SCP, notably:
  <run>/sdrf/PXDxxxxxx.sdrf.tsv
  <run>/audit/PXDxxxxxx.sdrf.audit.json
and the older underscore form PXDxxxxxx.sdrf_audit.json.
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

import sdrf_bigbio_readiness as readiness


def parse_accessions(path: Path) -> list[str]:
    return readiness.collect_accessions([], path)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def audit_candidates(candidate: Path, roots: list[Path], accession: str) -> list[Path]:
    """Return only audit paths structurally associated with this candidate.

    Do not broadly choose an arbitrary audit elsewhere in data/. The accepted layouts below either
    colocate the audit with the candidate or place it in the candidate run's sibling audit directory.
    """
    out: list[Path] = []
    run_root = candidate.parent.parent if candidate.parent.name in {"sdrf", "resolved", "drafts"} else candidate.parent
    likely = [
        # PXD.sdrf.tsv -> PXD.sdrf.audit.json in the same directory.
        candidate.with_suffix(".audit.json"),
        candidate.parent / f"{accession}.sdrf.audit.json",
        candidate.parent / f"{accession}.sdrf_audit.json",
        # Canonical PRIDE_SCP run layout: <run>/sdrf + <run>/audit.
        run_root / "audit" / f"{accession}.sdrf.audit.json",
        run_root / "audit" / f"{accession}.sdrf_audit.json",
        run_root / "audit" / f"{accession}.audit.json",
    ]
    for root in roots:
        # Explicit-root top-level audit layouts are also accepted, but never recursive arbitrary
        # matching because that could pair the candidate with an audit from another experiment run.
        likely.extend([
            root / "audit" / f"{accession}.sdrf.audit.json",
            root / "audit" / f"{accession}.sdrf_audit.json",
            root / "audit" / f"{accession}.audit.json",
        ])
    for path in likely:
        if path.is_file() and path not in out:
            out.append(path)
    return out


def candidate_info(candidate: Path, roots: list[Path], accession: str) -> tuple[readiness.CandidateInfo, dict[str, Any] | None]:
    info = readiness.CandidateInfo(path=str(candidate), sha256=readiness.sha256_file(candidate))
    for path in audit_candidates(candidate, roots, accession):
        audit = read_json(path)
        if audit is None:
            continue
        audit_acc = str(audit.get("accession") or accession).strip().upper()
        if audit_acc and audit_acc != accession:
            continue
        info.internal_audit_path = str(path)
        lv = audit.get("locally_valid")
        if isinstance(lv, bool):
            info.locally_valid = lv
        info.completeness_status = str(audit.get("completeness_status") or "")
        info.relation_mode = str(audit.get("relation_mode") or "")
        info.generation_mode = str(audit.get("generation_mode") or "")
        err = audit.get("validation_error_count")
        if isinstance(err, int):
            info.internal_validation_errors = err
        return info, audit
    return info, None


def trustworthy(info: readiness.CandidateInfo) -> tuple[bool, str]:
    if not info.internal_audit_path:
        return False, "no_companion_audit"
    if info.locally_valid is False:
        return False, "audit_locally_valid_false"
    if (info.internal_validation_errors or 0) > 0:
        return False, "audit_validation_errors"
    completeness = info.completeness_status.strip()
    accepted_completeness = completeness in readiness.ACCEPTED_INTERNAL_COMPLETENESS if completeness else False
    # Historical deterministic resolved-SDRF audits may not contain completeness_status; their
    # locally_valid=true + zero validation errors is sufficient to stage the artifact for the new
    # readiness gate, which will rerun all current checks and BigBio validation itself.
    if info.locally_valid is True or accepted_completeness:
        return True, "trusted_audit"
    return False, "audit_not_source_closed_or_locally_valid"


def normalized_audit(accession: str, info: readiness.CandidateInfo, source_audit: dict[str, Any], source_path: Path) -> dict[str, Any]:
    return {
        "accession": accession,
        "locally_valid": info.locally_valid,
        "validation_error_count": info.internal_validation_errors,
        "completeness_status": info.completeness_status,
        "relation_mode": info.relation_mode,
        "generation_mode": info.generation_mode,
        "source_audit_path": str(source_path),
        "source_audit": source_audit,
        "staging_policy": "existing_pride_scp_audit_only_no_row_generation",
    }


def stage(args: argparse.Namespace) -> dict[str, Any]:
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

        eligible: list[tuple[Path, readiness.CandidateInfo, dict[str, Any]]] = []
        rejected: list[str] = []
        for candidate in found:
            info, audit = candidate_info(candidate, roots, accession)
            ok, reason = trustworthy(info)
            if ok and audit is not None:
                eligible.append((candidate, info, audit))
            else:
                rejected.append(f"{candidate}:{reason}")

        by_hash: dict[str, list[tuple[Path, readiness.CandidateInfo, dict[str, Any]]]] = defaultdict(list)
        for candidate, info, audit in eligible:
            by_hash[info.sha256].append((candidate, info, audit))

        status = "no_trusted_candidate"
        chosen_path = ""
        sha = ""
        staged_audit = ""
        if len(by_hash) == 1:
            sha, group = next(iter(by_hash.items()))
            candidate, info, audit = sorted(group, key=lambda x: (len(str(x[0])), str(x[0])))[0]
            dest_dir = args.output / accession
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"{accession}.sdrf.tsv"
            shutil.copy2(candidate, dest)
            audit_dest = dest_dir / f"{accession}.sdrf_audit.json"
            audit_dest.write_text(
                json.dumps(normalized_audit(accession, info, audit, Path(info.internal_audit_path)), indent=2) + "\n",
                encoding="utf-8",
            )
            staged_audit = str(audit_dest)
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
            "staged_audit": staged_audit,
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
        "audit_filename_support": ["PXD.sdrf.audit.json", "PXD.sdrf_audit.json"],
        "policy": {
            "requires_existing_pride_scp_audit": True,
            "creates_or_edits_sdrf_rows": False,
            "chooses_between_nonidentical_trusted_candidates": False,
        },
    }
    (args.output / "candidate_stage_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return summary


def self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="pride-scp-stage-sdrf-") as td:
        root = Path(td)
        run = root / "run"
        (run / "sdrf").mkdir(parents=True)
        (run / "audit").mkdir()
        accession = "PXD999999"
        sdrf = run / "sdrf" / f"{accession}.sdrf.tsv"
        sdrf.write_text("source name\tcomment[data file]\ncell1\tcell1.raw\n", encoding="utf-8")
        # This exact dot-style sibling name is the historical layout that v0.5.13.1 missed.
        audit = run / "audit" / f"{accession}.sdrf.audit.json"
        audit.write_text(json.dumps({
            "accession": accession,
            "locally_valid": True,
            "validation_error_count": 0,
            "completeness_status": "locally_valid_draft",
            "relation_mode": "one_cell_per_data_file",
            "generation_mode": "source_grounded_explicit_mapping",
        }), encoding="utf-8")
        accessions = root / "accessions.txt"
        accessions.write_text(accession + "\n", encoding="utf-8")
        out = root / "out"
        ns = argparse.Namespace(accessions_file=accessions, search_root=[root], output=out)
        summary = stage(ns)
        assert summary["staged"] == 1
        assert (out / accession / f"{accession}.sdrf.tsv").is_file()
        normalized = out / accession / f"{accession}.sdrf_audit.json"
        assert normalized.is_file()
        obj = json.loads(normalized.read_text())
        assert obj["locally_valid"] is True
        assert obj["source_audit_path"].endswith(f"{accession}.sdrf.audit.json")
    print("stage_sdrf_readiness_candidates self-test: PASS")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--accessions-file", type=Path, required=False)
    p.add_argument("--search-root", type=Path, action="append", default=[],
                   help="repeatable explicit root to search recursively")
    p.add_argument("--output", type=Path, required=False)
    p.add_argument("--self-test", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()
    if args.self_test:
        self_test()
        return 0
    if args.accessions_file is None or not args.search_root or args.output is None:
        raise SystemExit("--accessions-file, at least one --search-root, and --output are required")
    stage(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
