# PRIDE_SCP single-cell SDRF annotation lane

## Status

Current implementation: `pride-scp sdrf-resolve` + `pride-scp sdrf-annotate` (`pride-scp-sdrf-v0.2.4`).

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


## v0.2.4 external SDRF source resolver/cache

The v0.2.3 GT106 inventory established that all 105 source-resolved primary-PRIDE
SCP accessions had a local `data/snapshot/sdrf/<PXD>.sdrf.tsv` file, but every one
was `header_only`. The local PRIDE SDRF API cache is therefore not an authoritative
inventory of usable SDRF annotations for this cohort.

v0.2.4 adds a separate source-resolution phase:

```text
pride-scp sdrf-resolve
```

The resolver is intentionally separate from Ollama annotation. It resolves and caches
available SDRF sources first, records their provenance, and writes one selected usable
SDRF per accession under `resolved/`.

Trust/selection order in resolver v0.1:

1. `curated_bigbio` — community-curated SDRF from
   `bigbio/sdrf-annotated-datasets/datasets/{ACCESSION}/{ACCESSION}.sdrf.tsv`;
2. `repository_submitted` — an actual `.sdrf.tsv` file listed in the repository file
   manifest and downloadable through a public URL;
3. `snapshot_pride_sdrf_api` — only when the cached API response itself has at least
   one mapped `comment[data file]` row.

HAMLET/agentic SDRFs are **not** automatically selected in this first resolver. They can
be evaluated later as an explicitly lower-trust source.

A candidate source is considered usable only after local TSV parsing confirms at least
one real data row and at least one mapped `comment[data file]` value. HTML error pages,
header-only payloads, and unmapped SDRFs are cached/audited but never selected.

Per-accession output:

```text
cache/<PXD>/curated_bigbio.sdrf.tsv
cache/<PXD>/repository_01.sdrf.tsv        # when discovered
resolved/<PXD>.sdrf.tsv                   # selected usable source only
audit/<PXD>.sdrf_source_audit.json
```

Batch outputs:

```text
sdrf_source_resolution.tsv
sdrf_source_resolution_summary.json
```

Each audit records the source URL, parse/usability status, byte count, and deterministic
FNV-1a content fingerprint. The fingerprint is an integrity/reproducibility marker, not a
cryptographic security hash.

### Source-resolver usage

```bash
target/release/pride-scp sdrf-resolve \
  --accessions-file work/sdrf/pride_scp_accessions.txt \
  --snapshot data/snapshot \
  --output data/sdrf_sources
```

For the source-resolved primary-PRIDE portion of the frozen GT106 cohort:

```bash
./scripts/run_gt106_pride_sdrf_source_resolution.sh
```

GT is used by that wrapper only to seed the known accession cohort. Hosting-repository
resolution and all SDRF source selection are source-derived.

### Annotation after source resolution

`pride-scp sdrf-annotate` now accepts:

```text
--resolved-sdrf-dir data/sdrf_sources/resolved
```

A usable SDRF in that directory takes precedence over the header-only PRIDE API cache.
The annotator then preserves/audits/enriches that real SDRF. Accessions for which the
resolver finds no usable source continue through the existing manuscript-assisted de-novo
path.

For the GT-seeded wrapper, set:

```bash
RESOLVED_SDRF_DIR=data/sdrf_source_resolution_gt105_pride_v024/resolved \
  ./scripts/run_gt106_pride_sdrf_annotation.sh
```

Do not run the full annotation batch until the source-resolution inventory is reviewed.
The immediate goal is to quantify how many of the 105 source-resolved PRIDE datasets are
already covered by curated/repository SDRFs and how many genuinely require de-novo work.

## Deterministic resolved-SDRF audit (v0.2.5)

After `sdrf-resolve`, do **not** send every resolved SDRF back through Ollama. The
`sdrf-audit` subcommand validates the selected local SDRF exactly as resolved,
compares its `comment[data file]` values against the PRIDE RAW inventory, derives
its one-cell/multiplexed relationship where possible, and reports the dataset-level
fields that would still need bounded enrichment. It never modifies the SDRF and
never invokes Ollama.

