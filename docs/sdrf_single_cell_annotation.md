# PRIDE_SCP single-cell SDRF annotation lane

## Status

Initial implementation: `pride-scp sdrf-annotate` (`pride-scp-sdrf-v0.1`).

This lane is intentionally independent of the frozen GT196/GT179 benchmark. It accepts an explicit list of PRIDE accessions and generates provenance-tracked SDRF-Proteomics **drafts** from source evidence only.

The PRIDE production catalogue/curation lane is currently on hold. Do not implicitly use its provisional include count as an SDRF accession list; provide the desired accession list explicitly.

## Standards pinned by this implementation

- SDRF-Proteomics core specification: v1.1.0
- single-cell template: v1.0.0
- template profile: `bigbio-single-cell-1.0.0-github-main-observed-2026-09-06`

Sources:

- https://sdrf.quantms.org/specification.html
- https://github.com/bigbio/sdrf-templates/blob/main/single-cell/1.0.0/single-cell.yaml

The single-cell template is still evolving. Every generated audit records the pinned profile and requires revalidation against the live template before submission.

## Design principle

Ollama does **not** write the SDRF directly.

The flow is:

```text
PRIDE project metadata
+ PRIDE RAW file inventory
+ existing PRIDE SDRF (if any)
+ existing PRIDE_SCP Stage04 annotations
+ manuscript-derived semantic evidence
+ extracted manuscript text when available
        ↓
provenance-labelled evidence packet
        ↓
Ollama structured fact proposal
        ↓
deterministic Rust row construction
        ↓
local SDRF/template checks
        ↓
SDRF draft + audit + unresolved-review TSV
```

This prevents the model from silently inventing sample-to-file mappings or filling unsupported metadata.

## Evidence inputs

For each accession, the command reads:

```text
data/snapshot/projects/PXDxxxxxx.json
data/snapshot/files/PXDxxxxxx.json
data/snapshot/sdrf/PXDxxxxxx.sdrf.tsv        # when present
work/python/pride_scp_annotations/annotations/PXDxxxxxx/**.json
work/python/pride_candidate_publications_with_content.tsv
```

Stage04 semantic-evidence files referenced by annotation provenance are also read. These are manuscript-derived and are useful when the upstream publication artifact is a PDF.

Additional extracted manuscript text/HTML/XML can be passed with repeated `--manuscript-text` arguments.

### PDF handling

The Rust SDRF lane does not add a second PDF parser. Direct `.pdf` paths are skipped and recorded through upstream Stage04 semantic evidence instead. Prefer the existing publication extraction pipeline or pass an extracted text file explicitly.

## RAW file selection

PRIDE file records with `fileCategory.value == RAW` are used. Raw-like extensions are retained as a fallback when category metadata is incomplete.

The full RAW inventory is used deterministically even when only a bounded sample of filenames is shown to Ollama.

## Sample↔file mapping safety

Ollama must classify the experimental relationship as one of:

```text
one_cell_per_data_file
multiplexed_cells_per_data_file
mixed
uncertain
```

Only `one_cell_per_data_file` permits the v0.1 implementation to derive a cell identifier from the RAW filename stem and emit a potentially complete one-row-per-cell draft.

For multiplexed/mixed/uncertain designs, the command emits an explicit **incomplete skeleton** and a review report. It does not hallucinate TMT channel-to-cell relationships.

A later iteration can add evidence-grounded multiplex layout reconstruction after this first lane has been tested on representative SCP datasets.

## Generated columns

The initial draft includes the SDRF core MS fields:

```text
source name
characteristics[organism]
characteristics[organism part]
characteristics[disease]
characteristics[cell type]
characteristics[biological replicate]
assay name
technology type
comment[proteomics data acquisition method]
comment[label]
comment[instrument]
comment[cleavage agent details]
comment[fraction identifier]
comment[technical replicate]
comment[data file]
comment[file uri]                         # when available
```

and the pinned single-cell fields:

```text
characteristics[sample type]
characteristics[single cell isolation protocol]
characteristics[cell identifier]
characteristics[individual]
comment[sample preparation batch]
characteristics[cells per well]
comment[carrier channel]
comment[reference channel]
```

plus SDRF provenance metadata. Ollama may propose candidate study factors for review, but v0.1 deliberately does **not** serialize factor-value columns because per-row factor assignments have not yet been reconstructed safely.

## Reserved values and unknowns

The core specification reserves values such as `not available`, `not applicable`, and `pooled`.

The generator prefers an unresolved/invalid draft over a semantically incorrect replacement. In particular, the single-cell template does not permit `not available` for some required fields. Such a row remains a draft and is reported in the review TSV rather than being disguised as valid.

## Output layout

For output root `data/sdrf_annotation`:

