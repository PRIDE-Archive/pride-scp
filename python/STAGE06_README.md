# PRIDE SCP Stage 06 — independent LLM QC

> **GT196 STATUS NOTE (September 2026):** this document describes the historical
> read-only Stage-06 claim-QC implementation for an already-built Stage-05
> catalogue. It is **not** the current GT196 accession-adjudication path and it
> must not be confused with the quarantined v0.1.8-v0.1.12 recall semantic-QC
> Phi/Gemma lane. Reconsider Stage 06 only after the primary annotation lane is
> calibrated against GT196. See `../docs/model_roles.md`.

Stage 06 is a **read-only** reviewer for the frozen Stage-05 catalogue.

It does not modify:
- `single_cell_dataset_catalogue.csv`
- `single_cell_samples.csv`
- `low_input_benchmarks.csv`
- Stage-04 annotation JSON files

## Default reviewer design

The default deliberately uses model families different from the Stage-04 Qwen
extractor:

1. `bespoke-minicheck`
   - purpose-built grounded factuality checker
   - one atomic catalogue claim at a time
   - source document + claim -> `Yes` / `No`

2. `phi4-mini:3.8b`
   - invoked only after MiniCheck returns `No`
   - structured critic
   - distinguishes missing/ambiguous evidence from explicit contradiction
   - may propose a correction for manual inspection
   - never edits the catalogue

This gives independent layers:

```text
Stage 04 Qwen extraction
        ↓
Stage 05 deterministic curation
        ↓
Bespoke-MiniCheck grounding
        ↓  only if unsupported
Phi-4-mini disagreement critic
        ↓
manual_review_queue.csv
```

## Install models

```bash
ollama pull bespoke-minicheck
ollama pull phi4-mini:3.8b
```

Alternative small critic models:

```bash
ollama pull gemma3:4b
ollama pull llama3.2:3b
```

Then substitute, for example:

```bash
--critic-model gemma3:4b
```

## Dry run

Run from the catalogue-pipeline directory:

```bash
python 06_review_pride_scp_catalogue.py \
  pride_scp_catalogue \
  --base-dir . \
  --output-dir pride_scp_qc_review_dry \
  --scope core \
  --dry-run
```

Inspect:

```text
pride_scp_qc_review_dry/evidence_packets/
```

## Recommended pilot

```bash
python 06_review_pride_scp_catalogue.py \
  pride_scp_catalogue \
  --base-dir . \
  --output-dir pride_scp_qc_review_pilot \
  --fact-model bespoke-minicheck \
  --critic-model phi4-mini:3.8b \
  --cpu-threads 4 \
  --scope core \
  --accession PXD000265 \
  --accession PXD043473 \
  --accession PXD049412 \
  --accession PXD059079 \
  --accession PXD058457
```

## Full core review

```bash
python 06_review_pride_scp_catalogue.py \
  pride_scp_catalogue \
  --base-dir . \
  --output-dir pride_scp_qc_review \
  --fact-model bespoke-minicheck \
  --critic-model phi4-mini:3.8b \
  --cpu-threads 4 \
  --scope core \
  --save-pdf-cache
```

## Technical metadata review

Use a separate output directory because the claim set differs:

```bash
python 06_review_pride_scp_catalogue.py \
  pride_scp_catalogue \
  --base-dir . \
  --output-dir pride_scp_qc_review_technical \
  --fact-model bespoke-minicheck \
  --critic-model phi4-mini:3.8b \
  --cpu-threads 4 \
  --scope technical \
  --save-pdf-cache
```

## Outputs

- `annotation_qc_review.csv`
- `annotation_qc_dataset_summary.csv`
- `manual_review_queue.csv`
- `annotation_qc_failures.csv`
- `qc_run_summary.json`
- `run_config.json`
- `evidence_packets/PXD....json`
- `claim_status/` — resumable per-claim state
- `raw/` — raw model responses
- `pdf_text_cache/` when `--save-pdf-cache` is used

## Decision semantics

- `pass`: MiniCheck found the atomic claim supported.
- `warn`: evidence was absent/partial/ambiguous, or reviewer models disagree.
- `fail`: Phi-4-mini found an explicit contradiction and cited a valid evidence
  ID.
- `not_reviewed`: no evidence packet, dry run, or reviewer execution error.

A `fail` is intentionally difficult to produce. Stage 06 deterministically
downgrades an attempted `fail` to `warn` unless the critic reports
`contradictory` evidence and cites at least one valid evidence ID.

`proposed_value` is a suggestion only. No correction is ever applied
automatically.