For the GT-seeded source-resolved PRIDE cohort, use:

```bash
./scripts/run_gt105_pride_sdrf_audit.sh
```

The wrapper also materializes `resolved_accessions.txt` and
`unresolved_accessions.txt` directly from `sdrf_source_resolution.tsv`. The latter
is the only cohort eligible for de-novo manuscript-assisted reconstruction unless
an audited resolved SDRF is explicitly selected for gap filling.

## v0.2.6 resolved-SDRF audit semantics: separate SDRF validity from repository linkage

The first deterministic audit of the 67 resolved SDRFs reported 49 locally valid and
18 locally invalid. Inspection of the invalid pattern showed that many failures were
one error per SDRF row while the local PRIDE RAW inventory contained far fewer top-level
files. This is characteristic of repository packaging/linkage differences (for example
logical Bruker `.d` acquisitions versus archives/containers), not necessarily malformed
SDRF metadata.

The auditor therefore no longer treats a resolved external SDRF as schema-invalid merely
because `comment[data file]` is not represented one-to-one in the local PRIDE RAW snapshot.
For resolved-SDRF audits:

- core/template validation errors remain errors;
- PRIDE snapshot file-linkage mismatches are warnings;
- linkage is reported independently as `complete`, `partial`, `none`, or `unavailable`;
- common archive wrappers (`.tar.gz`, `.tgz`, `.zip`, `.tar`) are normalized when comparing
  logical vendor entities to repository files;
- the original generated-SDRF path remains strict: a newly generated SDRF must map its data
  files back to the PRIDE inventory.

The relationship classifier is also role-aware. If `characteristics[sample type]` exists,
`one_cell_per_data_file` versus `multiplexed_cells_per_data_file` is inferred from the
`single cell` rows first. Pooled controls, carrier/reference material, secretome controls,
and other non-single-cell rows do not make the single-cell branch look multiplexed merely
because they share a RAW file or use multiple labels.

Single-cell-specific validation is role-aware as well. Explicit carrier/reference/control/
pooled rows may use unresolved/not-applicable cell-specific values without being treated as
single-cell study-row errors; study/single-cell rows remain strict.

The v0.2.6 auditor also separates metadata gaps into bounded enrichment priorities:

```text
P0_structural_review  local core/template error remains
P1_blocking_gap       relationship/core annotation gap that should be resolved first
P2_recommended_gap    recommended SCP metadata can be enriched
P3_optional_gap       optional metadata only (for example individual)
P4_preserve           no current target-field gap
```

Carrier/reference channel gaps are context-sensitive and are not reported for label-free or
non-isobaric experiments. `individual` is tracked as optional rather than making every SDRF
require review.

The audit output now includes separate repository linkage metrics and priority counts. Local
`locally_valid` means the Rust structural/template checks passed **excluding repository-file
linkage**. It is still not a substitute for final `sdrf-pipelines`/PRIDE SDRF Validator
validation before submission.

## Bounded de-novo baseline pilot after resolved-SDRF audit

After the v0.2.6 deterministic audit, the resolved-SDRF lane is held stable while the
source-unresolved PRIDE subset is evaluated separately. Before changing prompts or
reconstruction logic again, run a five-accession de-novo baseline:

```bash
./scripts/run_gt105_pride_sdrf_denovo_pilot.sh
```

The pilot is intentionally heterogeneous:

- `PXD001641` — early single-muscle-fiber proteomics;
- `PXD004174` — label-free single Xenopus blastomeres;
- `PXD028040` — patch-clamp single-neuron proteomics;
- `PXD029320` — multiplexed TMT single-cell proteomics;
- `PXD042367` — single/few-cell spatial tissue proteomics.

These accessions must already be present in
`data/sdrf_audit_gt105_pride_v026/unresolved_accessions.txt`; the wrapper aborts if the
accepted source-resolution/audit state no longer classifies any pilot accession as
unresolved. GT remains an evaluation/cohort seed only and is never used for SDRF field
values.

