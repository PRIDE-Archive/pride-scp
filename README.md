# PRIDE SCP catalogue — recall-first pipeline

A hybrid Rust + Python pipeline for building a high-recall catalogue of public
mass-spectrometry single-cell proteomics (SCP) datasets, followed by
source-grounded biological-unit annotation and conservative catalogue QC.

The pipeline is currently being optimized against the **frozen independent
GT196 v1 reference** (31 August 2026 cutoff).  GT196 contains 196 public
accession-level positives across repositories, including **106 PRIDE-labelled
positives** used to benchmark the PRIDE discovery front end.  The GT is
evaluation-only and must never be used as a production accession lookup.

The core architecture is **recall first**: candidate discovery should maximize
coverage before semantic stages optimize precision.  Missing publications,
weak metadata, negative context, or an uncertain model decision must not
silently delete a candidate from the recall universe.

## Current status — GT196 discovery Iteration 1 accepted

The frozen pre-change Rust321 baseline recovered 97/106 PRIDE GT positives
(91.51%).  Discovery Iteration 1 added measured single-fibre/myofibre and
bounded MALDI/MSI single-cell context coverage.  The real frozen-snapshot
benchmark now reports:

```text
projects scanned:        40,364
candidates:                  334
strong / possible / weak:   191 / 68 / 75
A / B / C / D priority:     141 / 33 / 154 / 6
GT recovered:               105 / 106
GT missed:                    1
PRIDE discovery recall:      99.06%
```

No original Rust321 candidate disappeared.  The remaining discovery class is
cross-repository/native-accession normalization rather than another broad PRIDE
vocabulary expansion.

The **current optimization frontier is annotation quality**, not discovery
keyword accumulation.  Historical Stage-04 sensitivity was only 36/64 on
publication-backed recovered GT positives, so biological-unit, sample-unit,
pooling, benchmark/reanalysis and provenance fields must be rebuilt and
re-scored against GT196 before the recall-first branch is allowed into Stage 05.

## Current architecture

```text
PRIDE public project universe
        |
        v
Rust snapshot/index
  project JSON + file manifests + SDRF
        |
        v
Rust recall-first discovery UNION
  repository/file/SDRF text
  SCP methods + biological-unit patterns
  fibre/myofibre + bounded MALDI/MSI signals
        |
        v
candidate-audit + export-python
  A_specific / B_method / C_broad / D_adjacent
        |
        v
publication/content enrichment
        |
        +-------------------------------+
        |                               |
        v                               v
publication-backed                  repository-only
Stage 04 Qwen v18                  Qwen triage
+ deterministic evidence gate      (non-destructive)
        |                               |
        +---------------+---------------+
                        v
              deterministic semantic unifier
                 (no candidate deletion)
                        |
                        v
              GT-driven annotation redesign
                  **current frontier**
                        |
              only after revalidation
                        v
              deterministic Stage 05
                        |
                        v
              read-only Stage 06 QC
              (historical infrastructure)
```

The old Stage-03 publication screen is diagnostic only; it is not a recall
gate.  The v0.1.8-v0.1.12 Phi/Gemma recall semantic-QC decisions are
**quarantined** and must not be used as labels or to feed Stage 05.

See:

- `docs/architecture.md` for the full data flow and trust boundaries;
- `docs/model_roles.md` for what each model does and which model lanes are
  active, historical, or quarantined;
- `docs/data_contracts.md` for Rust/Python interchange formats.

## Why a hybrid implementation?

Rust handles the deterministic and potentially high-volume work:

- whole-PRIDE enumeration and local snapshotting;
- bounded concurrent API requests;
- resumable caches and retry/backoff;
- project/file/SDRF parsing;
- parallel local discovery with Rayon;
- candidate scoring/ranking;
- known-positive recall auditing;
- TSV/JSONL bridge generation.

Python remains the semantic/evidence layer because the publication-content
resolvers, targeted Qwen extractor, deterministic reconciliation, catalogue
merger, and historical QC utilities already exist there.  Their trust levels
are now explicit: Qwen extraction/triage is active but being re-benchmarked,
the deterministic unifier is active and lossless, the recall Phi/Gemma
semantic-QC decisions are quarantined, and the older Stage-05/Stage-06 lane is
historical until the primary annotations are GT196-calibrated.

No FFI is used. Rust and Python communicate via plain TSV/JSONL files.

## Repository layout

