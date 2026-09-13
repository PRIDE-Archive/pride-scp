#!/usr/bin/env python3
"""Freeze exact-hash review material for release20 repair4 after deterministic readiness."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import tarfile
from pathlib import Path

TARGETS = ["PXD019515", "PXD019958", "PXD054066"]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_if(src: Path, dst: Path) -> bool:
    if not src.is_file():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--readiness-output", required=True, type=Path)
    ap.add_argument("--repair-output", required=True, type=Path)
    ap.add_argument("--output-dir", required=True, type=Path)
    ap.add_argument("--archive", required=True, type=Path)
    ap.add_argument("--accession", action="append", choices=TARGETS, help="limit review bundle to selected accession(s); default: all Repair4 targets")
    ns = ap.parse_args()

    out = ns.output_dir
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    rows: list[dict[str, str]] = []
    targets = ns.accession or TARGETS

    for accession in targets:
        readiness_json = ns.readiness_output / "accessions" / f"{accession}.readiness.json"
        if not readiness_json.is_file():
            raise SystemExit(f"{accession}: missing readiness JSON")
        readiness = json.loads(readiness_json.read_text(encoding="utf-8"))
        if readiness.get("state") != "needs_independent_review":
            raise SystemExit(
                f"{accession}: expected needs_independent_review, got {readiness.get('state')}"
            )
        projected = ns.readiness_output / "projected" / accession / f"{accession}.sdrf.tsv"
        candidate = ns.repair_output / "candidates" / accession / f"{accession}.sdrf.tsv"
        repair_manifest = ns.repair_output / "manifests" / f"{accession}.repair.json"
        for path in (projected, candidate, repair_manifest):
            if not path.is_file():
                raise SystemExit(f"{accession}: missing {path}")
        digest = sha256(projected)
        expected = str(readiness.get("projected_sha256") or "").lower()
        if digest != expected:
            raise SystemExit(f"{accession}: projected SHA mismatch: {digest} != {expected}")

        accession_dir = out / "accessions" / accession
        copy_if(projected, accession_dir / f"{accession}.projected.sdrf.tsv")
        copy_if(candidate, accession_dir / f"{accession}.repaired_candidate.sdrf.tsv")
        copy_if(repair_manifest, accession_dir / f"{accession}.repair.json")
        copy_if(readiness_json, accession_dir / f"{accession}.readiness.json")

        # Preserve the same independent evidence lane used for release20 review, when locally present.
        base = ns.repo / "data" / "sdrf_annotation_gt105_pride_v031_full"
        for subdir, suffix in (
            ("evidence", ".evidence.json"),
            ("audit", ".sdrf.audit.json"),
            ("review", ".sdrf.review.tsv"),
            ("proposals", ".ollama.json"),
        ):
            copy_if(
                base / subdir / f"{accession}{suffix}",
                accession_dir / "evidence" / subdir / f"{accession}{suffix}",
            )
        copy_if(
            ns.repo / "data" / "snapshot" / "projects" / f"{accession}.json",
            accession_dir / "evidence" / "snapshot" / f"{accession}.project.json",
        )
        copy_if(
            ns.repo / "data" / "snapshot" / "files" / f"{accession}.json",
            accession_dir / "evidence" / "snapshot" / f"{accession}.files.json",
        )
        rows.append(
            {
                "accession": accession,
                "projected_sha256": digest,
                "review_status": "pending",
                "readiness_state": str(readiness.get("state") or ""),
            }
        )

    inventory = out / "review_inventory.tsv"
    with inventory.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, delimiter="\t", fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    sums: list[str] = []
    for path in sorted(x for x in out.rglob("*") if x.is_file()):
        if path.name == "SHA256SUMS":
            continue
        sums.append(f"{sha256(path)}  {path.relative_to(out)}")
    (out / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")

    ns.archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(ns.archive, "w:gz") as tf:
        tf.add(out, arcname=out.name)
    print(f"review_accessions={len(rows)}")
    print(f"archive={ns.archive}")
    print(f"sha256={sha256(ns.archive)}")


if __name__ == "__main__":
    main()
