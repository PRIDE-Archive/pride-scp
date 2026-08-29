# PRIDE SCP catalogue — recall-first pipeline

A hybrid Rust + Python pipeline for building a high-recall catalogue of public
PRIDE single-cell proteomics (SCP) datasets, followed by evidence-grounded
semantic annotation and conservative QC.

This repository starts from a deliberately different premise than the older
pipeline: **candidate discovery must maximize recall before downstream models
optimize precision**.

The current grant estimate is approximately 208 public PRIDE SCP datasets, so
an upstream candidate universe smaller than that is an immediate recall alarm.

## Architecture

```text
ALL PUBLIC PRIDE PROJECTS
        |
        v
Rust snapshot/index
  project JSON
  file manifests
  SDRF when available
        |
        v
Rust multi-lane discovery UNION
  repository text
  filenames/file metadata
  SDRF text
  known SCP methods
  individual-cell language
  low-input/adjacent context retained
        |
        +----------------------------+
        |                            |
        v                            v
repository semantic triage       publication enrichment
(optional, non-destructive)      + PDF when available
                                     |
                                     v
                            existing targeted Qwen annotator
                                     |
                                     v
                            existing deterministic curation
                                     |
                                     v
                            existing Stage-06 jury QC
```

See `docs/architecture.md` and `docs/data_contracts.md`.

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

Python remains the semantic layer because the current PDF extraction,
Qwen/Ollama annotator, deterministic Stage 05 curation, and Stage 06
MiniCheck/Phi/Gemma QC have already been developed and tested.

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
│   ├── stages/                  # imported exact current Python scripts
│   └── recall/
│       └── triage_repository_candidates.py
├── scripts/
│   ├── import_current_python.sh
│   ├── run_recall_discovery.sh
│   ├── run_python_publication_enrichment.sh
│   ├── smoke_fixture.sh
│   └── check_repo.sh
├── benchmarks/
├── tests/fixtures/
└── docs/
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

## 7. Existing curation/QC

Once the candidate universe and semantic annotations are rebuilt, reuse the
imported current scripts:

```text
05_merge_pride_scp_catalogue.py
06_review_pride_scp_catalogue.py
```

The validated Stage-06 selective-jury architecture remains downstream QC; it
should not compensate for missed discovery candidates.

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

## Current status

This v0.1.0 release establishes the new repository structure and the
recall-first front end. Before using it to replace the old catalogue, validate:

1. Rust workspace tests;
2. a small live PRIDE snapshot;
3. recall against the earlier manual positive set;
4. candidate count and missed-positive report;
5. only then run the expensive publication/Qwen stages.

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

After Stage-01/02 publication enrichment, `run_python_publication_enrichment.sh`
partitions the 321-candidate universe into publication-backed and repository-only
evidence modes. Every candidate is assigned exactly one mode; absence of a PDF
never removes a candidate.

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

## 9. Semantic unification before Stage 05 (v0.1.8)

The publication-backed Stage-04 decision and repository-only Qwen triage are
**not calibrated equivalents**. Do not concatenate them or use
`possible_true_scp` as an inclusion rule.

After both semantic lanes finish, run:

```bash
scripts/unify_semantic_results.sh
```

The unifier validates complete 321-candidate coverage and writes:

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
deletion. `include_candidate` is also provisional and still requires QC.

The recommended next step is an independent small-model QC pass over the
provisional includes plus all review routes:

```bash
scripts/run_semantic_qc.sh
```

By default this runs `phi4-mini:3.8b` as a strict critic, explicitly unloads
it, then calls `gemma3:4b` only for selected conflicts/uncertainties. The
resulting `review_decisions.tsv` can override provisional includes as well as
review candidates. Critic/jury disagreement remains `uncertain` and therefore
blocks final Stage-05 bridge generation rather than being forced.

A final Stage-05 bridge is deliberately gated. Copy/fill the generated review
decision template as:

```text
work/python/semantic_unification/review_decisions.tsv
```

and set every review candidate to `include` or `exclude`. Then run:

```bash
scripts/build_stage05_bridge.sh
```

The bridge generator refuses to create a final bridge while review decisions
remain unresolved. Once complete, it emits a Stage-05-compatible manifest,
status directory, and annotations under `work/python/stage05_bridge/`.
Repository-only annotations are intentionally metadata-sparse; the unified
semantic manifest remains the classification provenance authority.



## v0.1.9 evidence-grounded semantic QC

If the v0.1.8 QC produced a near-universal `uncertain` result, do not manually adjudicate hundreds of accessions. v0.1.9 rebuilds QC packets from the exact Stage-04 source passages and repository excerpts, automatically invalidates the old QC cache, and reruns only the evidence-grounded critic/jury workflow.

```bash
python python/recall/evidence_grounded_qc_regression_smoke.py
scripts/run_semantic_qc_smoke.sh
# Only after the four-accession smoke is qualitatively correct:
scripts/run_semantic_qc.sh
```

Inspect `work/python/semantic_unification/qc_evidence_packet_summary.json` and `work/python/semantic_qc/semantic_qc_summary.json` before building the Stage-05 bridge.


## v0.1.10 semantic-QC calibration

The independent semantic QC now evaluates a complete individual-cell-to-MS evidence chain rather than requiring a literal same-cell/MS sentence. It accepts separate preparation or identity-preserving labels followed by MS, including identity-preserving multiplexing, while continuing to reject population/bulk samples and destructive pooling before cell identity is preserved. Critic and jury use separate output-token budgets and malformed structured responses receive one corrective JSON retry. Run `scripts/run_semantic_qc_smoke.sh` and require the 2-positive/2-negative qualitative controls to pass before launching the full QC batch.


### v0.1.11 semantic QC calibration

Independent semantic QC now evaluates the composition of each target MS sample explicitly.
A many-cell FACS population feeding one proteomic replicate is not single-cell MS, whereas
one-cell-per-well target samples remain SCP even when separate multi-cell libraries or
low-input benchmarks are present. Run `scripts/run_semantic_qc_smoke.sh` and require the
four-control 2-include/2-exclude result before starting the full QC batch.


### v0.1.12 semantic-QC sample-unit scoping

Semantic QC now uses passage-scoped lexical anchors so a many-cell count from a library, carrier, benchmark, or other control cannot be transferred to an explicit one-cell target MS sample. A many-cell count tied directly to a replicate/sample and downstream proteomic handling remains a strong population-sample exclusion signal. Run the four-control smoke and then the eight-control extended smoke before launching the full QC batch.