```text
pride-scp/
├── Cargo.toml
├── config/
│   └── discovery_terms.json
├── crates/
│   ├── pride-scp-core/
│   ├── pride-scp-index/
│   ├── pride-scp-discovery/
│   └── pride-scp-cli/
├── python/
│   ├── stages/                  # imported legacy-stage implementations
│   └── recall/                  # recall-first partition/unification/QC research
├── scripts/
│   ├── import_current_python.sh
│   ├── run_recall_discovery.sh
│   ├── run_gt196_discovery_benchmark.sh
│   ├── prepare_semantic_bridge.sh
│   ├── run_python_publication_enrichment.sh
│   ├── unify_semantic_results.sh
│   ├── smoke_fixture.sh
│   └── check_repo.sh
├── benchmarks/
├── tests/fixtures/
└── docs/
    ├── architecture.md
    ├── model_roles.md
    └── data_contracts.md
```

## 1. Create the new repo from this release

The release archive contains the new repository as `pride-scp/`.

After extracting it, import the exact **current local** Python scripts from the
old pipeline instead of relying on a stale embedded copy:

```bash
cd ~/Documents/github/pride-scp

./scripts/import_current_python.sh \
  ~/Documents/PRIDE_SCP/pride_scp_catalogue_pipeline
```

The helper writes `python/STAGE_SOURCES.sha256` so the migrated source state is
explicit and reviewable.

## 2. Build and validate Rust

```bash
cargo generate-lockfile
cargo fmt --all
cargo check --workspace
cargo test --workspace
cargo build --release

./scripts/smoke_fixture.sh
```

The fixture deliberately tests three discovery modes:

- explicit repository SCP language;
- a true SCP candidate recoverable only from file-manifest terminology;
- an RNA-seq-only negative context that must not become a candidate.

It then checks 100% recovery of the two known-positive fixture accessions.

## 3. Pilot the PRIDE snapshot before the full crawl

Start with a bounded test:

```bash
target/release/pride-scp snapshot \
  --output data/snapshot_pilot \
  --concurrency 4 \
  --timeout 120 \
  --retries 4 \
  --project-page-size 100 \
  --limit 100

target/release/pride-scp discover \
  --snapshot data/snapshot_pilot \
  --config config/discovery_terms.json \
  --output data/discovery_pilot \
  --min-score 1 \
  --expected-positive-count 0
```

The project catalogue is enumerated page-by-page and cached under
`project_pages/`. With `--limit 100`, enumeration stops as soon as 100 unique
PXD accessions have been collected; it does **not** download the complete PRIDE
project catalogue first. Request-body timeouts and truncated JSON responses
participate in the retry policy. Re-running the same command reuses successful
page/project/file/SDRF cache entries.

Inspect:

```text
data/snapshot_pilot/snapshot_summary.json
data/discovery_pilot/discovery_summary.json
data/discovery_pilot/project_discovery_audit.tsv
data/discovery_pilot/candidates.tsv
data/discovery_pilot/candidates.jsonl
```

`candidates.jsonl` contains the source snippets that caused each candidate to
be retained.

## 4. Full recall-first discovery

Once the pilot behaves normally:

```bash
./scripts/run_recall_discovery.sh
```

Equivalent explicit commands:

```bash
target/release/pride-scp snapshot \
  --output data/snapshot \
  --concurrency 8 \
  --timeout 120 \
  --retries 4 \
  --project-page-size 100

target/release/pride-scp discover \
  --snapshot data/snapshot \
  --config config/discovery_terms.json \
  --output data/discovery \
  --min-score 1

target/release/pride-scp export-python \
  --candidates data/discovery/candidates.tsv \
  --output data/python_bridge \
  --min-tier weak
```

### Snapshot network resilience

PRIDE's all-projects endpoint is paginated. The snapshotter caches each page in
`project_pages/page_XXXXXX.json`, then snapshots per-project metadata/files/SDRF
with bounded concurrency. The key options are:

```text
--project-page-size 100   project-enumeration page size
--timeout 120            per-request timeout including body transfer
--retries 4              retries after the initial attempt
--concurrency 8          concurrent per-project workers
```

HTTP/body-transfer failures are retried. Individual project/file/SDRF failures
are written under `errors/` and do not abort the rest of the crawl. A failure to
enumerate a project-catalogue page is fatal because proceeding would silently
truncate the universe, but successful earlier pages remain cached for resume.

### Important recall rule

Do **not** increase `--min-score` or `--min-tier` until known-positive recall
has been measured. A large candidate pool is acceptable; silently missing true
SCP projects is not.

