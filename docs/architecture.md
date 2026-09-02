# PRIDE_SCP architecture — GT196 optimization state

**Status:** discovery Iteration 1 accepted against frozen GT196; discovery
Iteration 2 (ProteomeCentral registry/native-accession supplementation) is
implemented and awaiting the frozen acceptance rerun. Annotation redesign is
the next major phase after that check.

PRIDE_SCP is a hybrid Rust + Python pipeline. Rust owns deterministic,
high-volume repository enumeration and recall-first discovery. Python owns
publication/source enrichment, bounded semantic extraction, deterministic
reconciliation, catalogue construction, and historical QC utilities.

The central architectural invariant is:

> **Candidate discovery maximizes recall; semantic stages may prioritize and
> annotate candidates but must not silently shrink the recall universe.**

GT196 is an external held reference. It is used by evaluation commands only
and is never a production lookup table.

## Current end-to-end flow

```text
                         PRODUCTION DATA FLOW

        PRIDE public project universe          ProteomeCentral / PROXI registry
                    |                                  |
                    v                                  v
        +-------------------------+        +-------------------------+
        | Rust PRIDE snapshot     |        | Rust registry snapshot  |
        | project JSON            |        | PXD aliases             |
        | file manifests          |        | native accessions       |
        | SDRF when available     |        | hosting provenance      |
        +-------------------------+        +-------------------------+
                    |                                  |
                    +---------------+------------------+
                                    v
                  +-------------------------+
                  | Rust discovery UNION    |
                  | primary PRIDE evidence  |
                  | registry-only PXD       |
                  | supplements absent from |
                  | the PRIDE snapshot      |
                  | SCP/method/unit signals |
                  +-------------------------+
                              |
                              | all positive-signal candidates retained
                              v
                  +-------------------------+
                  | candidate-audit         |
                  | A_specific / B_method   |
                  | C_broad / D_adjacent    |
                  +-------------------------+
                              |
                              v
                  +-------------------------+
                  | export-python bridge    |
                  | TSV + JSONL evidence    |
                  +-------------------------+
                              |
                              v
                    publication enrichment
                  Stage 01 -> Stage 02 ->
                  Stage 03 content resolver
                              |
                         partition by
                      usable source content
                       /                 \
                      /                   \
                     v                     v
        publication-backed lane      repository-only lane
        Stage 04 Qwen v18            Qwen repository triage
        + deterministic gate         (non-destructive)
                     \                   /
                      \                 /
                       v               v
                  +-------------------------+
                  | deterministic semantic  |
                  | unification             |
                  | no candidate deletion   |
                  +-------------------------+
                              |
                              v
                    GT-driven annotation
                       redesign / review
                      **CURRENT FRONTIER**
                              |
                    (only after validation)
                              v
                  Stage-05 deterministic catalogue
                              |
                              v
                  Stage-06 read-only claim QC
                  (historical infrastructure)
```

The old Stage-03 publication screen is **diagnostic only**. It must not gate
Stage 04 in the recall-first architecture.

## External evaluation loop

```text
                         EVALUATION ONLY

frozen GT196 master -------------------------------+
                                                   |
PRIDE + registry snapshot -> discover -> candidates +--> recall-audit
                                                   |
                                                   +--> exact accession deltas
```

The benchmark master is read only after candidates have been generated.
Production commands do not consult GT accessions.

Current frozen benchmark location:

```text
gpt/final_curation_20260831/
```

Treat that directory as immutable and do not stage it in Git.

## Accepted discovery state

### Frozen pre-change baseline

```text
PRIDE GT positives:     106
Rust candidates:        321
GT recovered:            97
GT missed:                9
PRIDE discovery recall: 91.51%
```

### GT196 discovery Iteration 1 — accepted 1 September 2026

Iteration 1 added measured vocabulary/regex coverage for single muscle
fibre/fiber, myofibre/myofiber and bounded MALDI/MSI + single-cell contexts.
The real frozen-snapshot rerun produced:

```text
projects scanned:        40,364
Rust candidates:            334
strong / possible / weak:   191 / 68 / 75
A / B / C / D priority:     141 / 33 / 154 / 6
GT recovered:               105 / 106
GT missed:                    1
PRIDE discovery recall:      99.06%
```

Candidate growth was 321 -> 334 (+13, +4.0%) with no baseline candidate loss.
The 13 additions were:

```text
PXD006182  GT recovery
PXD010489  GT recovery
PXD017755  GT recovery
PXD028435  GT recovery
PXD036010  non-GT review candidate
PXD045629  non-GT review candidate
PXD045631  non-GT review / proposed GT-correction candidate
PXD046863  GT recovery
PXD050980  GT recovery
PXD053022  GT recovery from repository_metadata
PXD056528  GT recovery
PXD064789  non-GT review candidate
PXD066393  non-GT review candidate
```