Do not tune the generator until the five-accession baseline has been inspected. The
baseline should establish which failures are due to RAW/container discovery,
relationship inference, missing manuscript evidence, field provenance, or genuinely
unresolved multiplex channel-to-cell mapping.

## v0.3.0 de-novo study-design scaffold

The five-accession v0.2.7 de-novo baseline completed operationally but every study fell
back to `relation_mode=uncertain` and a generic one-row-per-repository-file skeleton.
The baseline also showed that the small model often located useful manuscript prose but
failed to attach field-level provenance, causing concrete values to be downgraded.

v0.3.0 moves experiment structure upstream of the LLM. For source-unresolved/de-novo
studies, Rust now derives and records a `study_design` scaffold inside the evidence JSON:

```text
relation_mode_hint
relation_confidence
relation_evidence_refs
repository_file_mode
direct_acquisition_files
wrapped_acquisition_files
generic_archive_files
generic_archive_file_names
notes
```

The relation scaffold is intentionally conservative and source-derived. It recognizes:

- single-cell + TMT/TMTpro/iTRAQ/carrier evidence as multiplexed;
- explicit single-cell + few-cell/small-pool evidence as mixed;
- single-cell + label-free evidence as one-cell-per-data-file;
- specific single muscle-fiber/blastomere/neuron/oocyte evidence without multiplex
  evidence as a medium-confidence one-cell-per-data-file hint;
- everything else as uncertain.

A non-uncertain relation hint is applied deterministically after Ollama provenance repair
and carries the evidence refs that triggered the rule. GT labels are never consulted.

Repository file structure is classified separately. Direct RAW/vendor files and wrapped
vendor acquisitions such as `.d.zip` are distinguished from generic `.rar`/`.zip`/archive
containers. Generic archives remain visible in the draft/review, but the accession is
explicitly marked `incomplete_repository_archive_contents_mapping` when those archives are
the only repository-side files; their names are not treated as proof of one acquisition
per archive.

v0.3.0 also adds bounded provenance recovery for small-model outputs. If a concrete
proposed value has no valid refs, Rust may retain it only when that exact value occurs
verbatim in evidence already classified as relevant to the same field. The recovered
refs and repair are written to the review TSV. This does not permit semantic borrowing
between fields and does not apply to numeric identifiers or relation mode.

Semantic guards were tightened at the same time:

- capillary electrophoresis alone is not an MS acquisition method;
- analysis software remains invalid as isolation/acquisition/instrument metadata;
- carrier/reference channel values must actually describe carrier/reference channels;
- microaspiration, patch-clamp-guided sampling, micropipette/capillary microsampling,
  manual dissection and microdissection are recognized as possible single-cell isolation
  evidence.

For deterministic `one_cell_per_data_file` drafts, `comment[fraction identifier]` and
`comment[technical replicate]` default to `1` when no numeric value is supplied, because
each generated source is a unique single-cell acquisition in that specific mapping mode.
Multiplexed/mixed/uncertain designs remain unresolved until channel/sample structure is
reconstructed.

Run the same frozen five-accession comparison cohort with:

```bash
./scripts/run_gt105_pride_sdrf_denovo_scaffold_pilot.sh
```

The important comparison against v0.2.7 is the study-design scaffold and relation mode,
not simply the number of locally-valid SDRFs. The expected architectural outcome is that
label-free single-cell studies can advance to one-cell-per-file drafts, TMT studies stay
explicitly multiplexed, mixed/few-cell studies stay mixed, and generic repository archives
are flagged as containers instead of silently being treated as biological runs.

## v0.3.1 template-aware metadata scaffold and mapping-blocker validation

The v0.3.0 five-study pilot successfully fixed the experiment-cardinality problem:

```text
PXD001641  one_cell_per_data_file
PXD004174  one_cell_per_data_file
PXD028040  multiplexed_cells_per_data_file
PXD029320  multiplexed_cells_per_data_file
PXD042367  multiplexed_cells_per_data_file + generic_archives_only
```

