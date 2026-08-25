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