The sole frozen-GT miss is `PXD047101`. The baseline/source audit classified
this as a cross-repository/native-accession problem rather than another PRIDE
vocabulary miss. That class should be addressed by registry/native-accession
normalization, not an accession-specific exception.

### GT196 discovery Iteration 2 — implemented, acceptance pending

Iteration 2 adds `registry-snapshot`, which enumerates ProteomeCentral's PROXI
dataset registry, extracts PXD identifiers plus native repository aliases, and
materializes normalized records under `snapshot/registry/`. The production
discovery rule is intentionally narrow:

> A registry record is scanned only when its PXD alias is absent from the
> primary `snapshot/projects/` directory.

This prevents duplicate ProteomeCentral metadata from changing the accepted
score/tier of existing PRIDE candidates. It directly addresses the measured
`PXD047101` class, where the PXD is secondary to MassIVE `MSV000093434`.

Acceptance requires the real frozen benchmark to show:

- `PXD047101` recovered;
- all 334 Iteration-1 candidates preserved;
- no broad candidate explosion from registry-only aliases;
- ideally 106/106 PRIDE-labelled GT recovery.

## Layer 1 — Rust snapshot/index

`pride-scp-index` performs bounded, resumable repository acquisition and
materializes local evidence so discovery can be rerun without repeatedly
querying remote services.

The primary PRIDE snapshot includes:

- project catalogue and project metadata JSON;
- file manifests;
- SDRF where available;
- per-accession errors without aborting the whole crawl;
- cached catalogue pages and materialized project records.

Iteration 2 adds a separate ProteomeCentral registry cache:

- raw PROXI dataset pages;
- normalized `registry/projects/PXD....json` records;
- `registry_accessions.tsv` mapping PXD aliases to native repository accessions;
- inferred hosting-repository provenance where exposed by registry metadata;
- a registry summary distinguishing PXD aliases already present in PRIDE from
  true supplemental aliases.

A project-catalogue enumeration failure is treated differently from an
individual evidence failure: silently truncating the accession universe is a
recall error and must stop the run.

## Layer 2 — Rust recall-first discovery

`pride-scp-discovery` scans all cached primary PRIDE projects plus only those
registry PXD aliases missing from the primary snapshot, then unions independent
positive-signal lanes. Signals include:

- explicit SCP terminology;
- known SCP workflow/method names;
- biological-unit language;
- primary repository title/description/metadata;
- supplemental ProteomeCentral title/description/native-alias metadata for PXD
  aliases absent from PRIDE;
- filenames/file-manifest text;
- SDRF text;
- proximity-constrained cell/fibre + proteomics/MS patterns;
- bounded MALDI/MSI + single-cell-context patterns.

Negative/adjacent phrases are recorded as evidence but are not hard exclusions
because a true SCP accession may also contain carrier, pooled-control,
dilution, bulk-benchmark, transcriptomic or spatial-comparison experiments.

Discovery scores/tier are **prioritization values, not probabilities**.

## Layer 3 — candidate diagnostics and bridge

`candidate-audit` maps raw discovery hits into semantic profiles:

- `A_specific` — direct/specific SCP evidence;
- `B_method` — method-level evidence;
- `C_broad` — broad context retained for recall;
- `D_adjacent` — adjacent evidence retained for review.

`export-python` preserves full hit excerpts in TSV/JSONL and never removes a
candidate because it is weak or lacks a publication.

## Layer 4 — publication/source enrichment

`scripts/run_python_publication_enrichment.sh` currently performs:

1. Stage 01 publication/accession mapping;
2. Stage 02 validated PDF resolution/reuse/manual lookup;
3. Stage 03 normalized publication-content resolution (PDF, Europe-PMC full
   text and supported fallbacks);
4. manuscript/manual-review queue generation;
5. partition into publication-backed and repository-only evidence modes.

A missing manuscript is an **evidence mode**, not an exclusion reason.

The legacy publication-screen script can still generate diagnostics, but its
screen decision must not gate the recall-first candidate universe.

## Layer 5A — publication-backed Stage 04

The targeted annotator (`pride_scp_targeted_ollama.py`, current imported v18)
uses deterministic retrieval followed by up to five compact `qwen2.5:3b`
structured extraction calls:

1. genuine single-cell samples / low-input benchmarks / model SCP opinion;
2. preparation and individual-cell isolation;
3. LC configuration and gradient;
4. genuine single-cell throughput/proteome depth;
5. low-input benchmark performance.

