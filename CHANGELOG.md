# Changelog

## 0.1.11 - 2026-08-27

- Make the target MS sample unit explicit: one cell, multiple cells, mixed design, or unclear.
- Distinguish individual cells being present upstream from one biological cell contributing to each target proteomic/MS sample.
- Explicitly interpret statements such as `10^6` / extracted `106` cells per replicate followed by protein extraction from each sample as population proteomics.
- Give many-cell target-sample composition, destructive pooling, and population-only evidence deterministic exclusion precedence over optimistic model labels.
- Treat `benchmark_only=yes` as internally inconsistent when a complete genuine one-cell target-MS chain is also established; separate multi-cell libraries and low-input benchmarks no longer negate those samples.
- Add evidence-aware critic/jury arbitration: hard sample-unit exclusions win, while complete one-cell chains can override soft benchmark/control confusion.
- Preserve v0.1.10 structured-output retry/repair and selective jury behavior.
- Bump QC cache version so v0.1.10 smoke results are rerun automatically.
- Add regression coverage for the PXD028991 many-cell-per-replicate failure mode and PXD049412 mixed benchmark + genuine single-cell design.

## 0.1.10 - 2026-08-27

- Fix the v0.1.9 smoke-test positive-calibration failure while preserving correct population/bulk exclusions.
- Replace the ambiguous `same_unit_ms_proteomics` axis with an explicit individual-cell-to-MS evidence chain: genuine single-cell samples present, individual identity preserved, MS on individual-cell-derived samples, destructive pooling before identity, population/bulk-only status, benchmark-only status, and mixed control/library presence.
- Explicitly allow identity-preserving multiplexing and separate multi-cell libraries/carriers/controls without treating them as destructive pooling.
- Clarify that FACS itself is not pooling; one-cell-per-well sorting is compatible with SCP, whereas many cells contributing to one proteomic sample is not.
- Instruct critic/jury to infer continuity across methods passages instead of demanding one redundant sentence saying the same cell was measured by MS.
- Add separate critic/jury output-token budgets (520/800 by default).
- Add structured-output corrective retry with additional output budget after malformed/truncated JSON, plus tolerant parsing of wrappers/code fences.
- Bump QC cache version so v0.1.9 smoke results are automatically invalidated.
- Expand regression coverage for genuine egg/single-cell chains, mixed controls, identity-preserving multiplexing, destructive pooling, cache invalidation, and malformed-JSON retry.

## 0.1.9 - 2026-08-27

- Fix the v0.1.8 independent semantic-QC collapse in which 200/219 critic decisions and 201/219 final decisions were `uncertain`.
- Rehydrate exact Stage-04 `samples`, `preparation`, and performance evidence passages plus raw samples-task output before independent QC.
- Preserve repository title/description/discovery excerpts as primary source evidence for repository-only cases.
- Replace one opaque decision with factual axes: sample unit, same-unit MS/proteomics, target-dataset scope, pre-measurement pooling, and benchmark-only status.
- Derive normalized include/exclude/uncertain decisions deterministically from those factual axes, preventing over-cautious or optimistic top-level labels from dominating.
- Add population/pooling/benchmark risk flags, including many-cell count contexts such as `10^6 ... cells`, as retrieval hints rather than hard exclusions.
- Version evidence packets and critic/jury caches; v0.1.8 uncertain/blank cache records are invalidated automatically and rerun without `--force`.
- Stop sending every critic-uncertain candidate to Gemma; jury selection is now conflict/evidence driven.
- Add regression coverage for direct individual-cell evidence, many-cell population evidence, cache invalidation, and selective-jury behavior.

## 0.1.8 - 2026-08-27

