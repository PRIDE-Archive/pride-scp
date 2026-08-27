#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


b = load("packet_builder", "build_semantic_qc_packets.py")
q = load("qc", "adjudicate_semantic_qc.py")


def payload(**overrides):
    base = {
        "decision": "uncertain",
        "individual_cell_samples_present": "yes",
        "target_single_cell_ms_samples_present": "yes",
        "cells_per_target_ms_sample": "one",
        "individual_identity_preserved_to_ms": "yes",
        "destructive_pooling_before_identity": "absent",
        "population_samples_only": "no",
        "benchmark_only": "no",
        "separate_multi_cell_controls_present": "no",
        "evidence_quote_cell": "individual cells were sorted into individual wells",
        "evidence_quote_sample_unit": "one cell was deposited per target well",
        "evidence_quote_chain": "digested cells from those wells were analyzed by LC-MS/MS",
        "evidence_quote_ms": "data were recorded in DIA mode on an Orbitrap Astral",
        "reason": "one biological cell contributes to each target MS sample",
    }
    base.update(overrides)
    return base


# Complete target one-cell-to-MS chain must include even when raw label is cautious.
genuine = payload()
assert q.normalized_decision(genuine)[0] == "include"

# Mixed design: genuine one-cell target samples plus separate 20/40-cell libraries.
mixed = payload(
    cells_per_target_ms_sample="mixed_design",
    separate_multi_cell_controls_present="yes",
    benchmark_only="yes",  # internally inconsistent model field
    reason="one-cell target wells coexist with separate 20/40-cell libraries",
)
decision, basis = q.normalized_decision(mixed)
assert decision == "include"
assert "overrides_inconsistent_benchmark_only" in basis

# PXD028991-style many-cell target sample: individual cells exist upstream but
# 10^6/106 cells contribute to each proteomic replicate. The sample-unit axis
# must override an optimistic top-level include / target_single_cell=yes error.
population = payload(
    decision="include",
    target_single_cell_ms_samples_present="yes",
    cells_per_target_ms_sample="multiple",
    individual_identity_preserved_to_ms="yes",
    destructive_pooling_before_identity="absent",
    population_samples_only="no",
    evidence_quote_cell="10^6 root hair cells isolated by FACS",
    evidence_quote_sample_unit="10^6 root hair cells from each replicate sample",
    evidence_quote_chain="proteins from each sample were extracted and digested",
    evidence_quote_ms="peptides from each sample were analyzed by LC-MS/MS",
    reason="many cells feed each target proteomic sample",
)
decision, basis = q.normalized_decision(population)
assert decision == "exclude"
assert "target_ms_sample_contains_multiple_cells" in basis

# Explicit destructive pooling remains an exclusion.
pooled = payload(
    cells_per_target_ms_sample="multiple",
    destructive_pooling_before_identity="present",
    population_samples_only="yes",
)
assert q.normalized_decision(pooled)[0] == "exclude"

# Evidence-aware arbitration: a soft benchmark-only critic exclusion must not
# defeat a complete single-cell target-MS jury chain.
soft_critic = payload(
    decision="exclude",
    benchmark_only="yes",
    cells_per_target_ms_sample="mixed_design",
    separate_multi_cell_controls_present="yes",
)
jury_positive = payload(decision="include", cells_per_target_ms_sample="mixed_design")
final, basis, _, _ = q.combine_decisions(soft_critic, jury_positive)
assert final == "include"

# Conversely, a direct many-cell target-sample finding must defeat an optimistic
# positive chain from the other reviewer.
critic_many = population
jury_optimistic = payload(decision="include")
final, basis, _, _ = q.combine_decisions(critic_many, jury_optimistic)
assert final == "exclude"
assert "hard_sample_unit_exclusion" in basis

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

# v0.1.10 caches must be invalidated.
row = {"qc_packet_hash": "abc"}
old_cache = {
    "qc_version": "v0.1.10-positive-chain-qc-1",
    "packet_hash": "abc",
    "accession": "PXDTEST",
    "model": "phi4-mini:3.8b",
    **genuine,
}
assert not q.cache_payload_valid(old_cache, row=row, model="phi4-mini:3.8b")
new_cache = dict(old_cache, qc_version=q.QC_VERSION)
assert q.cache_payload_valid(new_cache, row=row, model="phi4-mini:3.8b")

# Jury remains selective for sparse medium-review uncertainty.
medium_sparse = {
    "unified_route": "review_medium",
    "review_flags": [],
    "qc_evidence_flags": [],
    "qc_primary_evidence": [],
    "qc_stage04_raw_samples": [],
}
unclear = payload(
    individual_cell_samples_present="unclear",
    target_single_cell_ms_samples_present="unclear",
    cells_per_target_ms_sample="unclear",
    individual_identity_preserved_to_ms="unclear",
    destructive_pooling_before_identity="unclear",
    population_samples_only="unclear",
    benchmark_only="unclear",
    separate_multi_cell_controls_present="unclear",
)
assert q.normalized_decision(unclear)[0] == "uncertain"
assert q.jury_required(medium_sparse, unclear)[0] is False

# Wrapped JSON accepted; truncated JSON gets corrective retry.
wrapped = "```json\n" + json.dumps(genuine) + "\n```"
assert q.parse_structured_response(wrapped)["cells_per_target_ms_sample"] == "one"
try:
    q.parse_structured_response('{"decision":"uncertain","individual_cell_samples_present":"yes"')
except ValueError:
    pass
else:
    raise AssertionError("truncated JSON should not parse")

valid_raw = json.dumps(genuine)
responses = [
    {"response": '{"decision":"uncertain","individual_cell_samples_present":"yes"', "eval_count": 520},
    {"response": valid_raw, "eval_count": 180},
]

class FakeResponse:
    status_code = 200
    text = ""
    def __init__(self, data):
        self._data = data
    def json(self):
        return self._data
    def raise_for_status(self):
        return None

calls = []
orig_post = q.requests.post

def fake_post(url, json=None, timeout=None):
    calls.append(json)
    return FakeResponse(responses.pop(0))

q.requests.post = fake_post
try:
    args = SimpleNamespace(
        structured_retries=1,
        retries=0,
        retry_backoff=0.0,
        timeout=30,
        num_ctx=8192,
        cpu_threads=4,
    )
    parsed, stats = q.post_generate(
        url="http://localhost:11434/api/generate",
        model="gemma3:4b",
        prompt="test prompt",
        args=args,
        keep_alive="0",
        num_predict=800,
    )
    assert parsed["decision"] == "uncertain"
    assert stats["structured_attempts"] == 2
    assert len(calls) == 2
    assert "CORRECTIVE OUTPUT INSTRUCTION" in calls[1]["prompt"]
finally:
    q.requests.post = orig_post

print("All v0.1.11 MS-sample-unit QC regression tests passed.")
print("Many-cell target MS samples override optimistic single-cell labels.")
print("Complete one-cell target-MS chains override inconsistent benchmark-only flags.")
print("Evidence-aware critic/jury arbitration preserves hard sample-unit exclusions.")
print("Separate multi-cell controls remain compatible with genuine single-cell target samples.")
print("Malformed structured output still receives a corrective JSON retry.")
