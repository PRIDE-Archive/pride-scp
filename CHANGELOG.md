
## GT196 v19-shadow-2.5 sample-unit normalization

- Preserve the accepted v19-shadow-2.4 decision policy while expanding literal biological-cell unit recognition.
- Add deterministic, evidence-referenced sample-unit normalization from existing repository and Stage04 sample/preparation evidence.
- Add a full PRIDE106 policy-replay runner so this policy-only change can be benchmarked without repeating Ollama inference.
- Keep GT196 evaluation-only and keep Stage05 / historical affirmative Phi-Gemma QC quarantined.

- Add v19-shadow-2.4 full-PRIDE106 calibration: make direct multi-cell risk branch-aware for mixed designs, add high-specificity diluted-bulk/single-cell-like and near-single-cell spatial exclusion evidence, preserve true mixed benchmark+single-cell studies, and report include/review/hard-exclude metrics explicitly in GT196 evaluation.
### v19-shadow-2.4 reporting hotfix

- Fix the end-of-run `NameError` caused by a missing `write_tsv` helper in `pride_scp_curation_v19.py`.
- Preserve the `v19-shadow-2.4` curation/cache version because model inference, evidence packets, and deterministic curation policy are unchanged. Existing compatible accession status files can therefore be reused with `CURATION_FORCE=0` to regenerate the summary/evaluation without repeating Ollama inference.
- Add regression coverage for heterogeneous and empty curation-summary TSV output.

- Add v19-shadow-2.3 consistency calibration: normalize impossible `destructive_pooling=no` + pre-identity-pooling combinations, add deterministic benchmark-only/diluted-bulk evidence, require a cell-like sample-unit noun, and let multiple direct one-cell/MS support signals override a noisy biological-unit class without overriding direct risk evidence.
- Add v19-shadow-2.2 evidence-authority hardening: LLM-only pooling/identity negatives can no longer exclude without deterministic corroboration; require accession-specific one-cell/MS linkage for inclusion; add lexical grounding checks for biological sample identity/counts; and route cell-enriched-sample linkage ambiguity to review.
# Unreleased — GT196 optimization