That run also showed that the remaining failures are downstream of relation inference. The
one-cell studies were blocked primarily by `characteristics[single cell isolation protocol]`,
while multiplexed studies emitted large row-by-row validation error counts even though the
real blocker is a single unresolved channel/sample mapping problem. Generic `.rar` containers
have the same issue: their contents must be resolved before final SDRF rows exist.

v0.3.1 keeps the v0.3.0 study-design logic and adds a deterministic `metadata_scaffold` to the
evidence/audit JSON. The scaffold only fills strongly source-supported facts and records the
supporting E#### refs. Current deterministic facts include:

- structured PRIDE project organism and instrument values when a human-readable source field is
  available;
- label-free annotation for explicitly label-free, non-multiplexed studies;
- DDA/DIA acquisition mode when explicitly stated;
- trypsin cleavage when explicitly stated;
- template-supported single-cell isolation methods derived from explicit methods evidence.

Manuscript keyword windows now include isolation-specific terms such as isolation, dissection,
tweezers, manual picking, microaspiration, patch clamp, micropipette, capillary microsampling,
laser capture, and microdissection. This is important for older manuscripts whose relevant
methods paragraphs do not repeat generic `single-cell proteomics` keywords.

### Template-aware isolation policy

The pinned single-cell 1.0.0 isolation vocabulary is treated as a representation constraint,
not as permission to invent an approximately similar method. v0.3.1 may map clearly manual
single-cell dissection/picking evidence to `manual picking`, and recognizes template-supported
FACS, cellenONE, microfluidics, laser capture microdissection, nanoPOTS, droplet microfluidics,
and acoustic droplet ejection.

If the experimental evidence instead supports a real method that the pinned template cannot
represent faithfully (for example patch-clamp-guided microaspiration or capillary microsampling),
Rust leaves the field unresolved and writes a `template_vocabulary_gap_supported_method` warning
with the observed method and evidence refs. It must not substitute `manual picking` merely to make
the validator green.

### Multiplex design scaffold

For multiplexed studies `study_design` now also records:

```text
multiplex_chemistry_hint
multiplex_evidence_refs
carrier_channel_hints
reference_channel_hints
multiplex_mapping_status
```

Channel-role hints are extracted only when a carrier/reference phrase and an explicit TMT-style
channel number occur together in source evidence. These hints are diagnostics; v0.3.1 still does
not fabricate per-cell channel rows without a defensible channel-to-sample map.

### Validation of intentionally incomplete mapping scaffolds

Multiplexed/mixed studies and generic archive-only studies do not yet contain final SDRF rows.
The previous validator therefore produced misleading error storms (for example three errors per
RAW placeholder row). v0.3.1 validates only the structural scaffold in these modes and emits one
explicit dataset-level blocking error:

```text
sample_to_channel_mapping_unresolved
repository_archive_contents_mapping_unresolved
```

Strict row-level core/template validation is unchanged for one-cell-per-file drafts and for final
resolved/existing SDRFs.

Run the frozen five-study comparison with:

```bash
./scripts/run_gt105_pride_sdrf_denovo_template_aware_pilot.sh
```

The wrapper deliberately reduces prompt context to 96 evidence items / 45k evidence characters /
32 repository file names because relation structure and several metadata fields are now resolved
deterministically. This is intended to reduce the ~49 minute v0.3.0 pilot runtime without dropping
the newly expanded methods evidence.

The desired result is not that every study becomes validator-clean. Instead:

- the two one-cell studies should either become locally valid or expose a single genuine/template
  metadata blocker;
- multiplexed studies should report one mapping blocker rather than hundreds of placeholder-row
  errors;
- unsupported real isolation methods should be identified as template gaps rather than coerced;
- generic archives should remain explicitly unresolved until their internal acquisition mapping is
  available.

## Post-full-run recovery triage (v0.3.1 linkage-policy hotfix)

A completed annotation batch should be triaged before additional reconstruction work.
Resolved/community SDRFs and newly generated SDRFs deliberately use different repository-file
validation policies:

- **resolved/existing SDRF**: logical `comment[data file]` values are preserved; mismatches against
  the local PRIDE snapshot are repository-linkage **warnings** (`AuditSnapshotInventory`), not schema
  errors;
- **PRIDE-SCP generated SDRF**: asserted data-file mappings remain strict and must link to the local
  PRIDE inventory (`EnforceSnapshotInventory`).

This matches the standalone `sdrf-audit` semantics and prevents enrichment from making a previously
valid curated SDRF invalid solely because the local PRIDE manifest exposes an archive/container or
otherwise incomplete filename inventory.

Use the generic post-run triage helper to split a completed annotation run into recovery lanes:

```bash
python scripts/sdrf_postrun_triage.py \
  --annotation-results data/sdrf_annotation/sdrf_annotation_results.tsv \
  --resolved-audit-results data/sdrf_audit/sdrf_audit_results.tsv \
  --output data/sdrf_recovery_triage
```

The helper writes `sdrf_recovery_triage.tsv`, a JSON summary, and one accession list per lane:

- `ready`
- `resolved_linkage_validation_regression`
- `existing_structural_review`
- `denovo_required_metadata`
- `denovo_template_gap`
- `denovo_mapping`
- `denovo_archive`
- `other_incomplete`

The triage tool is GT-agnostic; evaluation cohorts may be supplied externally, but no GT metadata is
used to classify or repair SDRF content.

## v0.3.2: required-metadata rescue and file-role-aware de-novo rows

The accepted v0.3.1 full PRIDE cohort plus the v0.3.1.2 linkage-policy repair left six de-novo accessions in the `incomplete_required_metadata` lane. Review of those studies showed that this lane contained two distinct problems rather than a single generic missing field:

1. Methods/isolation evidence could occur late in a manuscript and be omitted after broad `proteom` keyword windows consumed the evidence budget.
2. Some deposits contain true single-cell acquisitions alongside few-cell, bulk-equivalent, blank or QC runs. A dataset-wide `one_cell_per_data_file` hint must not force every repository acquisition to `sample type = single cell`.

v0.3.2 therefore:

- selects manuscript evidence in priority order: isolation/sample-handling windows, sample-design windows, acquisition windows, then broad proteomics context;
- expands conservative synonym mapping for manual dissection/picking (for example fine-tweezer single-fiber dissection and blastomere microdissection);
- records a template compatibility gap rather than inventing a vocabulary term when evidence supports a microwell-chip transfer procedure that the pinned isolation vocabulary cannot faithfully encode;
- classifies obvious repository filenames as `single_cell`, `few_cell`, `blank`, `quality_control`, `bulk`, or `unknown`;
- retains one-row-per-acquisition mapping where appropriate but assigns non-single rows explicit SDRF roles (`study sample`, `empty`, `quality control sample`, `bulk control`) instead of mislabelling them as single cells;
- records `study_design.file_role_hint_counts` in every audit for transparent review.

This is intentionally a bounded de-novo change. It does not alter resolved external SDRF preservation semantics and does not infer a biological sample role from opaque filenames when no safe pattern is present.

## v0.3.3: full-manuscript deterministic metadata scan

The v0.3.2 required-metadata rescue exposed a source-selection bug: manuscript keyword windows were prioritized correctly, but the manuscript reader was still truncated using the same `--max-evidence-chars` budget that limits the final evidence packet sent to Ollama. As a result, late Methods sections could never be searched when they occurred after the first ~60k characters of extracted full text.

v0.3.3 separates **source scanning** from **prompt/evidence budgeting**:

- extracted manuscript text is scanned up to a bounded 2,000,000 characters;
- isolation/sample-design/acquisition keyword windows are selected from that larger source view;
- only the selected windows enter the normal bounded evidence packet;
- `--max-evidence-items` and `--max-evidence-chars` still bound Ollama input and serialized evidence;
- direct PDF parsing remains out of scope; upstream extracted manuscript text remains authoritative.