## 5. Recall audit

The earlier manually curated catalogue can be used directly when it contains:

```text
pxd_accession
contains_true_single_cell_ms
```

Run:

```bash
target/release/pride-scp recall-audit \
  --candidates data/discovery/candidates.tsv \
  --known-positives benchmarks/known_positives_2026-08-20.csv \
  --output data/recall_audit
```

For the frozen GT196 evaluation master, use it directly as a benchmark and
filter to the repository lane being measured. The benchmark is evaluation-only:
it is never consulted by `discover`, `snapshot`, or any production lookup path.

```bash
target/release/pride-scp recall-audit \
  --candidates data/discovery/candidates.tsv \
  --known-positives gpt/final_curation_20260831/PRIDE_SCP_GT_REFERENCE_MASTER_2026-08-31_FINAL_v196.csv \
  --repository-filter PRIDE \
  --output data/recall_audit_gt196_pride
```

`recall-audit` accepts the frozen master's `reference_decision=include` rows as
positives. Do not copy GT accessions into discovery terms, runtime allow-lists,
or production tests. Keep the frozen GT directory immutable and out of Git
staging.

To rerun the same frozen discovery benchmark after a vocabulary/logic change
without refreshing the repository snapshot, use:

```bash
OUT_ROOT=data/gt196_discovery_iter1 \
  ./scripts/run_gt196_discovery_benchmark.sh
```

The script runs `discover`, `candidate-audit`, and the PRIDE-filtered recall
audit against the existing snapshot. Set `SNAPSHOT_DIR`, `GT_MASTER`, or
`OUT_ROOT` to override the defaults.

Outputs:

```text
recall_audit.tsv
missed_known_positives.tsv
recall_summary.json
```

Every missed known positive should be inspected and the discovery vocabulary
or lane logic expanded before precision filtering is tightened.

If the grant's ~208-accession source list is available, store it as a separate
benchmark and run the same audit. Do not hard-code those accessions into the
discovery result; use them to measure recall.

## 6. Publication enrichment without reintroducing the old gate

After `export-python`:

```bash
./scripts/run_python_publication_enrichment.sh
```

This uses the current Python Stage 01 with the Rust-produced accession list,
then resolves publication PDFs with the current Stage 02.

The old Stage 03 publication screen can still be run for an auxiliary score or
report, but **must not determine which Rust candidates are allowed into Stage
04**.

For publication-backed annotation, run the current Stage 04 with:

```text
--all-valid-pdfs
```

so every candidate with a valid PDF is annotated.

Projects without a publication/PDF stay in the candidate universe. They can be
prioritized with:

```bash
python python/recall/triage_repository_candidates.py \
  data/discovery/candidates.jsonl \
  --output-dir work/repository_triage \
  --model qwen2.5:3b \
  --cpu-threads 4
```

This triage is **non-destructive** and never automatically rejects a candidate.

## 7. Curation/QC trust boundary during GT196 optimization

The imported Stage-05 merger and Stage-06 read-only claim reviewer remain in
the repository, but they are **not the next automated step** for the current
recall-first branch.  First re-benchmark and redesign the primary annotation
lane against GT196.

In particular:

- do not use v0.1.8-v0.1.12 Phi/Gemma recall semantic-QC decisions as labels;
- do not build Stage 05 from those quarantined decisions;
- do not treat the old v19.1 catalogue as the current recall-first output;
- Stage 06 remains historical/read-only infrastructure for claim auditing and
  never changes GT or catalogue values automatically.

See `docs/model_roles.md` for the distinction between recall semantic QC and
the separate historical Stage-06 MiniCheck/Phi/Gemma claim-QC lane.

## Discovery scoring philosophy

The score is a prioritization aid, not a probability. Positive evidence can
come independently from:

- project title;
- project description;
- other repository metadata;
- file names/manifests;
- SDRF;
- known SCP-method terminology;
- individual-cell + proteomics/MS co-occurrence patterns.

Negative/adjacent phrases are recorded but not used as hard exclusions:

- single-cell-equivalent;
- diluted bulk;
- pooled cells;
- single-cell transcriptomics;
- spatial single-cell resolution.

This matters because a true SCP dataset may also contain benchmark or carrier
experiments that use exactly those phrases.

## Performance controls

Recommended starting values on a normal workstation:

```text
PRIDE API concurrency: 8
local discovery:        Rayon default CPU pool
PDF workers:            4
publication workers:    8
Ollama annotations:     1 local worker
```

