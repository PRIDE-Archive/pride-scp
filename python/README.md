# Python semantic / curation layer

The Python side consumes the recall-first Rust candidate universe. Its current
job is **evidence enrichment and structured interpretation without candidate
loss**. The GT196 optimization phase has changed the trust boundary: old final
catalogue/QC scripts remain available, but the recall-first branch should not
be pushed into Stage 05 until the primary semantic annotations are validated
against the frozen reference.

See also:

- `../docs/architecture.md`
- `../docs/model_roles.md`

## Imported stage scripts

`python/stages/` contains the current imported legacy-stage implementations:

- `pride_scp_pipeline_common.py`
- `01_fetch_pride_publications.py`
- `02_download_publication_pdfs.py`
- `03_resolve_publication_content.py`
- `03_screen_pride_scp_publications.py` — diagnostic only in recall-first mode
- `04_run_pride_scp_annotations.py`
- `05_merge_pride_scp_catalogue.py`
- `06_review_pride_scp_catalogue.py`
- `pride_scp_targeted_ollama.py`

`python/STAGE_SOURCES.sha256` records the imported source state.

## Current recall-first flow

```text
Rust semantic_candidates.jsonl
        |
        v
Stage 01 publication mapping
        |
        v
Stage 02 PDF resolution
        |
        v
Stage 03 content resolution
        |
        v
partition by usable publication content
       / \
      /   \
     v     v
Stage 04   repository-only
Qwen v18   Qwen triage
     \     /
      \   /
       v v
 deterministic semantic unifier
        |
        v
 GT-driven annotation/review frontier
```

The old Stage-03 *screen* must not gate Stage 04. Use
`--all-valid-content` (or the legacy `--all-valid-pdfs` alias when appropriate)
so all candidates with usable publication content can be annotated.

Repository-only candidates remain in scope even when no manuscript can be
resolved.

## Prepare the semantic bridge

```bash
scripts/prepare_semantic_bridge.sh
```

This runs Rust `candidate-audit` + `export-python` and writes:

```text
data/candidate_audit/candidate_diagnostics.tsv
data/candidate_audit/candidate_diagnostics.jsonl
data/python_bridge/candidate_accessions.txt
data/python_bridge/candidate_manifest.tsv
data/python_bridge/semantic_candidates.jsonl
```

Semantic priorities are non-destructive:

```text
A_specific
B_method
C_broad
D_adjacent
```

## Publication/content enrichment

```bash
scripts/run_python_publication_enrichment.sh
```

The helper performs publication mapping, PDF resolution, full-text fallback,
manual-manuscript queue generation and semantic partitioning.

It writes:

```text
work/python/semantic_partition/publication_backed_candidates.jsonl
work/python/semantic_partition/repository_only_candidates.jsonl
```

No candidate is removed because a publication/PDF is absent.

## Publication-backed Stage 04 — active, under GT196 re-benchmarking

Recommended recall-first invocation:

```bash
python python/stages/04_run_pride_scp_annotations.py \
  work/python/pride_candidate_publications_with_content.tsv \
  --targeted-script python/stages/pride_scp_targeted_ollama.py \
  --output-dir work/python/pride_scp_annotations \
  --model qwen2.5:3b \
  --cpu-threads 4 \
  --workers 1 \
  --all-valid-content
```

The targeted v18 annotator performs deterministic evidence retrieval and up to
five small Qwen extraction calls for:

1. single-cell samples / low-input benchmarks / model SCP opinion;
2. sample preparation and cell isolation;
3. LC configuration/gradient;
4. genuine single-cell performance;
5. low-input performance.

Deterministic validation and the v17 source-evidence gate can override Qwen's
top-level classification. Nevertheless, the historical GT196 baseline showed
only 36/64 publication-backed recovered GT positives called `yes`, so Stage 04
is **not yet trusted as final accession truth**.

## Repository-only triage — active and non-destructive

```bash
python python/recall/triage_repository_candidates.py \
  work/python/semantic_partition/repository_only_candidates.jsonl \
  --output-dir work/python/repository_triage \
  --model qwen2.5:3b \
  --cpu-threads 4
```

The model sees repository evidence only and returns a triage class plus
structured individual-cell and MS/proteomics evidence. Missing evidence must
not become automatic exclusion.

## Deterministic semantic unification — active

```bash
scripts/unify_semantic_results.sh
```

The unifier combines discovery, Stage-04 and repository-only evidence into a
lossless manifest and review routes. It intentionally preserves cases where a
high-recall discovery signal conflicts with a negative model/gate decision.

Primary output:

```text
work/python/semantic_unification/unified_semantic_manifest.tsv
```

The unifier is currently the safe end of the automated recall-first path.

## Recall semantic QC — QUARANTINED

These files implement the v0.1.8-v0.1.12 Phi/Gemma semantic-QC experiment:

```text
python/recall/build_semantic_qc_packets.py
python/recall/adjudicate_semantic_qc.py
scripts/run_semantic_qc.sh
```

Defaults:

```text
critic: phi4-mini:3.8b
jury:   gemma3:4b
```

Their factual-axis design remains useful for research, but the historical
affirmative/final decisions are **not approved labels** and must not be used to
feed Stage 05. They are retained only for error analysis/regression work until
a GT196-calibrated replacement is demonstrated.

Do not copy an old semantic-QC `review_decisions.tsv` into the Stage-05 bridge.

## Stage 05 bridge / catalogue — historical until revalidated

`build_stage05_bridge.sh` and `05_merge_pride_scp_catalogue.py` remain in the
repository for compatibility with the older pipeline. During the current
GT196 optimization phase, do not use the quarantined semantic-QC decisions to
unlock the bridge.

The old v19.1 catalogue is a historical baseline, not the current recall-first
catalogue.

## Stage 06 — separate historical read-only claim QC

`06_review_pride_scp_catalogue.py` is **not the same thing** as the quarantined
recall semantic-QC lane.

Historical v3.2 defaults:

```text
fact checker: bespoke-minicheck
critic:       phi4-mini:3.8b
jury:         gemma3:4b
```

It reviews atomic claims in an already-built Stage-05 catalogue and is
read-only. Easy MiniCheck-supported claims pass directly; Phi/Gemma are used
selectively for non-support/high-risk cases.

This remains useful historical infrastructure, but it should be reconsidered
only after the primary GT196-calibrated annotation lane is sound.

## Current model policy

- Qwen is a bounded evidence extractor/interpreter, not benchmark truth.
- Missing manuscript evidence becomes repository-based review/uncertainty, not
  exclusion.
- The v0.1.8-v0.1.12 Phi/Gemma decisions are quarantined.
- Stage-06 MiniCheck/Phi/Gemma is a separate read-only historical claim audit.
- GT196 is evaluation-only and never passed into model prompts.
