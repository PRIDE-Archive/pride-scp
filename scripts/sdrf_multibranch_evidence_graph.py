#!/usr/bin/env python3
"""Backward-compatible entry point for the generalized SDRF evidence graph.

The original v0.5.2 implementation encoded accession-specific branch logic.  That behavior has been
retired.  This module now adapts the historical CLI to :mod:`sdrf_generalized_evidence_graph` and
contains no accession-specific scientific rules.
"""
from __future__ import annotations

import argparse
import csv
import tempfile
from pathlib import Path

from sdrf_generalized_evidence_graph import run as generalized_run, self_test as generalized_self_test


def _accessions_from_v051(audit: Path, output: Path) -> Path:
    graph = audit / "multiplex_evidence_graph.tsv"
    if not graph.is_file():
        raise SystemExit(f"missing historical evidence graph: {graph}")
    vals = []
    with graph.open(errors="replace") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            acc = (row.get("accession") or "").strip().upper()
            if acc: vals.append(acc)
    path = output / "compat_accessions.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(sorted(set(vals))) + "\n")
    return path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--snapshot", type=Path)
    p.add_argument("--v051-audit", type=Path)
    p.add_argument("--publication-manifest", type=Path)
    p.add_argument("--supplementary-links", type=Path)
    p.add_argument("--output", type=Path)
    p.add_argument("--fetch-external-analysis", action="store_true")
    p.add_argument("--max-external-files", type=int, default=24)
    p.add_argument("--max-external-bytes", type=int, default=25*1024*1024)
    p.add_argument("--max-archive-bytes", type=int, default=200*1024*1024)
    p.add_argument("--self-test", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()
    if args.self_test:
        generalized_self_test(); return 0
    if not all((args.snapshot, args.v051_audit, args.publication_manifest, args.output)):
        raise SystemExit("--snapshot, --v051-audit, --publication-manifest and --output are required")
    accessions = _accessions_from_v051(args.v051_audit, args.output)
    compat = argparse.Namespace(
        accessions_file=accessions,
        snapshot=args.snapshot,
        publication_manifest=args.publication_manifest,
        supplementary_links=args.supplementary_links,
        output=args.output,
        reuse_root=[], reuse_external_root=[],
        max_artifacts_per_accession=12,
        max_artifact_bytes=100*1024*1024,
        fetch_external_analysis=args.fetch_external_analysis,
        max_external_files=args.max_external_files,
        max_external_bytes=args.max_external_bytes,
        max_archive_bytes=args.max_archive_bytes,
    )
    return generalized_run(compat)


if __name__ == "__main__":
    raise SystemExit(main())
