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