The PRIDE concurrency is intentionally bounded. Rust makes the local and I/O
pipeline efficient without aggressively hammering external services.

## Current optimization status

Discovery Iteration 1 is accepted at **105/106 PRIDE GT positives (99.06%)**
on the frozen 40,364-project snapshot with 334 candidates.  The next discovery
change should target cross-repository/native-accession normalization for the
sole remaining GT miss; avoid broad vocabulary expansion unless a measured
error class requires it.

The larger remaining quality gap is semantic annotation.  Preserve the 334-row
candidate universe while the publication/repository evidence lanes are
re-scored against GT196 field by field.  Only after the primary lane is sound
should Stage 05 and any secondary QC model be reconsidered.

## Progress, timing, and logs (v0.1.2)

Long-running Rust commands show terminal progress by default. Progress is sent
to stderr, while the final JSON summary remains on stdout.

A full snapshot will look approximately like:

```text
⠋ [00:00:12] enumerating PRIDE projects | page 18 | 1800 accessions
⠙ [00:03:41] [=========>------------------------------] 942/4217 (22%) ETA 00:12:51 completed PXD012345
```

Discovery similarly reports the parallel local scan and output-writing phase.
The progress format includes elapsed time and ETA.

Logging defaults to `info` and is also written to stderr:

```bash
target/release/pride-scp --log-level info snapshot ...
target/release/pride-scp --log-level debug snapshot ...
RUST_LOG=debug target/release/pride-scp snapshot ...
```

Disable interactive progress without changing final summaries:

```bash
target/release/pride-scp --no-progress snapshot ...
```

To retain a human-readable run log while still seeing it in the terminal:

```bash
mkdir -p logs
target/release/pride-scp snapshot ... 2>&1 | tee logs/snapshot.log
```

If you need a clean machine-readable JSON file, redirect stdout only:

```bash
target/release/pride-scp snapshot ... > snapshot_result.json
```


## Full-catalogue snapshot behavior (v0.1.3)

The live PRIDE v3 `/projects/all` endpoint has been observed returning the
complete public project catalogue even when `page` and `pageSize` query
parameters are supplied. v0.1.3 detects this response shape and stops after the
first complete catalogue payload rather than repeatedly downloading the same
~40k-project response.

A second safety condition stops enumeration after three consecutive pages add
no new accessions (configurable with `--max-stagnant-pages`). The final
`snapshot_summary.json` records the termination reason.

For full snapshots, catalogue project records are also materialized directly
under `projects/`. This removes a redundant per-project metadata request for
most accessions. File manifests and SDRF remain independently cached.

Snapshot concurrency now has two controls:

```text
--concurrency          concurrent accession workers (default 8)
--request-concurrency  global concurrent HTTP requests (default 16)
```

Project/file/SDRF requests for an accession are independent and may run in
parallel, but the global request semaphore prevents unbounded load on PRIDE.

A conservative full run is:

```bash
target/release/pride-scp snapshot \
  --output data/snapshot \
  --concurrency 8 \
  --request-concurrency 16 \
  --timeout 120 \
  --retries 4 \
  --project-page-size 100
```

It is safe to interrupt with Ctrl+C. Rerunning the same command reuses complete
cached catalogue/project/file/SDRF outputs. Existing `page_000001.json` and
later files from a v0.1.2 runaway enumeration can be left in place; v0.1.3
normally terminates from cached page 0 before reading them.

For a fast metadata-only first pass, use:

```bash
target/release/pride-scp snapshot \
  --output data/snapshot_metadata \
  --no-files \
  --no-sdrf \
  --concurrency 8 \
  --request-concurrency 16
```

This can be followed by discovery and targeted file/SDRF enrichment for the
candidate accession list if desired.

If resuming the interrupted v0.1.2 run, v0.1.3 first inspects the highest
completed cached `project_pages/page_*.json`. If it is a complete monolithic
catalogue response, that newest cache is used directly. To request one current
catalogue snapshot instead, add `--refresh-catalogue`; unlike `--force`, this
does not invalidate project/file/SDRF caches.

## v0.1.4 candidate diagnostics and semantic bridge

The discovery tier remains a recall score, not a probability of true SCP. Before
Ollama annotation, classify discovery features into semantic-review profiles:

```bash
target/release/pride-scp candidate-audit \
  --candidates data/discovery/candidates.jsonl \
  --config config/discovery_terms.json \
  --output data/candidate_audit
```

Outputs:

```text
candidate_diagnostics.tsv
candidate_diagnostics.jsonl
broad_only_candidates.tsv
candidate_audit_summary.json
```

The audit separates:

```text
specific_scp_labels
method_labels
biological_specific_labels
broad_context_labels
adjacent_labels
negative_context_labels
```

and assigns a non-destructive semantic priority:

```text
A_specific
B_method
C_broad
D_adjacent
```

All candidates are retained regardless of priority.

Prepare the Python bridge with full hit excerpts:

```bash
target/release/pride-scp export-python \
  --candidates data/discovery/candidates.tsv \
  --candidates-jsonl data/discovery/candidates.jsonl \
  --config config/discovery_terms.json \
  --output data/python_bridge \
  --min-tier weak
```

or run both commands with:

```bash
scripts/prepare_semantic_bridge.sh
```

The richer bridge writes:

```text
candidate_accessions.txt
candidate_manifest.tsv
semantic_candidates.jsonl
python_bridge_summary.json
```

After publication/content enrichment, `run_python_publication_enrichment.sh`
partitions the **current discovery candidate universe** into publication-backed
and repository-only evidence modes.  The accepted GT196 Iteration-1 universe is
334 candidates; the earlier v0.1.8 semantic-unification experiment used 321.
Every candidate is assigned exactly one evidence mode, and absence of a PDF or
full text never removes a candidate.

## Snapshot compaction

Once a full snapshot has successfully materialized `projects/` and discovery has
completed, the historical `project_pages/` cache is redundant. In particular,
the v0.1.2 runaway pagination cache can contain hundreds of gigabytes of repeated
monolithic catalogue responses.

Preview safe compaction first:

```bash
target/release/pride-scp compact-snapshot \
  --snapshot data/snapshot \
  --dry-run
```

Then compact:

```bash
target/release/pride-scp compact-snapshot \
  --snapshot data/snapshot
```

By default this validates that all indexed accessions have materialized project
records, deletes redundant catalogue pages, and keeps only the newest completed
catalogue page for provenance. `projects/`, `files/`, `sdrf/`, `accessions.txt`,
and discovery outputs are untouched.

For the current ~414 GB `project_pages/` cache this should reclaim almost all of
that space while preserving the complete materialized snapshot needed to rerun
discovery.

An optional later mode can also retain file/SDRF evidence only for a supplied
candidate accession list:

```bash
target/release/pride-scp compact-snapshot \
  --snapshot data/snapshot \
  --retain-accessions-file data/python_bridge/candidate_accessions.txt \
  --prune-noncandidate-evidence
```

Do **not** use that second mode yet if you may retune discovery vocabulary and
want to rescan all 40,364 projects using the original file/SDRF evidence. The
~4 GB full file/SDRF cache is modest compared with the redundant catalogue-page
cache and is worth retaining for now.

## v0.1.5 publication/PDF recovery

The publication resolver now treats PDFs as an evidence enrichment layer, not a
candidate gate.  It retries the previously broken OA cache and resolves in this
order:

1. current validated PDF;
2. `manual_pdfs/` / manual manifest;
3. legacy PDF reuse directories;
4. Europe PMC DOI/PMID/title lookup and PMCID render fallbacks;
5. manifest PDF candidate;
6. Unpaywall when `UNPAYWALL_EMAIL` is configured.

Run the normal enrichment helper:

```bash
scripts/run_python_publication_enrichment.sh
```

To reuse PDFs from another workspace:

```bash
LEGACY_PDF_DIRS=/path/to/old/publication_pdfs:/another/pdf/cache \
  scripts/run_python_publication_enrichment.sh
```

Unresolved publications are written to:

```text
work/python/manual_pdf_queue.tsv
```

Manually downloaded PDFs can be placed in `manual_pdfs/` as `PXDxxxxxx.pdf`
or using the queue's `suggested_filename`.  For shared publications, use
`manual_pdfs/manual_pdf_manifest.tsv`; see `manual_pdfs/README.md`.

PDF files under `manual_pdfs/` are ignored by Git.  The resolver validates PDF
magic bytes before accepting any automatic, reused, or manual file.


## v0.1.6 NCBI PMC identifier fallback

The v0.1.5 live smoke test showed that Europe PMC free-text DOI searches can
return no matches in some environments even for papers that are definitely in
PubMed Central. v0.1.6 therefore resolves DOI/PMID/PMCID first through the
NCBI PMC ID Converter API. A recovered PMCID is immediately converted into
validated PMC PDF candidate URLs. Europe PMC remains a secondary metadata/title
resolver.

