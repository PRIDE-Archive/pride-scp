# Residual73 evidence-escalation harness

`pride-scp-sdrf-evidence-escalation-v0.1` turns the historical rescue phases into one bounded controller for unresolved production accessions.

## Why

The pipeline already contains strong individual components:

- PRIDE project/publication metadata retrieval;
- PDF + Europe PMC full-text materialization;
- external publication recovery;
- supplementary/external-analysis evidence acquisition;
- constrained Qwen semantic extraction;
- Rust source-grounded SDRF annotation;
- deterministic post-run triage/readiness.

The missing behavior was control flow: a failed accession did not automatically say *which evidence class was missing* and escalate only that evidence before another annotation pass.

## Policy

The harness is **not an autonomous SDRF-writing web agent**.

It follows this order:

1. inspect readiness state and local evidence;
2. classify an evidence lane;
3. use already-local publication/full-text evidence when sufficient;
4. only if warranted, refresh public PRIDE/publication evidence;
5. recover open full text and supplementary links where possible;
6. for mapping lanes, acquire structured support/external-analysis assets;
7. run the small model only as a semantic reader over provenance-labelled publication evidence;
8. rerun `pride-scp sdrf-annotate`;
9. deterministically triage the result;
10. fail closed if evidence remains insufficient.

The model cannot create RAW/sample/channel mappings. Filenames do not create biological identity. GT is not runtime truth.

## Lanes

- `candidate_missing`: refresh publication identity/full text and annotation evidence.
- `required_metadata`: use publication evidence + small-LLM semantic reading.
- `mapping`: publication + supplementary/support assets; model may describe roles but cannot create row mappings.
- `archive_mapping`: repository/support-asset recovery.
- `semantic_conflict`: retrieve more source text and semantic evidence.
- `publication_missing`: refresh identifiers/open full text.
- `validator_compatibility`: deterministic lane; internet/model suppressed.
- `closed`: already `needs_independent_review` or `submission_ready`.

## Online sources

The harness reuses repository-owned stages rather than adding a new scraper:

- `01_fetch_pride_publications.py`
- `02_download_publication_pdfs.py`
- `03_resolve_publication_content.py`
- `sdrf_residual_external_publication_recovery.py`
- `sdrf_generalized_evidence_graph.py`

These already implement bounded PRIDE, Europe PMC/PMC, Crossref/Unpaywall and publication-content recovery behavior.

`--online-mode auto` is recommended. Network failures are recorded and the accession remains unresolved; they never authorize fallback inference.

## Small-LLM policy

The harness invokes the existing targeted publication annotator with `qwen2.5:3b` by default. It receives materialized publication content, not arbitrary internet pages. The existing targeted annotator and Rust generator retain their provenance/evidence-ref checks.

This is preferable to giving a 3B model unrestricted browsing: retrieval remains deterministic and auditable, while the model is used where it is strongest—semantic interpretation of a small evidence packet.

## First use on the Residual73

Start with **plan-only** mode. This is cheap and does not call the network or model:

```bash
python scripts/sdrf_evidence_escalation_harness.py \
  --accessions-file work/residual73_v1/accessions.txt \
  --readiness-tsv work/residual73_v1/residual85_baseline_readiness.tsv \
  --snapshot data/snapshot \
  --publication-manifest work/python/pride_candidate_publications_with_content.tsv \
  --resolved-sdrf-dir data/sdrf_source_resolution_gt105_pride_v024/resolved \
  --output work/residual73_evidence_escalation_v1 \
  --online-mode auto
```

Inspect:

```text
work/residual73_evidence_escalation_v1/evidence_escalation_plan.tsv
work/residual73_evidence_escalation_v1/evidence_escalation_summary.json
work/residual73_evidence_escalation_v1/online_accessions.txt
work/residual73_evidence_escalation_v1/mapping_accessions.txt
work/residual73_evidence_escalation_v1/deterministic_only_accessions.txt
```

Then execute the bounded escalation:

```bash
python scripts/sdrf_evidence_escalation_harness.py \
  --accessions-file work/residual73_v1/accessions.txt \
  --readiness-tsv work/residual73_v1/residual85_baseline_readiness.tsv \
  --snapshot data/snapshot \
  --publication-manifest work/python/pride_candidate_publications_with_content.tsv \
  --resolved-sdrf-dir data/sdrf_source_resolution_gt105_pride_v024/resolved \
  --output work/residual73_evidence_escalation_v1 \
  --online-mode auto \
  --model qwen2.5:3b \
  --cpu-threads 4 \
  --execute
```

The controller writes every invoked command to `command_plan.json` and keeps stage logs. It is restart-friendly because the underlying publication and LLM components are already cache-aware.

## Expected benefit

The harness should substantially reduce manual iteration for:

- missing manuscript/full-text evidence;
- missing required metadata that is explicit in Methods;
- sample-role/cell-line/organism/isolation/chemistry ambiguity;
- accessions whose publication or supplement was not locally materialized.

It will not automatically solve genuinely missing sample/file/channel maps. Those are intentionally surfaced as structured `mapping` evidence cases rather than being guessed.

## Recommended cohort split after the current PR-ready 12

The frozen Residual85 accounting now leaves 73 unresolved. Two are already deliberately parked fail-closed:

- `PXD043473`: exact reporter-channel mapping absent;
- `PXD061710`: exact KPC cell-line identity absent.

For the first harness run, keep those two out of automatic reconstruction and run the active 71. They may be revisited later only if the evidence-escalation retrieval finds new accession-specific source evidence.

The frozen baseline lanes remaining after the 12 PR-ready promotions are approximately:

- 17 metadata-incomplete;
- 10 BigBio/ontology/tooling blockers;
- 4 parse/content blockers;
- 5 divergent trusted candidates;
- 35 no-source-closed candidates;
- 2 parked evidence-insufficient cases.

This is why plan-only mode should be run first: it should send validator/tooling cases to a deterministic lane and concentrate network/LLM work on the publication/evidence-limited cases.