```text
evidence/PXDxxxxxx.evidence.json
proposals/PXDxxxxxx.ollama.json
sdrf/PXDxxxxxx.sdrf.tsv
review/PXDxxxxxx.sdrf.review.tsv
audit/PXDxxxxxx.sdrf.audit.json
sdrf_annotation_results.tsv
sdrf_annotation_summary.json
```

The audit records source counts, relation mode, generator/template versions, validation failures, and whether an existing SDRF was already present.

## Usage

One accession:

```bash
target/release/pride-scp sdrf-annotate \
  --accession PXD049412 \
  --snapshot data/snapshot \
  --annotations-dir work/python/pride_scp_annotations/annotations \
  --publication-manifest work/python/pride_candidate_publications_with_content.tsv \
  --output data/sdrf_annotation/PXD049412 \
  --model qwen2.5:3b
```

A curated accession list:

```bash
target/release/pride-scp sdrf-annotate \
  --accessions-file work/sdrf/pride_scp_accessions.txt \
  --snapshot data/snapshot \
  --annotations-dir work/python/pride_scp_annotations/annotations \
  --publication-manifest work/python/pride_candidate_publications_with_content.tsv \
  --output data/sdrf_annotation \
  --model qwen2.5:3b
```

Add extracted manuscript text:

```bash
target/release/pride-scp sdrf-annotate \
  --accession PXD049412 \
  --manuscript-text /path/to/manuscript_extracted.txt \
  --output data/sdrf_annotation/PXD049412
```

## Validation

The Rust stage performs deterministic format/template preflight checks. Before any SDRF is treated as submission-ready, also validate with the official `sdrf-pipelines` tooling:

```bash
pip install sdrf-pipelines
parse_sdrf validate-sdrf --sdrf_file PXDxxxxxx.sdrf.tsv
```

Template-specific validation should be rerun against the live single-cell template because that template is still evolving.

## v0.1 acceptance criterion

The first smoke is successful when a representative label-free / one-cell-per-RAW SCP dataset produces:

- a provenance evidence packet;
- a structured Ollama proposal whose evidence refs all resolve;
- one deterministic SDRF row per PRIDE RAW file;
- no invented required metadata;
- a review file that clearly exposes any unresolved fields;
- a locally-valid draft when evidence is actually sufficient.

Multiplexed designs are expected to remain incomplete in v0.1 until channel↔cell mapping support is implemented.

## v0.1.1 batch robustness

`pride-scp-sdrf-v0.1.1` fixes the first live smoke finding:

- `relation_mode=uncertain` is a non-assertive fallback and therefore does not require an evidence reference;
- asserted relation modes (`one_cell_per_data_file`, `multiplexed_cells_per_data_file`, `mixed`) still require provenance;
- parsed Ollama proposals are persisted before evidence-reference validation, so a failed item remains diagnosable;
- batch runs remain accession-isolated: one error is recorded under `errors/` and does not abort other accessions;
- rerunning without `--force` reuses successful v0.1.1 audits while retrying accessions that never reached an audit.

### Frozen GT106 PRIDE reconstruction cohort

For the specific reconstruction experiment on the 106 known PRIDE SCP accessions:

```bash
./scripts/run_gt106_pride_sdrf_annotation.sh
```

The wrapper reads the frozen GT master only to derive the 106 PRIDE accession identifiers. It writes the selected cohort and a machine-readable provenance statement into the output directory. GT labels, GT curation annotations, canonical-study mappings, and GT metadata are not passed to SDRF generation.

This is appropriate for reconstructing SDRFs for a known evaluation/reference cohort; it must not be confused with GT-independent catalogue discovery.


## v0.1.2 provenance-repair behavior

Batch testing across the GT106 PRIDE cohort showed that small local LLMs frequently return a plausible metadata value without populating its `evidence_refs`, or emit placeholder refs such as `E0000`. These are model-output quality issues and must not turn an otherwise recoverable accession into a pipeline error.

`pride-scp-sdrf-v0.1.2` therefore separates **proposal repair** from **draft validation**:

- the raw Ollama JSON is preserved as `proposals/<PXD>.ollama.raw.json`;
- evidence refs not present in the accession evidence packet are removed and logged;
- common placeholder strings (`NA`, `N/A`, `unknown`, `default`, `not specified`, etc.) are normalized to `not available`;
- an asserted field with no surviving provenance is downgraded to `not available`;
- an unsupported asserted `relation_mode` is downgraded to `uncertain`;
- unsupported factor values are downgraded rather than trusted;
- the repaired proposal is written as `proposals/<PXD>.ollama.json`;
- every repair appears as a warning in the review/audit output;
- structural/template incompleteness remains a validation error in the SDRF draft, but does not abort the accession.

This is intentionally fail-safe: unsupported model assertions are discarded, never silently accepted. True operational failures (Ollama timeout, malformed JSON, unreadable source files) remain accession errors.