- Add v19-shadow-2.1 execution integrity: expose the curation pipeline version, refuse stale source versions in GT runners, and key cache reuse by pipeline version + model + evidence-packet SHA-256.
- Make the 15-accession v19 smoke force recomputation by default so stale status JSON cannot masquerade as a new model/policy iteration.
- Invalidate the attempted v19-shadow-2 smoke after archived outputs showed all 15 records were actually `v19-shadow-1`.
- Soften `biological_unit_class` as an exclusion signal after replay showed the 3B extractor often used `cell_population` for studies containing many separate one-cell samples; contradictory class alone now routes to review.
- Clarify the v19 prompt that `cell_population` means multiple cells in one target MS sample, and remove the HeLa formatting anchor that leaked into unrelated sample-unit outputs.
- Extend direct multi-cell guards to scientific notation / extracted forms such as `1 x 10^6` / `1 x 106` and adjacent-sentence sample wording, covering the observed PXD028991 failure mode without accession hard-coding.
- Reject the first live discovery Iteration-2 result as a registry-parser failure: 55,706 ProteomeCentral records were fetched across 558 pages but zero PXD aliases were extracted, leaving discovery unchanged at 334 candidates and 105/106 GT recall.
- Make ProteomeCentral PXD extraction recursively structure-aware for nested/wrapped PROXI identifier fields and identifier CV terms while avoiding promotion of PXD mentions that occur only in free-text descriptions.
- Add a page-1 registry schema guard so a populated PROXI response with zero parsed PXD aliases fails immediately instead of spending hours producing an invalid empty alias index.
- Reuse the 558 cached PROXI pages for parser-fix acceptance when `--force`/`REGISTRY_FORCE` is not set; no repeat network crawl is required.
- Implement discovery Iteration 2 with a cached ProteomeCentral/PROXI registry snapshot and PXD/native-accession normalization for cross-repository datasets absent from the primary PRIDE catalogue.
- Reconcile generated registry supplements after complete enumeration so stale PXD aliases cannot survive refreshes, and fail on an explicit page-cap hit instead of benchmarking an incomplete registry snapshot.
- Keep ProteomeCentral strictly supplemental during discovery: registry records whose PXD already exists under `snapshot/projects/` are not rescored, preserving accepted Iteration-1 scores/tiers.
- Add `registry-snapshot` CLI support, normalized `snapshot/registry/projects/` records, an explicit `registry_accessions.tsv` alias/provenance crosswalk, and registry enumeration diagnostics.
- Update production and GT196 benchmark runners to ensure the registry cache before discovery, with explicit offline/refresh controls.
- Extend discovery summaries with primary-project and registry-supplement counts and add regression tests for PXD/native-alias parsing plus primary-snapshot precedence.
- Keep GT196 evaluation-only: registry enumeration is global and never receives GT accessions. The Iteration-2 acceptance target is recovery of the sole Iteration-1 miss (`PXD047101`) through general registry normalization, not an accession-specific rule.
- Accept discovery Iteration 1 on the frozen 40,364-project snapshot: 334 candidates, 105/106 PRIDE GT positives recovered (99.06% recall), one miss, and no loss from the original Rust321 universe.
- Confirm the 13 candidate additions: eight GT recoveries and five non-GT review candidates; PXD053022 is recovered from repository metadata at weak priority.
- Update architecture/model documentation to distinguish active Qwen extraction/triage, deterministic lossless unification, quarantined v0.1.8-v0.1.12 Phi/Gemma recall semantic QC, and the separate historical Stage-06 MiniCheck/Phi/Gemma claim-QC lane.
- Mark GT-driven biological-unit/pooling annotation redesign as the current optimization frontier before Stage 05 is re-enabled for the recall-first branch.
- Freeze the pre-change Rust321 discovery baseline at 97/106 PRIDE-labelled GT196 positives (91.51% recall) before changing discovery logic.
- Expand recall-first discovery vocabulary for single muscle fibre/fiber and myofibre/myofiber proteomics, a measured dominant false-negative class.
- Add a bounded MALDI/MSI + single-cell-context regex signal so single-cell MALDI imaging datasets can enter review without making generic spatial or MALDI studies positive.
- Add regression tests for fibre vocabulary and for joint MALDI/single-cell evidence.
- Extend `recall-audit` to read frozen GT-style `reference_decision=include` labels and optionally filter by `hosting_repository`, keeping GT use strictly in evaluation rather than runtime discovery.
- Add an evaluation-only `run_gt196_discovery_benchmark.sh` runner so every discovery iteration is scored against the same frozen snapshot/GT contract without using GT accessions during discovery.
- Preserve negative/pooled fibre studies as recall candidates for downstream adjudication; discovery remains intentionally non-destructive.
- Record cross-repository/native-accession normalization as a separate next discovery problem after source review showed PXD047101 is MassIVE-hosted as MSV000093434 despite its PXD secondary accession.
- Keep the v0.1.8-v0.1.12 affirmative Phi/Gemma QC decisions quarantined; this iteration changes discovery only.

## 0.1.12 - 2026-08-28

- Scope many-cell counts to the exact source passage/sample role rather than applying any count anywhere in a study to every target MS sample.
- Add deterministic passage anchors for explicit one-cell targets, many-cell population target samples, and separate multi-cell libraries/controls.
- Prevent library/control/benchmark counts from overwriting an explicit one-cell-to-MS chain.
- Keep a many-cell target-sample passage as a hard exclusion when no explicit one-cell target passage exists.
- Avoid misreading low-input quantities such as `250 pg of HeLa cell peptides` as cell-count population evidence.
- Add passage-anchor counts to semantic-QC diagnostic TSV output.
- Add an eight-accession extended smoke set to reduce overfitting to the original four controls before the full 219-candidate run.
- Bump evidence-packet and semantic-QC cache versions so v0.1.11 results are automatically invalidated.

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

