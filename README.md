# PRIDE-SCP

PRIDE-SCP is a hybrid Rust + Python pipeline for discovering, curating, and
annotating public mass-spectrometry single-cell proteomics (SCP) datasets.

The project is built around two production principles:

1. **Recall-first discovery.** Repository enumeration and candidate discovery
   are deterministic and intentionally conservative about candidate loss.
2. **Evidence-grounded annotation.** LLMs interpret bounded source evidence;
   deterministic code validates provenance, constructs final records, and
   preserves unresolved fields rather than inventing metadata.

The current implementation supports PRIDE as the primary production lane,
cross-repository accession/identity resolution, native MassIVE discovery, and
SDRF-Proteomics source resolution, audit, and reconstruction.

> Frozen GT/reference collections are evaluation-only. Production discovery,
> curation, and SDRF field generation must not use GT accessions or labels as
> runtime truth.

## Architecture

```text
Public repositories
  PRIDE API ───────────────┐
  ProteomeCentral / PROXI ─┼─> local deterministic snapshot/index
  MassIVE native catalog ──┘
                                  |
                                  v
                         recall-first discovery
                                  |
                                  v
                       candidate evidence packets
                                  |
                    +-------------+-------------+
                    |                           |
                    v                           v
          publication/source enrichment    repository evidence
                    |                           |
                    +-------------+-------------+
                                  v
                      evidence-grounded curation
                                  |
                                  v
                         production catalogue

SDRF lane
  repository/project identity
          |
          v
  source resolution
  BigBio curated -> repository submitted -> local repository cache
          |
          +----------------------+-----------------------+
          |                                              |
          v                                              v
  preserve + deterministic audit                 no usable SDRF
                                                         |
                                                         v
                                               study-design scaffold
                                                         |
                                               bounded Ollama extraction
                                                         |
                                               deterministic SDRF draft
```

### Components

| Component | Responsibility |
|---|---|
| `pride-scp-index` | Resumable PRIDE, ProteomeCentral, and MassIVE repository acquisition and normalization. |
| `pride-scp-discovery` | Deterministic high-recall candidate scoring and source routing. |
| `pride-scp-sdrf` | SDRF source resolution, structural audit, evidence extraction, study-design inference, provenance repair, and deterministic draft construction. |
| `pride-scp-cli` | User-facing command-line interface. |
| `python/stages/` | Publication enrichment and legacy semantic pipeline stages still used by the production workflow. |
| `python/curation/` | Evidence-grounded curation logic. |

Detailed design and trust boundaries are documented in
[`docs/architecture.md`](docs/architecture.md).

## Build

Rust toolchain configuration is pinned in `rust-toolchain.toml`.

```bash
cargo fmt --all -- --check
cargo check --workspace --locked
cargo test --workspace --locked
cargo build --release --locked
```

Python helpers target Python 3.12:

```bash
python -m pip install -r python/requirements.txt
```

See [`VALIDATION.md`](VALIDATION.md) for the full validation matrix.

## Core commands

The examples below use the release binary:

```bash
BIN=target/release/pride-scp
```

### 1. Snapshot PRIDE

```bash
$BIN snapshot \
  --output data/snapshot \
  --concurrency 8 \
  --request-concurrency 16
```

The snapshot is resumable and materializes project metadata, file manifests,
and repository SDRF responses locally.

### 2. Add ProteomeCentral cross-repository identity

```bash
$BIN registry-snapshot \
  --snapshot data/snapshot
```

This records PXD/native-accession relationships and repository provenance.

### 3. Add native MassIVE projects

```bash
$BIN massive-native-snapshot \
  --snapshot data/snapshot
```

This lane can discover public `MSV...` datasets even when no PXD alias exists.

### 4. Discover SCP candidates

PRIDE-primary production discovery:

```bash
$BIN discover \
  --snapshot data/snapshot \
  --config config/discovery_terms.json \
  --output data/discovery_pride \
  --source-scope pride-primary \
  --min-score 1 \
  --expected-positive-count 0
```

Cross-repository or audit workflows may use the other source scopes exposed by
`pride-scp discover --help`.

### 5. Inspect candidate evidence

```bash
$BIN candidate-audit \
  --candidates data/discovery_pride/candidates.jsonl \
  --config config/discovery_terms.json \
  --output data/candidate_audit
```

### 6. Export the Python bridge

