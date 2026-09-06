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

## v0.2.0 existing-SDRF-first evidence architecture

The v0.1.2 smoke showed a deeper failure mode: all five representative accessions already
had deposited SDRFs, but the generator flattened those files into generic LLM evidence and
then regenerated a new skeleton. Repository file-record metadata also leaked into the prompt,
causing values such as `PRIDE`, `fileCategory`, and `publicFileLocations` to be proposed as
biology.

`pride-scp-sdrf-v0.2.0` changes the architecture rather than adding more post-hoc cleanup:

1. **Existing SDRF is first-class structured input.** Headers and rows are parsed as TSV.
   Existing sample-to-file relationships and deposited core MS values are preserved.
2. **Single-cell enrichment is additive.** Missing single-cell columns are appended and only
   missing/reserved cells are candidates for evidence-grounded completion.
3. **Repository transport metadata is not LLM evidence.** The file manifest is used
   deterministically for RAW inventory and URIs, but fields such as `fileCategory`,
   `publicFileLocations`, repository labels, and download URLs are excluded from metadata
   extraction prompts.
4. **Existing SDRF values are summarized structurally.** The model sees bounded unique values
   for relevant SDRF columns and a deterministic relationship summary rather than raw TSV rows.
5. **Existing SDRF relationship hints can override the model.** When deposited rows prove
   one-cell-per-file or multiplexed sample-to-file structure, the deterministic relation is
   authoritative and is recorded as a provenance repair warning.
6. **The correct SDRF metadata column is used:** `comment[sdrf annotation tool]`.
7. **Sample type stays ontology/CV-oriented.** The local preflight no longer treats values
   outside a small hard-coded list as fatal; uncommon values are deferred to the official
   `sdrf-pipelines` ontology validation.
8. Prompt size defaults are reduced for small local models: 128 evidence items, 60k evidence
   characters, and 64 RAW filenames. The complete RAW inventory is still used deterministically.

### Existing SDRF merge behavior

For a deposited SDRF, the output preserves existing values for organism, source/assay names,
acquisition, label, instrument, cleavage, fraction, technical replicate, and data-file mapping.
The generator may fill missing dataset-level values only when the Ollama proposal is
provenance-backed. It may derive a cell identifier from an existing `source name` only when
existing SDRF structure/provenance establishes a one-cell-per-file relationship or the row is
already explicitly marked `single cell`.

Carrier/reference/control rows are not forced into cell identifiers. When their role is already
known, the cell identifier/isolation concepts use the applicable reserved values rather than
inventing a cell.

### Recommended v0.2 smoke

First rerun the five accessions that exposed the v0.1 evidence problem. A successful v0.2 smoke
should show `generation_mode=enriched_existing_sdrf` and substantially fewer repeated core-MS
validation errors because the deposited SDRF values are preserved.

Then separately test accessions with **no deposited SDRF**. This is important because existing
SDRF enrichment and de-novo SDRF reconstruction are distinct scientific problems. Generate the
missing-SDRF list from the GT106 cohort locally and smoke-test a few of those before starting the
full 106-accession batch.

## v0.2.1: field-targeted extraction and GT106 cohort modes

The v0.2.0 existing-SDRF smoke established that preserving deposited SDRF rows is
structurally correct, but also showed that a small LLM can still attach a valid
evidence reference to the wrong semantic field (for example proposing MaxQuant as
a single-cell isolation method). v0.2.1 therefore tightens the proposal layer
without weakening provenance:

- only fields that are missing in at least one deposited SDRF row are requested
  from Ollama;
- fields already represented by the deposited SDRF are locked and model values
  for those fields are discarded;
- evidence is shown to the model in field-specific sections rather than one flat
  evidence pool;
- evidence references are retained only when the cited evidence is relevant to
  that field;
- obvious category errors (analysis software as isolation method, instrument, or
  acquisition method; non-integer fraction/technical replicate values) are
  deterministically downgraded to unresolved;
- factor proposals are deferred until per-row factor mapping is implemented;
- a deposited SDRF with zero data rows can no longer pass local validation.

The GT106 helper supports three accession-selection modes. GT remains cohort-only
and never provides SDRF values:

```bash
COHORT_MODE=all ./scripts/run_gt106_pride_sdrf_annotation.sh
COHORT_MODE=existing-sdrf ./scripts/run_gt106_pride_sdrf_annotation.sh
COHORT_MODE=missing-sdrf ./scripts/run_gt106_pride_sdrf_annotation.sh
```

To inspect the cohort split without starting Ollama:

```bash
LIST_ONLY=1 COHORT_MODE=missing-sdrf \
  ./scripts/run_gt106_pride_sdrf_annotation.sh
```

Each mode writes both the complete 106-accession list and the selected list, plus
a JSON summary containing the number with and without deposited SDRFs.

## v0.2.2: source-truth cohort resolution and strict existing-SDRF preservation

The v0.2.1 smoke exposed two structural facts that change the next step:

1. an accession can be labelled PRIDE in the frozen evaluation table while current
   source-derived ProteomeXchange metadata resolves it to another hosting repository;
2. an existing SDRF must never be silently replaced by a filename-derived skeleton if
   parsing/enrichment fails.

v0.2.2 therefore makes these rules explicit:

- the GT106 wrapper still uses GT only as the starting accession cohort, but resolves
  runtime PRIDE scope from the primary PRIDE snapshot and the source-derived
  `registry/projects/<PXD>.json` hosting repository field;
- GT repository labels are never runtime source truth;
- non-PRIDE/unresolved accessions are written to
  `gt106_non_pride_or_unresolved_accessions.tsv` and are deferred rather than causing a
  PRIDE SDRF error;
- every selected accession is written to
  `gt106_pride_sdrf_source_inventory.tsv` with primary-PRIDE presence, SDRF presence,
  and a content-derived SDRF source-class heuristic;
- `community_curated`, `agentic`, `pride_scp_generated`, and
  `repository_or_unknown` source classes are inferred only from
  `comment[sdrf annotation tool]`; this is an audit hint, not authoritative repository
  provenance;
- if an existing SDRF is detected, merge/parsing failure is now fatal for that
  accession. The generator never falls back to `generated_skeleton_unresolved_mapping`;
- SDRF rows are checked for header/row-width consistency;
- an existing SDRF with no missing dataset-level target fields skips Ollama entirely and
  is preserved/validated deterministically.

This phase intentionally separates **SDRF source auditing** from **de-novo SDRF
reconstruction**. Community-curated SDRFs should normally be preserved and validated,
not regenerated by a small LLM merely because the original submitter did not provide
an SDRF at deposition time.

## v0.2.3: distinguish cached SDRF responses from usable SDRFs

The PRIDE `/files/sdrf/{accession}` endpoint can return a non-empty response even when
no actual sample/data rows are present. Earlier snapshot runs already warned that cached
SDRF responses might be API wrappers or empty templates. The v0.2.2 smoke confirmed this
for the five tested SCP accessions: each cached file existed but contained only a header.

v0.2.3 therefore separates **snapshot SDRF file presence** from **usable SDRF presence**.
A cached SDRF is usable only when it parses successfully, contains at least one non-empty
data row, contains `comment[data file]`, and has at least one concrete mapped data-file
value. Header-only, unreadable, unmapped, or missing-data-file-column responses are not
fed to the preservation/enrichment path and cannot block de-novo reconstruction.

The GT106 wrapper now reports `sdrf_status`, `sdrf_usable`, snapshot-file counts, usable
existing-SDRF counts, and unusable snapshot-SDRF counts. `COHORT_MODE=missing-sdrf`
means **missing or unusable SDRF**, not merely missing cache file.
