# Model roles and review policy

This document describes the model responsibilities in the **current GT196
optimization architecture**. It is intentionally explicit about which model
paths are active, historical, or quarantined.

The core rule is:

> Models interpret bounded source evidence. They do not define ground truth,
> they do not control discovery recall, and an unsupported model decision must
> not silently delete a candidate.

The frozen reference under `gpt/final_curation_20260831/` is an evaluation
artifact only. Production inference must never look up GT accessions.

## Current model map

| Component | Default model | Current status | Responsibility | Must not do |
|---|---|---|---|---|
| Rust discovery | none | **active** | Deterministic high-recall candidate generation from repository metadata, file names/manifests, SDRF, method vocabulary, biological-unit language and bounded regex patterns. | Use an LLM or GT accession lookup to decide candidate membership. |
| Publication-backed Stage 04 | `qwen2.5:3b` | **active, under GT196 re-benchmarking** | Five compact source-grounded extraction tasks: sample/SCP classification, preparation/isolation, LC configuration, genuine single-cell performance, and low-input performance. | Be treated as final benchmark truth. |
| Stage-04 deterministic gate | none | **active** | Validate/normalize Qwen output and apply source-evidence rules for accession scope, direct cell-to-MS evidence, false friends, synthetic benchmarks, non-MS modalities and metadata sanity. | Recover candidates already lost by discovery. |
| Repository-only triage | `qwen2.5:3b` | **active, non-destructive** | Interpret only repository evidence and emit structured cell/MS evidence plus a triage class for candidates with no usable publication content. | Automatically exclude a candidate or claim certainty from a method name alone. |
| Semantic unifier | none | **active** | Merge publication-backed and repository-only evidence without candidate loss; assign review routes and flags. | Turn an LLM opinion into an irreversible exclusion. |
| Recall semantic critic | `phi4-mini:3.8b` | **quarantined for affirmative/final decisions** | Historical v0.1.8-v0.1.12 source-grounded critic over structured one-cell-to-MS axes. Useful only as historical error-analysis code until redesigned and revalidated. | Supply GT labels, override the frozen reference, or feed Stage 05 automatically. |
| Recall semantic jury | `gemma3:4b` | **quarantined for affirmative/final decisions** | Historical selective second reviewer for critic conflicts/uncertainty. | Be used as an approved current adjudicator. |
| Historical Stage-06 fact checker | `bespoke-minicheck` | **historical/read-only audit** | Check individual catalogue claims against retrieved source passages. Easy supported claims passed directly in the old v3.2 selective-jury design. | Define whether an accession belongs in the current GT-driven catalogue. |
| Historical Stage-06 critic/jury | `phi4-mini:3.8b` + `gemma3:4b` | **historical/read-only audit** | Adjudicate MiniCheck non-support or deterministic high-risk claims in the older final-catalogue QC lane. | Be confused with the quarantined recall semantic-QC lane or automatically rewrite catalogue values. |

## 1. `qwen2.5:3b` — publication-backed Stage 04

The targeted annotator in `python/stages/pride_scp_targeted_ollama.py` is a
hybrid deterministic/LLM extractor. It first parses and retrieves source
passages deterministically, then makes up to five small structured Ollama
calls.

### Task A — samples / SCP interpretation

Extracts:

- whether the supplied evidence describes genuine single-cell proteomics;
- genuine individual-cell sample types;
- organisms;
- cell-count statements tied to those samples;
- low-input/dilution benchmark samples.

This model-level `is_single_cell_proteomics` value is **not** accepted blindly.
The deterministic Stage-04 evidence gate can override it.

### Task B — sample preparation / isolation

Extracts concise physical sample-preparation metadata and individual-cell
isolation methods. Deterministic sanitizers reject unsupported or generic
sample-preparation prose.

### Task C — LC

Extracts LC/UHPLC system/column configuration and gradients from explicit LC
evidence. Deterministic checks prevent sample-preparation text from leaking
into LC fields.

### Task D — genuine single-cell performance

Extracts quantitative throughput and proteome-depth statements specifically
for genuine one-cell measurements. Deterministic source matching can replace
an unsafe semantic numeric combination.

### Task E — low-input performance

Extracts quantitative performance for dilution/low-input benchmarks separately
from genuine one-cell performance.

The current imported annotator identifies itself as:

```text
annotation_pipeline_version = v18
classification_policy_version = v17
metadata_qc_version = v18
```