The resolver cache schema is now 3, so v0.1.5 `no_open_access_pdf` results are
retried automatically. Optional NCBI contact metadata can be supplied via
`NCBI_EMAIL` or `CONTACT_EMAIL`.

Validate with:

```bash
python python/recall/pdf_resolver_regression_smoke.py
python python/recall/pdf_resolver_live_smoke.py \
  --keep-output work/python/pdf_resolver_live_smoke_v016
```


## v0.1.7 generic publication content and manual manuscript queue

A publication no longer needs a PDF container to become publication-backed.
After Stage 02, Stage 03 resolves content in this order:

```text
validated PDF
    ↓
Europe PMC full-text JATS XML
    ↓
PMC article HTML full text
    ↓
repository-only evidence
```

Run the complete enrichment/content pass with:

```bash
scripts/run_python_publication_enrichment.sh
```

The resulting content manifest is:

```text
work/python/pride_candidate_publications_with_content.tsv
```

and the semantic partition is based on any validated publication-content
artifact rather than PDF availability alone.

To list accessions that still lack PDFs/manuscripts:

```bash
scripts/write_missing_manuscripts.sh
```

Outputs include:

```text
work/python/manual_manuscripts/missing_pdf_accessions.txt
work/python/manual_manuscripts/missing_pdf_publications.tsv
work/python/manual_manuscripts/missing_manuscript_accessions.txt
work/python/manual_manuscripts/missing_manuscript_publications.tsv
work/python/manual_manuscripts/manual_pdf_manifest.template.tsv
```

`missing_pdf_*` includes every publication-linked PXD that lacks a usable PDF,
even if XML/HTML full text was recovered. `missing_manuscript_*` is the higher
priority manual-download set still lacking any usable publication full text.

Save manual PDFs under `manual_pdfs/`. For explicit mapping, copy/edit the
generated template as `manual_pdfs/manual_pdf_manifest.tsv`; one PDF path may
be listed for multiple PXD accessions. Rerunning the enrichment script picks up
and validates manual PDFs automatically.

Publication-backed Stage-04 annotation now uses:

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

## 9. Semantic unification and historical semantic-QC experiments

The publication-backed Stage-04 decision and repository-only Qwen triage are
**not calibrated equivalents**.  Run the deterministic unifier after both
lanes finish:

```bash
scripts/unify_semantic_results.sh
```

The unifier writes a lossless manifest and review routes:

```text
work/python/semantic_unification/
├── unified_semantic_manifest.tsv
├── unified_semantic_manifest.jsonl
├── include_candidate.tsv
├── review_high.tsv
├── review_medium.tsv
├── review_low.tsv
├── likely_non_scp.tsv
├── secondary_review_queue.tsv
├── secondary_review_queue.jsonl
├── review_decisions.template.tsv
└── semantic_unification_summary.json
```

Routing remains recall-preserving. `likely_non_scp` is a retained route, not a
deletion. `include_candidate` is provisional, not a GT label.  The v0.1.8
implementation validated complete coverage of the historical 321-candidate
universe; rerun it on the current 334-candidate universe rather than assuming
old counts.

### Quarantined v0.1.8-v0.1.12 Phi/Gemma semantic QC

The repository still contains:

```text
scripts/run_semantic_qc.sh
python/recall/build_semantic_qc_packets.py
python/recall/adjudicate_semantic_qc.py
```

Those scripts implemented the historical `phi4-mini:3.8b` critic + selective
`gemma3:4b` jury experiments.  The factual axes and regression fixtures are
useful for error analysis, but the **affirmative/final decisions are
quarantined** after comparison with the independent GT reference.

Do not run this lane to produce current truth labels, do not copy its old
`review_decisions.tsv` into Stage 05, and do not tune GT196 to agree with it.
The v0.1.9-v0.1.12 changes below should therefore be read as historical design
experiments, not current operating instructions:

- v0.1.9 rehydrated primary source passages and factual axes after v0.1.8
  collapsed toward uncertainty;
- v0.1.10 formalized an individual-cell-to-MS evidence chain and structured
  retry/repair;
- v0.1.11 made target-MS sample composition and many-cell populations explicit;
- v0.1.12 scoped many-cell counts to exact passages/sample roles.

The next production annotation lane should reuse the useful source-grounding
ideas only after each factual axis is re-benchmarked directly against GT196.
See `docs/model_roles.md`.