```bash
$BIN export-python \
  --candidates data/discovery_pride/candidates.tsv \
  --output data/python_bridge
```

The bridge keeps deterministic repository evidence separate from semantic
interpretation.

## SDRF-Proteomics workflow

PRIDE-SCP treats existing curated SDRFs as source evidence that should be
preserved, not regenerated.

### Resolve trusted SDRF sources

```bash
$BIN sdrf-resolve \
  --accessions-file accessions.txt \
  --snapshot data/snapshot \
  --output data/sdrf_sources
```

Source precedence is currently:

1. community-curated BigBio SDRF;
2. repository-submitted SDRF;
3. usable repository/API snapshot SDRF.

Header-only API responses are not treated as usable SDRFs.

### Audit resolved SDRFs without an LLM

```bash
$BIN sdrf-audit \
  --accessions-file accessions.txt \
  --snapshot data/snapshot \
  --resolved-sdrf-dir data/sdrf_sources/resolved \
  --output data/sdrf_audit
```

The audit separates SDRF structural validity from repository-file linkage.

### Annotate or reconstruct SDRFs

```bash
$BIN sdrf-annotate \
  --accessions-file accessions.txt \
  --snapshot data/snapshot \
  --resolved-sdrf-dir data/sdrf_sources/resolved \
  --annotations-dir work/python/pride_scp_annotations/annotations \
  --publication-manifest work/python/pride_candidate_publications_with_content.tsv \
  --output data/sdrf_annotation \
  --model qwen2.5:3b
```

Completed accessions are cached at accession level. Re-running the same command
with the same generator/spec/template state resumes from completed audit files;
do not remove the output directory or pass `--force` when resuming.

The LLM never serializes final SDRF rows directly. Rust validates evidence
references, applies deterministic study-design rules, preserves existing rows,
and leaves unsupported values unresolved.

See [`docs/sdrf_single_cell_annotation.md`](docs/sdrf_single_cell_annotation.md)
for the SDRF-specific contract.

## Repository layout

```text
pride-scp/
├── crates/                 Rust implementation
├── config/                 deterministic discovery configuration
├── python/                 publication, semantic, and curation helpers
├── scripts/                maintained operational wrappers
├── docs/                   architecture and operational documentation
├── tests/                  small committed fixtures
├── Cargo.toml
├── README.md
└── VALIDATION.md
```

Large snapshots, manuscripts, GT/reference collections, experiment outputs,
handoffs, and source archives are local artifacts and are intentionally excluded
from Git.

## Production and evaluation boundaries

Production code may use repository metadata, repository files, manuscript
content, curated external SDRFs, and deterministic/LLM evidence generated from
those sources.

Evaluation datasets may measure recall, precision, field agreement, and failure
modes **after** production outputs have been generated. They must not provide
runtime accession membership, field values, relation mappings, or labels.

See [`docs/model_roles.md`](docs/model_roles.md) and
[`docs/data_contracts.md`](docs/data_contracts.md).

## Repository maintenance

The repository includes a safe cleanup helper that only moves **untracked**
historical/local artifacts and refuses to move tracked files:

```bash
./scripts/repository_cleanup.sh
./scripts/repository_cleanup.sh --apply
```

An optional experiment archive mode handles legacy GT benchmark wrappers and
one-off evaluation material:

```bash
./scripts/repository_cleanup.sh --apply --include-experiments
```

Nothing is deleted. Files are moved to a timestamped sibling archive outside
the Git worktree. See
[`docs/repository_maintenance.md`](docs/repository_maintenance.md).

## Reproducible container / HPC deployment

The mixed Rust + Python pipeline has a Docker-canonical, Singularity/Apptainer and Slurm deployment
pattern documented in [`docs/hpc_container_deployment.md`](docs/hpc_container_deployment.md). The HPC
launchers support offline pre-fetched Ollama models, optional NVIDIA GPU execution and node-local
staging without embedding cluster-specific paths in the application.

### BigBio-aligned SDRF readiness

`python scripts/sdrf_bigbio_readiness.py` provides the fail-closed final acceptance gate for
source-grounded SDRF candidates. It integrates the official `parse_sdrf` validator, optional frozen
`sdrf-skills` deterministic checks, SHA-256-bound independent-review approval, and the eventual
`bigbio/sdrf-annotated-datasets` `sandbox/`/`datasets/` directory contract. It never generates missing
sample/file/channel truth.