After extraction, deterministic code normalizes fields and applies the v17
source-evidence gate. The gate can override Qwen's top-level SCP opinion.

This lane is **active but not yet GT-quality final classification**. The
historical baseline found only 36/64 publication-backed recovered GT positives
called `yes` by Stage 04, so annotation/gating is the next major optimization
problem.

## Layer 5B — repository-only Qwen triage

`python/recall/triage_repository_candidates.py` uses `qwen2.5:3b` over the
repository evidence packet only. It returns structured individual-cell and
MS/proteomics evidence plus a triage class.

This step is explicitly non-destructive. Sparse repository evidence should
produce `possible`/`uncertain` rather than an invented negative decision.

## Layer 6 — deterministic semantic unification

`python/recall/unify_semantic_results.py` merges the two semantic evidence
modes with the Rust discovery profile. It detects conflicts such as:

- specific discovery evidence vs Stage-04 negative classification;
- repository Qwen `possible_true_scp` despite absent/contradictory structured
  cell/MS evidence;
- broad/adjacent discovery evidence with optimistic model calls.

It assigns review routes but preserves every candidate. In the frozen
pre-Iteration-1 baseline it retained all 97 GT positives that entered Rust
discovery even though Stage 04 itself was much less sensitive.

## Layer 7 — current frontier: GT-driven annotation redesign

The next objective is to make source-grounded annotation reproduce GT-quality
factual axes for matched accessions, especially:

- `reference_decision`;
- `biological_sample_unit`;
- genuine one-cell target MS samples present;
- cells per target MS sample;
- identity preserved to MS;
- destructive pooling before identity-preserving labelling;
- benchmark-only status;
- adjacent single-cell-only status;
- reanalysis-only status;
- mixed designs;
- source/evidence provenance;
- native/alias accession handling;
- canonical study-family identity.

The design priority is deterministic/source-structured evidence first and small
LLMs for bounded interpretation, not unconstrained yes/no voting.

## Quarantined recall semantic QC

The v0.1.8-v0.1.12 Phi/Gemma recall semantic-QC path is retained in source for
historical regression/error analysis but its affirmative/final decisions are
**quarantined**.

Do not run Stage 05 from those decisions. Do not use them as labels. Do not
modify GT196 from them.

See `docs/model_roles.md` for the exact distinction between this quarantined
lane and the older Stage-06 claim-QC implementation.

## Historical Stage 05 and Stage 06

`05_merge_pride_scp_catalogue.py` is the deterministic catalogue merger from
the older pipeline. `06_review_pride_scp_catalogue.py` is a read-only claim-QC
layer using MiniCheck and selective Phi/Gemma adjudication.

These remain useful infrastructure and historical outputs, but **the current
recall-first branch must not be pushed through them until the primary semantic
annotations are validated against GT196**.

The older v19.1 final catalogue is therefore a historical comparison point,
not the current recall-first output.

## Architecture invariants

1. Missing publication/PDF never removes a repository candidate.
2. Stage-03 screening is diagnostic only.
3. Negative/adjacent language annotates risk; it does not hard-reject during
   discovery.
4. Candidate recall is benchmarked before precision is optimized.
5. Rust/Python communicate through explicit TSV/JSONL contracts, not FFI.
6. GT196 is evaluation-only and immutable during optimization.
7. Never hard-code GT accessions into production discovery logic.
8. Keep accession-level and canonical-study-level decisions separate.
9. Old v0.1.8-v0.1.12 Phi/Gemma decisions are quarantined.
10. ProteomeCentral is supplemental: duplicate PXD records never rescore the
    primary PRIDE project.
11. Every code iteration reports the exact accessions gained/lost, not only a
    summary metric.

## Near-term roadmap

1. **Accept Discovery Iteration 2** on the real ProteomeCentral registry cache;
   target 106/106 while preserving all 334 Iteration-1 candidates.
2. Freeze the resulting candidate universe and rerun stage-by-stage GT crosswalks.
3. Redesign the primary biological-unit/pooling annotation lane against GT196.
4. Measure per-field annotation agreement and source provenance.
5. Only after the primary lane is sound, reconsider an independent QC model.
6. Add explicit canonical-family materialization while retaining all
   accession-level records.

## Related documentation

- `docs/model_roles.md` — exact responsibilities and status of each model.
- `docs/data_contracts.md` — Rust/Python interchange formats.
- `python/README.md` — Python semantic/candidate-evidence workflow.
- `python/STAGE06_V32_README.md` — historical Stage-06 v3.2 claim-QC details.
- `CHANGELOG.md` — implementation history and GT196 optimization changes.
