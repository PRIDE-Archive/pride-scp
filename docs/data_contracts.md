# Data contracts

Generated runtime state lives under `data/` or `work/` and is intentionally not
tracked by Git.

## PRIDE snapshot

```text
data/snapshot/
  accessions.txt
  project_pages/
  projects/PXD....json
  files/PXD....json
  sdrf/PXD....sdrf.tsv
  errors/
  snapshot_summary.json
```

The repository/API SDRF cache is raw source state. A cache file existing does
not imply that it contains usable SDRF rows.

## Registry supplement

```text
data/snapshot/registry/
  pages/
  projects/
  accessions.txt
  registry_accessions.tsv
  registry_summary.json
```

Records preserve hosting repository, PXD aliases, and native accessions.

## Native MassIVE

```text
data/snapshot/native/massive/
  pages/
  projects/MSV....json
  accessions.txt
  massive_accessions.tsv
  massive_summary.json
```

Native identity is preserved even when a PXD alias exists.

## Discovery

Typical discovery output:

```text
data/discovery*/
  candidates.tsv
  candidates.jsonl
  project_discovery_audit.tsv
  discovery_summary.json
```

Candidate JSONL contains the source evidence that triggered retention.

## Python bridge

`export-python` writes a stable bridge such as:

```text
data/python_bridge/
  candidate_accessions.txt
  candidate_manifest.tsv
  semantic_candidates.jsonl
```

## SDRF source resolution

```text
data/sdrf_sources/
  cache/PXD.../
  resolved/PXD....sdrf.tsv
  audit/PXD....sdrf_source_audit.json
  sdrf_source_resolution.tsv
  sdrf_source_resolution_summary.json
```

Each selected source records source kind, URL, local path, usability status,
and deterministic content fingerprint.

## SDRF deterministic audit

```text
data/sdrf_audit/
  sdrf_audit_results.tsv
  sdrf_audit_summary.json
  review/PXD....sdrf.review.tsv
```

Important audit dimensions include structural validity, relation mode, missing
metadata, and repository-linkage status.

## SDRF annotation/reconstruction

```text
data/sdrf_annotation/
  evidence/PXD....evidence.json
  proposals/PXD....ollama.raw.json
  proposals/PXD....ollama.json
  sdrf/PXD....sdrf.tsv
  review/PXD....sdrf.review.tsv
  audit/PXD....sdrf.audit.json
  errors/
  sdrf_annotation_results.tsv
  sdrf_annotation_summary.json
```

The completed accession audit JSON is the resumability boundary. If its
version/spec/template identity matches the current generator, a later batch run
may reuse that accession without repeating Ollama work.

## Reference/GT data

Frozen reference collections are evaluation-only. Keep them outside the
production data flow and out of Git. Evaluation scripts may compare generated
outputs with those files after production inference is complete.