### Why Stage 04 is not final truth

The frozen baseline showed that the historical Stage-04 gate called only
36/64 publication-backed recovered GT positives `yes` (56.25%). The current
recall-first semantic unifier therefore retains high-recall conflicts instead
of allowing a negative Stage-04 decision to delete the accession.

The next annotation optimization phase should treat Qwen primarily as a
**structured source interpreter/extractor**, then benchmark each factual axis
against GT196 before allowing any automated inclusion/exclusion policy.

## 2. `qwen2.5:3b` — repository-only triage

`python/recall/triage_repository_candidates.py` uses a separate prompt and only
the Rust evidence packet. It emits:

- `triage_class`;
- `individual_cell_measurement_evidence`;
- `mass_spectrometry_proteomics_evidence`;
- a short reason.

This lane is deliberately recall-oriented and non-destructive. Repository
evidence can be sparse, so `possible`/`uncertain` is preferred to invented
certainty. Method names can justify review but do not prove a one-cell target
MS sample.

## 3. Deterministic semantic unification

`python/recall/unify_semantic_results.py` is not an LLM. It combines:

- discovery score/tier and semantic priority;
- publication-backed Stage-04 summaries;
- repository-only Qwen summaries;
- explicit evidence conflicts and negative-context flags.

It routes every candidate into review categories such as
`include_candidate`, `review_high`, `review_medium`, `review_low`, and
`likely_non_scp` while preserving the accession in the recall universe.

In the frozen pre-Iteration-1 321-candidate baseline this unifier retained all
97 Rust-recovered PRIDE GT positives even when Stage 04 was negative.

## 4. Quarantined recall semantic QC — Phi/Gemma

The v0.1.8-v0.1.12 recall semantic-QC implementation lives in:

```text
python/recall/build_semantic_qc_packets.py
python/recall/adjudicate_semantic_qc.py
scripts/run_semantic_qc.sh
```

Its critic and jury were designed around useful factual axes:

- individual cells present;
- genuine one-cell target MS samples present;
- cells per target MS sample;
- individual identity preserved to MS;
- destructive pooling before identity;
- population-only samples;
- benchmark-only status;
- mixed/separate controls.

However, the historical affirmative/final decisions from this lane were not
reliable enough against the independent reference. **They are quarantined.**

Do not:

- use old Phi/Gemma outputs as labels;
- use them to train or tune the pipeline;
- copy their decisions into GT196;
- run Stage 05 from those decisions;
- describe this lane as the current validated adjudicator.

The code can remain for regression/error analysis while the primary annotation
lane is redesigned against GT196.

## 5. Historical Stage 06 — MiniCheck + selective Phi/Gemma jury

`python/stages/06_review_pride_scp_catalogue.py` is a different QC system from
the recall semantic-QC lane above. It reviews **atomic claims in an already
curated Stage-05 catalogue**.

The historical v3.2 policy was:

```text
Bespoke-MiniCheck supports claim + no deterministic risk
        -> PASS

MiniCheck does not support claim OR deterministic high-risk context
        -> Phi-4-mini + Gemma 3 4B structured adjudication

FAIL
        -> requires strong cited contradiction logic
```

It is read-only and never rewrites catalogue CSVs or Stage-04 JSONs. It is
useful historical infrastructure, but it is **not currently the path for
producing GT-quality accession decisions**. It should only be reconsidered
after the primary discovery/evidence/annotation lane is demonstrably calibrated
against the frozen reference.

## 6. Model-use rules going forward

1. **Discovery recall is deterministic.** No model is allowed to rescue or
   suppress candidates by GT lookup.
2. **Primary evidence first.** Repository metadata, SDRF/file names and exact
   publication passages are authoritative inputs; model labels are derived
   interpretations.
3. **Separate factual axes from top-level decisions.** In particular, sample
   unit, pooling, identity preservation, benchmark status and reanalysis status
   should be scored independently before deriving include/exclude.
4. **Uncertainty preserves recall.** Missing evidence should route to review,
   not automatic exclusion.
5. **No old QC labels as truth.** v0.1.8-v0.1.12 Phi/Gemma decisions remain
   quarantined.
6. **Evaluate every iteration against the same frozen GT.** Report concrete
   accession changes, not only aggregate metrics.
7. **Keep accession and canonical-study decisions separate.** A model should
   not collapse companion/mirror accessions merely because they share a paper.
