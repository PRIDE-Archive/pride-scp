# Stage-06 v3.2 — memory-safe selective jury + context-aware pooling QC

> **GT196 STATUS NOTE (September 2026):** this document describes the historical
> read-only Stage-06 claim-QC implementation for an already-built Stage-05
> catalogue. It is **not** the current GT196 accession-adjudication path and it
> must not be confused with the quarantined v0.1.8-v0.1.12 recall semantic-QC
> Phi/Gemma lane. Reconsider Stage 06 only after the primary annotation lane is
> calibrated against GT196. See `../docs/model_roles.md`.

`stage6-qc-v3.2` is a focused hardening release built directly from the
validated v3.1 selective-jury implementation.

It does **not** change Stage 04 or Stage 05 and remains read-only with respect
to the curated catalogue.

## Why v3.2 exists

The first full 27-accession v3.1 core audit reached claim 115/134 before Linux
OOM-killed Ollama's `llama-server` at roughly 12.6 GB anonymous RSS. The first
failed claim had completed MiniCheck and Phi but had no Gemma output, consistent
with model-residency pressure during third-reviewer loading.

The same full audit also revealed three false-positive high-risk pooling
contexts:

- PXD019958: prior transcriptomics work pooling cells for whole-transcriptome
  sequencing;
- PXD020586: separately prepared single cells pooled with a booster in a
  multiplexed SCP workflow;
- PXD038699: discussion of the original pooled-cell DVP approach as prior
  methodology.

PXD000265 remains the positive regression: roughly 50–70 isolated egg cells
were transferred together into one SDS-sample-buffer droplet for proteomic
analysis.

## Reviewer architecture

The validated v3.1 decision policy is unchanged:

```text
MiniCheck YES + no deterministic high-risk evidence
    -> PASS directly

MiniCheck NO or deterministic high-risk evidence
    -> blind Phi + Gemma adjudication

FAIL
    -> requires two structured reviewers citing explicit contradictions
```

Model-to-model deliberation remains OFF by default.

## Memory hardening

Default Ollama residency is now:

```text
MiniCheck: 5m on ordinary easy claims
Phi:       0 (unload immediately)
Gemma:     0 (unload immediately)
```

When a claim requires structured adjudication, Stage 06 explicitly unloads
MiniCheck before loading Phi. Before loading a distinct Gemma jury model it
also explicitly unloads Phi. This keeps the warm MiniCheck optimization for
the majority of easy claims without retaining all three models during a
jury claim.

Relevant CLI controls:

```text
--fact-keep-alive
--critic-keep-alive
--jury-keep-alive
--keep-fact-loaded-during-adjudication
```

The last flag is an opt-out of the memory-safe behavior and should not normally
be used on the CPU-only workstation.

For an additional server-side guard, configure Ollama with one resident model
and one parallel request:

```ini
[Service]
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_NUM_PARALLEL=1"
```

then restart Ollama.

## Runtime retries and circuit breaker

Transient connection/time-out and selected 5xx failures are retried with
bounded exponential backoff.

Defaults:

```text
--ollama-retries 2
--ollama-retry-backoff 3
```

If Ollama remains unreachable after retries, the claim is stored with:

```text
error_kind = ollama_unavailable
```

and Stage 06 stops early by default rather than creating dozens of identical
`NOT_REVIEWED` rows. `runtime_state.json` records the planned/processed claim
counts and abort reason.

After restoring Ollama, rerun the same command with:

```text
--retry-errors
```

to resume claim-level state.

`--continue-after-ollama-unavailable` is available but is not recommended.

## Pooling-context hardening

A high-risk pooling flag now excludes local contexts that are clearly:

- transcriptomics/RNA-seq rather than the target proteomics measurement;
- prior/original/earlier methodology being discussed as background;
- identity-preserving multiplex/carrier/booster/isobaric pooling after
  separately prepared single cells.

The destructive multi-cell-to-one-container pattern remains high priority.

Validated regressions:

```text
PXD000265  -> high-risk retained
PXD019958  -> transcriptomics pooling excluded
PXD020586  -> booster/multiplex pooling excluded
PXD038699  -> prior pooled-DVP background excluded
```

## Validation

Run:

```bash
python -m py_compile \
  06_review_pride_scp_catalogue.py \
  stage06_v32_regression_smoke.py

python stage06_v32_regression_smoke.py
```

Expected:

```text
All Stage-06 v3.2 regression tests passed.
v2.1 retrieval regressions retained; v3.1 full-core pooling false positives excluded.
MiniCheck Yes + no-risk PASSes without structured adjudication.
Phi + Gemma are selective adjudicators for MiniCheck No/high-risk only.
Deliberation is OFF by default.
FAIL still requires two structured cited contradictions.
Ollama retry, explicit unload, and memory-safe reviewer sequencing regressions passed.
```

## Recommended v3.2 run

Because v3.2 changes both the Stage-06 version/config fingerprint and the
high-risk evidence semantics, use a **new output directory** rather than
mixing v3.2 statuses into the interrupted v3.1 directory:

```bash
python 06_review_pride_scp_catalogue.py \
  pride_scp_catalogue \
  --base-dir . \
  --output-dir pride_scp_qc_review_core_v32 \
  --fact-model bespoke-minicheck \
  --critic-model phi4-mini:3.8b \
  --jury-model gemma3:4b \
  --cpu-threads 4 \
  --scope core
```

The v3.1 partial output should be retained for audit/history.