- Add a lossless semantic-unification layer across the 219 publication-backed and 102 repository-only candidates.
- Detect repository-Qwen overcalls by comparing `possible_true_scp` against its own structured individual-cell/MS evidence fields.
- Route candidates into `include_candidate`, `review_high`, `review_medium`, `review_low`, and `likely_non_scp` without deleting any accession.
- Preserve high-recall conflicts such as `A_specific` discovery evidence versus a negative Stage-04 gate for secondary adjudication rather than silently excluding them.
- Emit a 321-row unified semantic manifest, route-specific TSVs, a secondary-review JSONL/TSV queue, and an explicit review-decision template.
- Add a guarded Stage-05 bridge generator. Final bridge generation refuses unresolved review candidates; `--allow-provisional` exists only for structural smoke tests.
- Synthesize metadata-sparse Stage-05-compatible annotations for repository-only candidates after explicit adjudication, while retaining the unified manifest as provenance authority.
- Add offline regression coverage for routing, repository overcall detection, review gating, and Stage-05 bridge generation.
- Add optional independent semantic QC with `phi4-mini:3.8b` critic and selective `gemma3:4b` jury, resumable per accession and explicitly unloaded between model phases.
- Emit a QC decision file that can override provisional includes/reviews; critic/jury disagreement remains uncertain and blocks the final bridge.

## 0.1.7 - 2026-08-26

- Add generic publication-content resolution after PDF resolution: validated PDF, Europe PMC JATS full-text XML, then PMC article HTML.
- Normalize XML/HTML full text deterministically into paragraph text for the existing targeted semantic annotator.
- Extend Stage 04 and the targeted annotator to accept normalized full-text artifacts via `--source-text` / `--all-valid-content`.
- Partition candidates by usable publication content rather than PDF availability alone; candidate loss remains forbidden.
- Add `write_missing_manuscript_queue.py` and `scripts/write_missing_manuscripts.sh`.
- Emit separate missing-PDF and priority-manual-manuscript PXD accession lists, publication queues, and a PXD-to-PDF manifest template.
- Extend manual PDF mappings to support PMCID and one shared PDF linked to multiple PXD accessions.
- Preserve the v0.1.7 publication-content front end from accidental overwrite by `import_current_python.sh`.

## 0.1.6 - 2026-08-26

- Add NCBI PMC ID Converter as the primary DOI/PMID/PMCID resolver for publications represented in PMC.
- Bypass the environment-specific Europe PMC DOI-search failure seen in the v0.1.5 live smoke test.
- Populate PMCID/PMID from DOI before attempting PMC PDF render URLs.
- Keep Europe PMC as a secondary metadata/title resolver for publications outside the ID-converter path.
- Bump PDF resolution cache schema to v3 so v0.1.5 unresolved results are retried automatically.
- Preserve manual/legacy PDF override semantics and structured unresolved diagnostics.


## 0.1.5 - 2026-08-26

- Harden Europe PMC DOI/PMID/title resolution and force JSON metadata responses even when the PDF downloader session prefers PDF content.
- Recover PMID/PMCID metadata during Stage 01/02 and try official PMC/Europe-PMC render URLs whenever a PMCID is known; `hasPDF == Y` is no longer required.
- Bump the Stage-02 resolution cache schema so the broken v0.1.4 `no_open_access_pdf` cache is retried automatically.
- Add validated PDF reuse from legacy directories and a Git-ignored `manual_pdfs/` fallback with optional TSV mapping.
- Add `manual_pdf_queue.tsv` with article URLs, suggested filenames, resolver diagnostics, and recovered identifiers for genuinely unresolved publications.
- Add structured `pdf_resolution_trace` and non-empty diagnostic errors for unresolved rows.
- Preserve repository-only candidates; manual/PDF recovery changes evidence mode but never drops a candidate.
- Protect the repository-owned v0.1.5 resolver files from accidental overwrite by later `import_current_python.sh` runs.
- Add offline PDF-resolver regression tests.

## 0.1.4 - 2026-08-26