This change is designed to recover late Methods evidence such as manual/tweezer single-fiber isolation, blastomere microdissection, or CellenONE cell sorting without increasing the small-LLM context window. It does not relax provenance rules and does not convert an unsupported real isolation procedure into a false template vocabulary value.

Run the residual required-metadata lane with:

```bash
./scripts/run_gt105_pride_sdrf_fulltext_required_metadata_rescue.sh
```

The helper derives its accession list from the v0.3.2 results and therefore only reruns accessions still marked `incomplete_required_metadata`.

## v0.3.4: reserve de-novo evidence budget for direct manuscript Methods windows

The v0.3.3 five-accession residual rescue still produced empty deterministic isolation scaffolds even after scanning up to 2,000,000 manuscript characters. Inspection showed that the manuscript loop could be starved before it ran: project metadata plus existing annotation/semantic JSON could fill the entire `max_evidence_items` / `max_evidence_chars` packet.

v0.3.4 separates *source priority* from *prompt size* for de-novo datasets. When direct manuscript sources are available and there is no usable existing SDRF, a bounded quarter of the evidence packet (capped at 24 items and 12,000 characters) is reserved for manuscript keyword windows. Existing/resolved SDRFs keep the full preservation-first evidence budget unchanged.

The manuscript scan itself remains bounded at 2,000,000 characters and the Ollama packet remains bounded by the user-provided evidence limits. The new rescue wrapper prints manuscript-source and manuscript-evidence-item counts so routing failures are directly observable.

## PDF-backed publication text materialization (v0.3.5 pipeline integration)

The SDRF Rust crate intentionally does not parse PDFs.  Publication-content Stage 03 now
materializes normalized text for validated PDF-backed rows and records that path in
`publication_content_text_path`, while retaining the PDF itself as the canonical
`publication_content_path` used by the established Stage-04 annotation pipeline.

This closes an interface gap where a publication was available as a PDF but the SDRF
manuscript scanner saw zero usable text sources.  PDF extraction is performed upstream
with local fallbacks (PyMuPDF, `pdftotext`, then `pypdf`); OCR is still outside this
path and must remain an explicit/manual operation when ordinary text extraction fails.

For bounded recovery runs, Stage 03 accepts `--accessions-file` so only the requested
accessions are re-materialized.  The original publication manifest is never modified;
a derived manifest is written and supplied to `pride-scp sdrf-annotate`.

## v0.3.6: match-centered manuscript windows and aligned isolation relevance

The v0.3.5 publication-text rescue proved that direct manuscript text reached the SDRF evidence
packet for four of five residual studies, but the deterministic isolation scaffold still remained
empty. Two generic issues were responsible:

1. PDF-to-text output often contains long single-newline blocks instead of blank-line-separated
   paragraphs. The previous keyword-window code could detect a Methods keyword anywhere in such a
   block, then clip the *start* of the block and lose the actual matched phrase.
2. The deterministic manual-isolation mapper recognized cues such as `using tweezers` and
   `individually transferred`, but the field-relevance gate did not consistently recognize the same
   vocabulary, so valid evidence could be rejected before reaching the mapper.

v0.3.6 centers each manuscript evidence window on the actual regex match and normalizes both CR and
form-feed separators. It also aligns the isolation relevance vocabulary with the deterministic
extractor. `manual picking` remains conservative: an explicit manual action/instrument cue such as
`tweezers`, manual/micro-dissection, or mechanical dissociation plus individual transfer is required.
A generic statement that a fiber was merely "taken" or "isolated" is not enough to assert a manual
picking protocol.

The change does not increase the Ollama evidence budget and reuses the v0.3.5 materialized
publication-text manifest:

```bash
./scripts/run_gt105_pride_sdrf_isolation_context_rescue.sh
```

The wrapper prints the deterministic isolation value, evidence refs, manuscript-source counts and up
to three isolation-relevant manuscript excerpts per accession. This makes it possible to distinguish
remaining template/evidence limitations from routing or window-extraction failures without another
broad generator iteration.
