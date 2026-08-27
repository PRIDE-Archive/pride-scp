# Python semantic / curation layer

`python/stages/` is populated from the **current local pipeline**, not from a
stale copy embedded in this repository archive.

Run:

```bash
scripts/import_current_python.sh \
  ~/Documents/PRIDE_SCP/pride_scp_catalogue_pipeline
```

This copies the current versions of:

- `pride_scp_pipeline_common.py`
- `01_fetch_pride_publications.py`
- `02_download_publication_pdfs.py`
- Stage-03 publication screen (diagnostic only in the new architecture)
- `04_run_pride_scp_annotations.py`
- `05_merge_pride_scp_catalogue.py`
- `06_review_pride_scp_catalogue.py`
- `pride_scp_targeted_ollama.py`

and writes `python/STAGE_SOURCES.sha256` so the imported state is explicit.

## Important architecture change

Stage 03 is no longer a gate. The annotation runner should use
`--all-valid-pdfs` for the recall-first candidate set.

Repository-only candidates are retained even when no PDF can be resolved.
`python/recall/triage_repository_candidates.py` can prioritize those cases
using only the Rust evidence bundle; it does not mutate or reject candidates.

## v0.1.4 unified semantic evidence modes

`pride-scp export-python` now writes `semantic_candidates.jsonl`, including
specific/method/broad/adjacent discovery labels and source-hit excerpts.

After Stage 01/02 publication enrichment:

```bash
python python/recall/partition_semantic_candidates.py \
  data/python_bridge/semantic_candidates.jsonl \
  --pdf-manifest work/python/pride_candidate_publications_with_pdfs.tsv \
  --output-dir work/python/semantic_partition
```

This produces publication-backed and repository-only candidate JSONL files.
No candidate is removed because it lacks a publication or usable PDF.

Use the current Stage 04 with `--all-valid-pdfs` for publication-backed
candidates, and use `triage_repository_candidates.py` on the repository-only
JSONL as non-destructive semantic evidence. A later reconciliation stage can
merge both evidence modes before deterministic curation.

## v0.1.8 semantic unification

After publication-backed Stage 04 and repository-only triage both finish, run:

```bash
scripts/unify_semantic_results.sh
```

This produces a lossless 321-row semantic manifest and a secondary-review
queue. The routing layer intentionally distrusts unsupported
`possible_true_scp` calls when the repository triage's own structured fields
report absent/contradictory individual-cell evidence or absent MS/proteomics
evidence. It also preserves `A_specific` candidates when Stage 04 is negative,
because historical genuine SCP datasets occur in that conflict pattern.

Do not run final Stage 05 directly from the two raw semantic lanes. After the
secondary-review decisions have been filled, use:

```bash
scripts/build_stage05_bridge.sh
```

The bridge is compatible with the imported current Stage-05 script and keeps
repository-only records in scope without inventing publication metadata.
### Independent semantic QC

The deterministic unifier also writes `qc_candidate_queue.jsonl`, containing
provisional includes plus all review routes. Run:

```bash
scripts/run_semantic_qc.sh
```

The default critic is `phi4-mini:3.8b`; the default selective jury is
`gemma3:4b`. Model phases are separated by an explicit Ollama unload to avoid
the multi-model memory pressure seen in earlier Stage-06 development. The
reviewer uses only the unified evidence packet and never web/outside knowledge.
It writes `include`, `exclude`, or `uncertain`; uncertain decisions intentionally
block the final Stage-05 bridge.

