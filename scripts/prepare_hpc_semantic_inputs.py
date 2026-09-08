#!/usr/bin/env python3
"""Create a portable, accession-scoped input bundle for offline HPC semantic extraction."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
from pathlib import Path
from typing import Iterable

VERSION = "pride-scp-hpc-semantic-input-bundler-v0.1"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_accessions(path: Path) -> list[str]:
    vals = []
    seen = set()
    for raw in path.read_text(errors="replace").splitlines():
        acc = raw.strip().upper()
        if acc and not acc.startswith("#") and acc not in seen:
            seen.add(acc); vals.append(acc)
    return vals


def first(row: dict[str, str], *keys: str) -> str:
    for k in keys:
        v = (row.get(k) or "").strip()
        if v:
            return v
    return ""


def resolve_content_path(raw: str, manifest: Path) -> Path | None:
    if not raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        p = manifest.parent / p
    return p


def copy_snapshot(accessions: Iterable[str], snapshot: Path, out: Path) -> dict[str, int]:
    counts = {"projects": 0, "files": 0, "missing_projects": 0, "missing_files": 0}
    for sub in ("projects", "files"):
        (out / "snapshot" / sub).mkdir(parents=True, exist_ok=True)
    for acc in accessions:
        project_candidates = [snapshot / "projects" / f"{acc}.json", snapshot / "project" / f"{acc}.json", snapshot / f"{acc}.json"]
        src = next((p for p in project_candidates if p.is_file()), None)
        if src:
            shutil.copy2(src, out / "snapshot" / "projects" / f"{acc}.json"); counts["projects"] += 1
        else:
            counts["missing_projects"] += 1
        fsrc = snapshot / "files" / f"{acc}.json"
        if fsrc.is_file():
            shutil.copy2(fsrc, out / "snapshot" / "files" / f"{acc}.json"); counts["files"] += 1
        else:
            counts["missing_files"] += 1
    return counts


def rewrite_manifest(accessions: set[str], manifest: Path, out: Path) -> dict[str, int]:
    content_dir = out / "publication_content"
    content_dir.mkdir(parents=True, exist_ok=True)
    with manifest.open(newline="", errors="replace") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if "publication_content_text_path" not in fieldnames:
        fieldnames.append("publication_content_text_path")
    selected = []
    copied_by_sha: dict[str, str] = {}
    counts = {"rows": 0, "texts_copied": 0, "rows_without_local_text": 0}
    for row in rows:
        acc = first(row, "accession", "project_accession", "pxd").upper()
        if acc not in accessions:
            continue
        counts["rows"] += 1
        raw_path = first(row, "publication_content_text_path", "text_path", "content_text_path")
        src = resolve_content_path(raw_path, manifest)
        if src and src.is_file():
            digest = sha256_file(src)
            if digest in copied_by_sha:
                rel = copied_by_sha[digest]
            else:
                safe_name = src.name.replace(" ", "_")
                dst_name = f"{digest[:16]}_{safe_name}"
                dst = content_dir / dst_name
                shutil.copy2(src, dst)
                rel = str(Path("publication_content") / dst_name)
                copied_by_sha[digest] = rel
                counts["texts_copied"] += 1
            row["publication_content_text_path"] = rel
        else:
            row["publication_content_text_path"] = ""
            counts["rows_without_local_text"] += 1
        selected.append(row)
    out_manifest = out / "publication_manifest.tsv"
    with out_manifest.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, delimiter="\t", lineterminator="\n", extrasaction="ignore")
        writer.writeheader(); writer.writerows(selected)
    return counts


def copy_v053(src: Path | None, out: Path) -> int:
    if not src or not src.is_dir():
        return 0
    dst = out / "v053_audit"
    dst.mkdir(parents=True, exist_ok=True)
    copied = 0
    for name in ("branch_contracts.tsv", "branch_file_membership.tsv", "join_evidence.tsv", "relation_assessments.tsv", "accession_summary.tsv"):
        p = src / name
        if p.is_file():
            shutil.copy2(p, dst / name); copied += 1
    return copied


def run(args: argparse.Namespace) -> int:
    accessions = read_accessions(args.accessions_file)
    if not accessions:
        raise SystemExit("no accessions found")
    out = args.output.resolve()
    if out.exists() and args.rebuild:
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "accessions.txt").write_text("\n".join(accessions) + "\n")
    snapshot_counts = copy_snapshot(accessions, args.snapshot.resolve(), out)
    manifest_counts = rewrite_manifest(set(accessions), args.publication_manifest.resolve(), out)
    v053_files = copy_v053(args.v053_audit.resolve() if args.v053_audit else None, out)
    seed_graph = ""
    if args.seed_graph:
        src = args.seed_graph.resolve()
        if not src.is_file():
            raise SystemExit(f"seed graph missing: {src}")
        dst = out / "seed_graph.sqlite"
        shutil.copy2(src, dst)
        seed_graph = str(dst.name)
    summary = {
        "bundler_version": VERSION,
        "accessions": len(accessions),
        "snapshot": snapshot_counts,
        "publication_manifest": manifest_counts,
        "v053_files": v053_files,
        "seed_graph": seed_graph,
        "bundle_root": str(out),
    }
    (out / "bundle_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    hashes = []
    for p in sorted(x for x in out.rglob("*") if x.is_file() and x.name != "bundle.sha256"):
        hashes.append(f"{sha256_file(p)}  {p.relative_to(out)}")
    (out / "bundle.sha256").write_text("\n".join(hashes) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


def self_test() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td); snap = root / "snap"; (snap / "projects").mkdir(parents=True); (snap / "files").mkdir()
        acc = "PXD900001"
        (snap / "projects" / f"{acc}.json").write_text('{"title":"Synthetic SCP"}')
        (snap / "files" / f"{acc}.json").write_text('[]')
        txt = root / "paper.txt"; txt.write_text("single cell proteomics")
        manifest = root / "manifest.tsv"
        manifest.write_text("accession\tpublication_content_text_path\n" + f"{acc}\t{txt}\n")
        al = root / "accessions.txt"; al.write_text(acc + "\n")
        args = argparse.Namespace(accessions_file=al, snapshot=snap, publication_manifest=manifest, output=root/"bundle", seed_graph=None, v053_audit=None, rebuild=True)
        run(args)
        rows = list(csv.DictReader((root/"bundle"/"publication_manifest.tsv").open(), delimiter="\t"))
        rel = Path(rows[0]["publication_content_text_path"])
        assert not rel.is_absolute() and (root/"bundle"/rel).is_file()
    print("prepare_hpc_semantic_inputs self-test: PASS")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--accessions-file", type=Path)
    p.add_argument("--snapshot", type=Path)
    p.add_argument("--publication-manifest", type=Path)
    p.add_argument("--output", type=Path)
    p.add_argument("--seed-graph", type=Path)
    p.add_argument("--v053-audit", type=Path)
    p.add_argument("--rebuild", action="store_true")
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()
    if args.self_test:
        self_test(); return 0
    if not all((args.accessions_file, args.snapshot, args.publication_manifest, args.output)):
        raise SystemExit("--accessions-file, --snapshot, --publication-manifest and --output are required")
    return run(args)

if __name__ == "__main__":
    raise SystemExit(main())