- Add `candidate-audit` to separate specific SCP, method, broad-context, adjacent, and negative discovery signals without filtering candidates.
- Add semantic priorities (`A_specific`, `B_method`, `C_broad`, `D_adjacent`) and broad-only diagnostics.
- Expand `export-python` to preserve full discovery hit excerpts and emit `semantic_candidates.jsonl` plus a richer candidate manifest.
- Add publication-backed vs repository-only semantic partitioning; candidates without usable PDFs are retained rather than lost.
- Add `compact-snapshot` with validation and dry-run support to remove redundant `project_pages/` cache while retaining the materialized 40,364-project snapshot.
- Add optional candidate-only file/SDRF pruning for later use; full evidence retention remains recommended during discovery development.

## 0.1.3 - 2026-08-26

- Fix full-catalogue enumeration against the live PRIDE v3 `/projects/all` behavior, which can return the complete ~40k-project catalogue despite `page`/`pageSize` parameters.
- Detect monolithic catalogue responses and terminate after the first complete payload.
- Add `--max-stagnant-pages` as an independent duplicate-only pagination safety stop.
- Seed per-project repository metadata directly from the catalogue response, avoiding tens of thousands of redundant project-detail requests.
- Add bounded parallel project/file/SDRF fetching with independent `--concurrency` and `--request-concurrency` controls.
- Bound the number of in-flight accession tasks instead of accumulating the whole catalogue in `JoinSet`.
- Add enumeration termination/duplicate/seed diagnostics to `snapshot_summary.json`.
- Preserve v0.1.2 cache/resume semantics; interrupted full snapshots can reuse all completed cache files.
- Prefer the newest completed monolithic catalogue cache when recovering an interrupted runaway enumeration, and add `--refresh-catalogue` for a fresh catalogue-only request without invalidating project/file/SDRF caches.

## 0.1.1 — paginated/resilient PRIDE snapshot hotfix

- Fixed bounded pilots so `--limit` stops project enumeration early instead of first downloading the complete `/projects/all` response.
- Switched project-universe enumeration to PRIDE's documented `page`/`pageSize` pagination and cached each page for resume.
- Added `--project-page-size` (default 100), raised the default request timeout to 120 s, and raised default retries to 4.
- Fixed response-body timeouts/truncated JSON so they participate in retry/backoff instead of aborting immediately after headers were received.
- Added `Retry-After` handling for numeric server retry hints.
- Added `accessions.txt` to snapshot output for transparent/resumable project-universe inspection.
- Removed the unused `ProjectSnapshotResult` type that generated the v0.1.0 dead-code warning.
- Added pagination unit regressions and configurable network settings to `run_recall_discovery.sh`.

## 0.1.0 — recall-first repository bootstrap

- Added Rust workspace with `pride-scp` CLI.
- Added resumable PRIDE project/file/SDRF snapshotting with bounded Tokio concurrency.
- Added Rayon-backed multi-lane deterministic SCP candidate discovery.
- Added configurable recall-oriented discovery vocabulary.
- Added full `project_discovery_audit.tsv` including score-zero projects.
- Added candidate JSONL evidence bundles with source excerpts.
- Added known-positive recall audit and missed-positive report.
- Added Python bridge export for the existing publication/PDF/Ollama pipeline.
- Added non-destructive repository-evidence Qwen triage for no-PDF candidates.
- Added migration helper to copy the exact current Stage 01–06 Python scripts from the old working tree and hash them.
- Changed architecture so the old Stage 03 publication screen is diagnostic, not a hard gate.
- Added synthetic fixtures testing repository-text and file-manifest discovery recall.

## 0.1.2 - 2026-08-25

- Add `indicatif` progress bars/spinners to snapshot, discovery, recall audit,
  and Python bridge export.
- Show elapsed time, percentage, ETA, and current stage on long-running work.
- Add timestamped stderr logging with `--log-level` / `RUST_LOG` control.
- Add global `--no-progress` for CI/non-interactive runs.
- Keep final JSON/stdout contracts separate from progress/logging on stderr.
- Move snapshot progress updates into the network workers so progress reflects
  live PRIDE download completion.
