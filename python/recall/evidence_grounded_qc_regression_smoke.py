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
        "individual_identity_preserved": "yes",
        "ms_on_individual_cell_samples": "yes",
        "destructive_pooling_before_identity": "absent",
        "population_or_bulk_only": "no",
        "benchmark_only": "no",
        "mixed_controls_or_libraries_present": "no",
        "evidence_quote_cell": "individual cells were sorted into individual wells",
        "evidence_quote_chain": "digested cells were analyzed by LC-MS/MS",
        "evidence_quote_ms": "data were recorded in DIA mode on an Orbitrap Astral",
        "reason": "identity is preserved from an individual cell to its MS-derived sample",
    }
    base.update(overrides)
    return base


# Positive normalization must accept a complete chain even when the model's raw
# decision is cautious.
genuine = payload()
assert q.normalized_decision(genuine)[0] == "include"

# Separate multi-cell libraries/controls do not negate direct single-cell data.
mixed_controls = payload(mixed_controls_or_libraries_present="yes")
assert q.normalized_decision(mixed_controls)[0] == "include"

# Identity-preserving multiplexing is allowed: the exclusion axis is destructive
# pooling BEFORE identity is established, not physical combination after labels.
identity_preserving_multiplex = payload(
    evidence_quote_chain="each cell received a unique isobaric label before channels were combined",
)
assert q.normalized_decision(identity_preserving_multiplex)[0] == "include"

population = payload(
    decision="include",
    individual_cell_samples_present="no",
    individual_identity_preserved="no",
    ms_on_individual_cell_samples="no",
    destructive_pooling_before_identity="present",
    population_or_bulk_only="yes",
    evidence_quote_cell="10^6 root hair cells",
    evidence_quote_chain="proteins from each population sample were extracted",
    evidence_quote_ms="peptides were analyzed by LC-MS/MS",
    reason="many cells contributed to one proteomic sample",
)
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

# Older cache records must not be reusable in v0.1.10.
row = {"qc_packet_hash": "abc"}
old_cache = {
    "qc_version": "v0.1.9-evidence-grounded-qc-1",
    "packet_hash": "abc",
    "accession": "PXDTEST",
    "model": "phi4-mini:3.8b",
    **genuine,
}
assert not q.cache_payload_valid(old_cache, row=row, model="phi4-mini:3.8b")
new_cache = dict(old_cache, qc_version=q.QC_VERSION)
assert q.cache_payload_valid(new_cache, row=row, model="phi4-mini:3.8b")

# Jury is not called for every uncertain record merely because it is uncertain.
medium_sparse = {
    "unified_route": "review_medium",
    "review_flags": [],
    "qc_evidence_flags": [],
    "qc_primary_evidence": [],
    "qc_stage04_raw_samples": [],
}
unclear = payload(
    individual_cell_samples_present="unclear",
    individual_identity_preserved="unclear",
    ms_on_individual_cell_samples="unclear",
    destructive_pooling_before_identity="unclear",
    population_or_bulk_only="unclear",
    benchmark_only="unclear",
)
assert q.normalized_decision(unclear)[0] == "uncertain"
assert q.jury_required(medium_sparse, unclear)[0] is False

# Wrapped JSON is accepted, while a truncated object is rejected and therefore
# eligible for a corrective retry.
wrapped = "```json\n" + json.dumps(genuine) + "\n```"
assert q.parse_structured_response(wrapped)["individual_identity_preserved"] == "yes"
try:
    q.parse_structured_response('{"decision":"uncertain","individual_cell_samples_present":"yes"')
except ValueError:
    pass
else:
    raise AssertionError("truncated JSON should not parse")

# Exercise the actual corrective structured retry without network access.
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

print("All v0.1.10 positive-chain QC regression tests passed.")
print("Complete individual-cell-to-MS chains normalize to include despite cautious raw labels.")
print("Separate multi-cell controls and identity-preserving multiplexing do not negate SCP samples.")
print("Population/destructive-pooling evidence remains a deterministic exclusion.")
print("Older QC caches are invalidated automatically.")
print("Malformed/truncated structured output receives a corrective JSON retry.")
