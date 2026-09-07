#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import json
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("m2_identity", HERE / "enrich_massive_native_identity.py")
MOD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MOD
assert SPEC.loader is not None
SPEC.loader.exec_module(MOD)


def test_free_text_pxd_is_not_structured_identity():
    value = {
        "title": "Reanalysis of PXD047101",
        "description": "comparison to PXD099999",
        "dataset_id": "MSV000093434",
    }
    evidence = MOD.structured_accession_evidence(value)
    assert "PXD047101" not in evidence
    assert "PXD099999" not in evidence
    assert "MSV000093434" in evidence


def test_proxi_identifiers_promote_pxd():
    value = {
        "identifiers": [
            {"accession": "MS:1001919", "name": "ProteomeXchange accession number", "value": "PXD047101"},
            {"accession": "MS:1002634", "name": "MassIVE dataset identifier", "value": "MSV000093434"},
        ],
        "description": "mentions PXD999999 only as prior work",
    }
    rows = MOD.collect_alias_evidence("MSV000093434", "massive_proxi", value, direct_resource=True)
    assert {r["pxd_accession"] for r in rows} == {"PXD047101"}


def test_detail_positional_requires_same_row_exact_msv():
    value = {
        "rows": [
            ["MSV000093434", "Single cell proteomics", "PXD047101", "public"],
            ["MSV000099999", "Mentions PXD000001 in title", "public"],
        ]
    }
    evidence = MOD.massive_detail_alias_evidence(value, "MSV000093434")
    assert set(evidence) == {"PXD047101"}


def test_proteomecentral_search_requires_structured_msv_match():
    unrelated = {
        "datasets": [{
            "identifiers": [{"name": "ProteomeXchange accession number", "value": "PXD123456"}],
            "description": "free text mentions MSV000093434",
        }]
    }
    rows = MOD.collect_alias_evidence(
        "MSV000093434", "proteomecentral_search", unrelated, direct_resource=False
    )
    assert rows == []

    matched = {
        "datasets": [{
            "identifiers": [
                {"name": "ProteomeXchange accession number", "value": "PXD047101"},
                {"name": "dataset identifier", "value": "MSV000093434"},
            ]
        }]
    }
    rows = MOD.collect_alias_evidence(
        "MSV000093434", "proteomecentral_search", matched, direct_resource=False
    )
    assert {r["pxd_accession"] for r in rows} == {"PXD047101"}


def test_component_key_matches_m2a_sha12_contract():
    assert MOD.stable_component_key({"MSV000093434", "PXD047101"}) == MOD.stable_component_key(
        {"PXD047101", "MSV000093434"}
    )
    assert MOD.stable_component_key({"MSV000093434", "PXD047101"}).startswith("dataset_identity:")


def test_publication_doi_extraction():
    value = {
        "publications": [{"terms": [{"name": "DOI", "value": "10.1234/ABC.DEF"}]}],
        "fullDatasetLinks": [{"name": "publication", "value": "https://doi.org/10.5678/xyz"}],
    }
    dois, urls, _, _ = MOD.extract_publication_metadata(value)
    assert "10.1234/abc.def" in dois
    assert "10.5678/xyz" in dois
    assert any("doi.org" in url for url in urls)


def main():
    tests = [name for name in globals() if name.startswith("test_")]
    for name in sorted(tests):
        globals()[name]()
    print(f"enrich_massive_native_identity: {len(tests)} tests ok")


def test_end_to_end_apply_uses_only_structured_cached_sources():
    import csv
    import subprocess
    import sys

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        snapshot = root / "snapshot"
        massive = snapshot / "native" / "massive"
        projects = massive / "projects"
        cache = massive / "identity_enrichment"
        registry = snapshot / "registry"
        out = root / "out"
        projects.mkdir(parents=True)
        registry.mkdir(parents=True)

        (projects / "MSV000000111.json").write_text(json.dumps({
            "accession": "MSV000000111",
            "title": "single-cell",
            "pxdAliases": [],
            "massiveNativeRecord": {"description": "free text mentions PXD999999"},
        }), encoding="utf-8")
        (projects / "MSV000000222.json").write_text(json.dumps({
            "accession": "MSV000000222",
            "title": "single-cell native only",
            "pxdAliases": [],
            "massiveNativeRecord": {"description": "no alias"},
        }), encoding="utf-8")

        with (massive / "massive_accessions.tsv").open("w", encoding="utf-8", newline="") as h:
            w = csv.DictWriter(h, fieldnames=["msv_accession", "pxd_aliases", "project_json_path", "dataset_title"], delimiter="\t")
            w.writeheader()
            w.writerow({"msv_accession": "MSV000000111", "pxd_aliases": "", "project_json_path": str(projects / "MSV000000111.json"), "dataset_title": "single-cell"})
            w.writerow({"msv_accession": "MSV000000222", "pxd_aliases": "", "project_json_path": str(projects / "MSV000000222.json"), "dataset_title": "single-cell native only"})
        with (registry / "registry_accessions.tsv").open("w", encoding="utf-8", newline="") as h:
            w = csv.DictWriter(h, fieldnames=["pxd_accession", "hosting_repository", "native_accessions", "present_in_primary_snapshot", "registry_json_path", "dataset_title"], delimiter="\t")
            w.writeheader()

        delta = root / "delta.tsv"
        with delta.open("w", encoding="utf-8", newline="") as h:
            w = csv.DictWriter(h, fieldnames=["candidate_accession", "native_msv"], delimiter="\t")
            w.writeheader()
            w.writerow({"candidate_accession": "MSV000000111", "native_msv": "MSV000000111"})
            w.writerow({"candidate_accession": "MSV000000222", "native_msv": "MSV000000222"})

        MOD.atomic_write_json(cache / "massive_proxi" / "MSV000000111.json", {
            "identifiers": [
                {"name": "ProteomeXchange accession number", "value": "PXD000001"},
                {"name": "dataset identifier", "value": "MSV000000111"},
            ]
        })
        MOD.atomic_write_json(cache / "massive_proxi" / "MSV000000222.json", {
            "identifiers": [{"name": "dataset identifier", "value": "MSV000000222"}]
        })
        # Keep the unresolved fixture fully offline.
        for subdir in ["massive_detail", "proteomecentral_direct", "proteomecentral_search"]:
            marker = cache / subdir / "MSV000000222.json.not_found"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("fixture\n", encoding="utf-8")

        subprocess.run([
            sys.executable, str(HERE / "enrich_massive_native_identity.py"),
            "--snapshot", str(snapshot),
            "--delta-tsv", str(delta),
            "--output-dir", str(out),
            "--expect-candidates", "2",
            "--apply",
        ], check=True, stdout=subprocess.DEVNULL)

        p1 = json.loads((projects / "MSV000000111.json").read_text())
        p2 = json.loads((projects / "MSV000000222.json").read_text())
        assert p1["pxdAliases"] == ["PXD000001"]
        assert p2["pxdAliases"] == []
        assert p1["sourceIdentity"]["gt_used_for_identity"] is False
        assert "PXD999999" in p1["sourceIdentity"]["unverified_pxd_tokens"]
        summary = json.loads((out / "massive_native_identity_enrichment_summary.json").read_text())
        assert summary["records_newly_resolved"] == 1
        assert summary["records_unresolved_after"] == 1
        assert summary["gt_used_for_identity"] is False

if __name__ == "__main__":
    main()
