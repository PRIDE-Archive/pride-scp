# Changelog

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