## GT196 curation v19 shadow lane — 2026-09-02

- Froze discovery optimization at 106/106 PRIDE GT positives.
- Added an evidence-first `v19-shadow-1` curation lane that combines repository evidence with saved Stage04 raw evidence blocks.
- Added explicit GT-aligned biological-unit, identity-preservation, destructive-pooling, benchmark, adjacent-modality, reanalysis, and mixed-design fields.
- Added field-specific evidence-reference validation; unsupported model assertions are downgraded rather than trusted.
- Added deterministic include/exclude/review logic outside the LLM.
- Added frozen-GT evaluation tooling. GT196 remains evaluation-only and is never supplied to production/shadow inference.
- The historical Stage04 v18 final classifier remains unchanged while v19 is benchmarked.
## 2026-09-03 — v19-shadow-2.6 deterministic evidence enrichment

- Preserve the accepted shadow-2.5 model packet and Qwen prompt.
- Add a broader deterministic-only evidence pool from full saved Stage04 evidence, project JSON, file metadata and SDRF rows.
- Add publication-bundle accession-linkage diagnostics and suppress explicitly mismatched bundles from enrichment.
- Expand one-cell procedural detection to zygotes and common cell-type nouns.
- Allow direct qualifying one-cell procedural evidence to override model-only benchmark/pooling/identity conflicts when no direct negative evidence exists.
- Add an evaluation-only enriched-evidence replay so the 114-row benchmark can be rescored without Ollama.
- Conservative packet-only lower bound: 82/106 GT positives included, 24 reviewed, 0 excluded; strict-negative controls remain 0 include / 2 review / 6 exclude.

### v19-shadow-2.6.1 — branch-safety evidence hotfix

- Fixes the shadow-2.6 false-negative regression where a diluted/single-cell-equivalent benchmark arm could veto a separate genuine one-cell branch.
- Recognizes high-specificity procedural wording such as `single cells were lysed` as a qualifying branch without treating noun phrases such as `single HeLa cell digest` as real-cell evidence.
- Treats a real qualifying branch plus a separate diluted/benchmark or multi-cell branch as mixed design rather than automatic exclusion.
- Allows biological-sample-unit normalization from the qualifying branch even when a separate nonqualifying benchmark arm is present.
- Keeps the five explicit publication/accession mismatch diagnostics visible; source-linkage remediation remains a separate upstream task.

## 2026-09-03 — v19-shadow-2.6.1 review-evidence audit lane

- Accepted `v19-shadow-2.6.1` evidence replay baseline after the full PRIDE106 benchmark reached 90 include / 16 review / 0 hard excludes while preserving 0 strict-negative includes.
- Added evaluation-only review evidence dossier generation (`audit_v19_review_evidence.py`) and runner.
- The audit does not read GT labels or modify curation decisions. It inventories repository snapshot, file/SDRF, Stage04 publication evidence, and explicit publication↔PXD routing for current `review` accessions.
- Added cross-directory explicit-PXD publication routing diagnostics so historical publication/accession mismatches can be repaired upstream rather than compensated for by decision rules.

## 2026-09-03 — v19-shadow-2.7 provenance-safe evidence routing

- Added an explicit-PXD publication evidence router with cross-directory routing.
- Quarantined publication bundles whose explicit PXD list does not contain the declared annotation target.
- Quarantined historical packet publication rows only for proven explicit publication/PXD mismatches; affected publication evidence is rebuilt from routed source bundles.
- Added conservative procedural single-cell sample-unit grounding.
- Added bacterial single-cell branch recognition while protecting `single-cell resolution` and method-only phrases.
- Added linkage-clean replay and route-index diagnostics; no GT labels are used by the replay itself.
- Kept the accepted v19-shadow-2.6.1 include/exclude policy unchanged.
