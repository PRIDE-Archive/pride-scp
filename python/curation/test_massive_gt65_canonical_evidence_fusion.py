#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILDER = HERE / "build_massive_gt65_canonical_evidence_fusion.py"
MERGER = HERE / "merge_massive_gt65_c2_curation.py"


def write_tsv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as h:
        w = csv.DictWriter(h, fieldnames=fields, delimiter="\t")
        w.writeheader(); w.writerows(rows)


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_builder_fuses_trusted_native_evidence_without_gt() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        snapshot = root / "snapshot"
        massive = snapshot / "native" / "massive"
        projects = massive / "projects"
        projects.mkdir(parents=True)
        (massive / "identity_enrichment").mkdir(parents=True)

        pxd = "PXD000001"
        msvs = ["MSV000000001", "MSV000000002"]
        write_tsv(
            massive / "massive_accessions.tsv",
            [
                {"msv_accession": msvs[0], "pxd_aliases": pxd, "project_json_path": "x", "dataset_title": "A"},
                {"msv_accession": msvs[1], "pxd_aliases": pxd, "project_json_path": "y", "dataset_title": "B"},
            ],
            ["msv_accession", "pxd_aliases", "project_json_path", "dataset_title"],
        )
        for i, msv in enumerate(msvs, 1):
            project = {
                "accession": msv,
                "pxdAliases": [pxd],
                "sourceIdentity": {
                    "canonical_dataset_identity": "dataset_identity:test",
                    "pxd_aliases": [pxd],
                    "gt_used_for_identity": False,
                },
                "massiveNativeRecord": {
                    "title": f"Single-cell proteomics source {i}",
                    "description": "Individual cells were analyzed by LC-MS/MS.",
                },
            }
            (projects / f"{msv}.json").write_text(json.dumps(project), encoding="utf-8")

        cands = root / "m2c1.jsonl"
        cands.write_text(json.dumps({
            "accession": pxd,
            "score": 100,
            "tier": "strong",
            "dataset_title": "PXD sparse candidate",
            "gt_status": "must_be_removed",
        }) + "\n", encoding="utf-8")
        manifest = root / "manifest.tsv"
        fields = [
            "gt_accession", "recovery_route", "matched_candidates", "representative_accession",
            "representation_class", "representative_score", "representative_tier",
            "native_evidence_attached", "trusted_pxd_aliases", "dataset_title", "evaluation_only",
        ]
        write_tsv(manifest, [
            {"gt_accession": "MSV000000001", "recovery_route": "pxd_alias", "matched_candidates": pxd,
             "representative_accession": pxd, "representation_class": "pxd_candidate", "representative_score": "100",
             "representative_tier": "strong", "native_evidence_attached": "no", "trusted_pxd_aliases": "",
             "dataset_title": "A", "evaluation_only": "yes"},
            {"gt_accession": "MSV000000002", "recovery_route": "pxd_alias", "matched_candidates": pxd,
             "representative_accession": pxd, "representation_class": "pxd_candidate", "representative_score": "100",
             "representative_tier": "strong", "native_evidence_attached": "no", "trusted_pxd_aliases": "",
             "dataset_title": "B", "evaluation_only": "yes"},
        ], fields)
        out = root / "out"
        subprocess.run([
            sys.executable, str(BUILDER),
            "--m2c1-candidates-jsonl", str(cands),
            "--m2c1-manifest", str(manifest),
            "--snapshot", str(snapshot),
            "--output-dir", str(out),
            "--expect-gt-rows", "2",
            "--expect-pxd-gt-rows", "2",
            "--expect-unique-pxd-representatives", "1",
        ], check=True, capture_output=True, text=True)

        built = read_jsonl(out / "massive_gt65_canonical_fusion_candidates.jsonl")
        assert len(built) == 1
        row = built[0]
        assert row["accession"] == pxd
        assert "gt_status" not in row
        assert row["canonical_fused_native_accessions"] == msvs
        sidecar = json.loads(Path(row["native_evidence_path"]).read_text(encoding="utf-8"))
        assert sidecar["provenance"]["gt_used_for_identity"] is False
        assert sidecar["provenance"]["gt_used_for_fusion"] is False
        assert sidecar["linked_native_accessions"] == msvs
        assert sidecar["evidence_items"]
        assert any("MSV000000001:" in item["source_path"] for item in sidecar["evidence_items"])
        assert any("MSV000000002:" in item["source_path"] for item in sidecar["evidence_items"])
        summary = json.loads((out / "massive_gt65_canonical_fusion_build_summary.json").read_text())
        assert summary["fusion_coverage_complete"] is True
        assert summary["gt_labels_written_to_curation_candidates"] is False
        assert summary["gt_used_for_fusion"] is False


def test_merger_replaces_only_affected_rows() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        fields = ["accession", "run_status", "curation_decision", "decision_reason"]
        baseline = root / "baseline.tsv"
        affected = root / "affected.tsv"
        write_tsv(baseline, [
            {"accession": "MSV000000001", "run_status": "success", "curation_decision": "include", "decision_reason": "old-native"},
            {"accession": "PXD000001", "run_status": "success", "curation_decision": "review", "decision_reason": "old-pxd"},
        ], fields)
        write_tsv(affected, [
            {"accession": "PXD000001", "run_status": "success", "curation_decision": "include", "decision_reason": "fused"},
        ], fields)
        accessions = root / "affected.txt"; accessions.write_text("PXD000001\n")
        out = root / "merged.tsv"
        subprocess.run([
            sys.executable, str(MERGER),
            "--baseline-summary", str(baseline),
            "--affected-summary", str(affected),
            "--affected-accessions", str(accessions),
            "--output", str(out),
            "--expect-total", "2",
            "--expect-affected", "1",
        ], check=True, capture_output=True, text=True)
        with out.open(encoding="utf-8", newline="") as h:
            rows = {r["accession"]: r for r in csv.DictReader(h, delimiter="\t")}
        assert rows["MSV000000001"]["decision_reason"] == "old-native"
        assert rows["PXD000001"]["decision_reason"] == "fused"


if __name__ == "__main__":
    test_builder_fuses_trusted_native_evidence_without_gt()
    test_merger_replaces_only_affected_rows()
    print("ok - MassIVE M2-C2 canonical evidence fusion tests")
