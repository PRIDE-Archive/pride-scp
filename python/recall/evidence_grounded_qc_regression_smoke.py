#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


b = load("packet_builder", "build_semantic_qc_packets.py")
q = load("qc", "adjudicate_semantic_qc.py")

# Deterministic factual-axis normalization must resolve a cautious top-level
# `uncertain` when the axes themselves are decisive.
genuine = {
    "decision": "uncertain",
    "sample_unit": "individual_cell",
    "same_unit_ms_proteomics": "yes",
    "target_dataset_scope": "supports_target",
    "premeasurement_pooling": "absent",
    "benchmark_only": "no",
    "evidence_quote_cell": "Each egg was thawed and homogenized",
    "evidence_quote_ms": "until mass spectrometry analysis",
    "reason": "individual eggs were separately processed",
}
assert q.normalized_decision(genuine)[0] == "include"

population = {
    "decision": "include",
    "sample_unit": "population_or_pool",
    "same_unit_ms_proteomics": "yes",
    "target_dataset_scope": "supports_target",
    "premeasurement_pooling": "present",
    "benchmark_only": "no",
    "evidence_quote_cell": "10^6 root hair cells",
    "evidence_quote_ms": "peptides were analyzed by LC-MS/MS",
    "reason": "many cells contributed to one proteomic sample",
}
assert q.normalized_decision(population)[0] == "exclude"

# Risk pattern should surface a many-cell preparation passage.
flags = set(b.flags_for("We isolated 10^6 root hair cells by FACS and analyzed each sample by LC-MS/MS."))
assert "multi_cell_count_language" in flags
assert "ms_proteomics_language" in flags

# Rehydrate exact Stage-04 evidence and raw samples response.
with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "annotations"
    accdir = root / "PXDTEST"
    accdir.mkdir(parents=True)
    ann = accdir / "PXDTEST__pub1.json"
    ann.write_text(json.dumps({"target_accession": "PXDTEST", "publication_source_path": "/tmp/test.pdf"}))
    work = accdir / "PXDTEST__pub1.work"
    work.mkdir()
    (work / "evidence.json").write_text(json.dumps({
        "tasks": {
            "samples": [{"evidence_id":"E01","block_id":"B1","page":1,"section":"methods","text":"Absolute deep proteome of individual X. laevis eggs."}],
            "preparation": [{"evidence_id":"E02","block_id":"B2","page":2,"section":"methods","text":"Five eggs were used. Each egg was thawed and homogenized before mass spectrometry analysis."}],
        }
    }))
    (work / "samples.attempt1.raw.txt").write_text(json.dumps({
        "is_single_cell_proteomics":"yes",
        "true_single_cell_samples":[{"sample_type":"X. laevis eggs"}],
        "low_input_benchmarks":[],
    }))
    blocks, raw, sources = b.stage04_source_evidence(root, "PXDTEST", max_blocks=12)
    assert len(blocks) == 2
    assert raw and raw[0]["is_single_cell_proteomics"] == "yes"
    assert sources == ["/tmp/test.pdf"]

# v0.1.8 cache records must not be reusable in v0.1.9.
row = {"qc_packet_hash": "abc"}
old_cache = {
    "accession": "PXDTEST",
    "model": "phi4-mini:3.8b",
    **genuine,
}
assert not q.cache_payload_valid(old_cache, row=row, model="phi4-mini:3.8b")
new_cache = dict(old_cache, qc_version=q.QC_VERSION, packet_hash="abc")
assert q.cache_payload_valid(new_cache, row=row, model="phi4-mini:3.8b")

# Jury is no longer called for every uncertain record merely because it is uncertain.
medium_sparse = {
    "unified_route": "review_medium",
    "review_flags": [],
    "qc_evidence_flags": [],
    "qc_primary_evidence": [],
    "qc_stage04_raw_samples": [],
}
unclear = dict(genuine)
unclear.update({
    "sample_unit":"unclear",
    "same_unit_ms_proteomics":"unclear",
    "premeasurement_pooling":"unclear",
    "benchmark_only":"unclear",
})
assert q.normalized_decision(unclear)[0] == "uncertain"
assert q.jury_required(medium_sparse, unclear)[0] is False

print("All v0.1.9 evidence-grounded QC regression tests passed.")
print("Direct Stage-04 passages are rehydrated before independent QC.")
print("Structured factual axes override over-cautious/optimistic top-level decisions.")
print("v0.1.8 QC caches are invalidated automatically.")
print("Jury selection no longer expands to every critic-uncertain candidate.")
