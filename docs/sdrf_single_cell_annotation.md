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

## v0.4.0: mapping-evidence audit before multiplex row reconstruction

The v0.3.6 isolation-context rescue closes the generic required-metadata lane as an architecture
problem. The accepted cohort state after combining the v0.3.1.2 full-run repair, the v0.3.2 rescue,
and the v0.3.6 rescue is:

```text
68 locally valid / ready
17 sample/file/channel mapping
13 repository archive-content mapping
 6 existing-SDRF structural review
 1 source-limited required metadata
---
105 primary PRIDE accessions
```

The mapping lane is intentionally audited before generating multiplex rows. A valid multiplex SDRF
may require multiple biological rows to share a RAW file, with reporter-channel labels identifying
the actual single-cell samples and `comment[carrier channel]` / `comment[reference channel]`
recording set-level roles. Generating those rows from chemistry alone would fabricate sample
identities.

`scripts/sdrf_mapping_evidence_audit.py` is therefore a non-generative evidence auditor. It:

- inventories explicit TMT/TMTpro reporter-channel mentions from existing SDRF evidence packets;
- additionally scans normalized publication full text materialized by Stage 03;
- distinguishes carrier, reference/bridge, single-cell/analytical, blank/control and ambiguous
  channel roles;
- expands explicit reporter-channel ranges only when the endpoints are directly stated;
- keeps mixed `single-cell and control` ranges ambiguous rather than treating every channel as a
  cell;
- identifies the special one-analytical-channel-plus-carrier design that can potentially support one
  biological row per RAW without fabricating additional cells;
- flags chemistry-only or channel-evidence-free accessions for source enrichment or relation-mode
  re-evaluation.

The auditor does **not** write SDRFs and does **not** use GT metadata. Run the current 17-accession
mapping lane with:

```bash
./scripts/run_gt105_pride_sdrf_mapping_evidence_audit.sh
```

The wrapper derives the 15 original `denovo_mapping` accessions from the v0.3.1.2 triage and adds
accessions that moved into the mapping lane during v0.3.6. It materializes publication text for the
combined lane, then writes:

```text
data/sdrf_mapping_evidence_audit_gt105_pride_v040/
  mapping_accessions.txt
  publication_manifest_with_text.tsv
  audit/sdrf_mapping_evidence_audit_summary.json
  audit/sdrf_mapping_evidence_audit.tsv
  audit/contexts/PXD*.mapping_contexts.json
  audit/<mapping_class>.txt
```

Only mapping classes supported by explicit public evidence should advance to deterministic row
construction. Ambiguous channel ranges, missing reporter-role assignments, and likely false
multiplex relation hints remain review/recovery targets rather than being silently completed.

## v0.4.1: refreshed publication evidence and sub-study-scoped relation recheck

The first 17-accession mapping audit found no high- or medium-confidence deterministic mapping
candidates. Eleven accessions had only chemistry/partial reporter evidence, one had only a carrier
role hint, and five had no explicit reporter-channel evidence at all. This distribution exposed two
problems that must be resolved before multiplex row generation:

1. the mapping audit was using the older publication manifest, even though PRIDE publication
   metadata can change after a dataset is announced; and
2. the Rust study-design scaffold currently treats dataset-level co-occurrence of `single cell` and
   TMT/iTRAQ evidence as enough to suggest multiplexing. In mixed deposits, the isobaric experiment
   can belong to a different sub-study than the single-cell branch.

v0.4.1 remains non-generative. The wrapper refreshes publication associations directly from current
PRIDE project records using Stage 01, merges those rows with existing local PDF-backed publication
rows, materializes searchable full text with Stage 03, and reruns the mapping auditor.

The mapping auditor is upgraded to `pride-scp-sdrf-mapping-auditor-v0.2`. It adds:

- deterministic two-channel `respectively` grammar, e.g. `128 and 131 were analytical and carrier
  channels, respectively`, without assigning both numbers to the nearest role word;
- branch-scoped relation evidence: isobaric evidence only supports multiplexing when it is locally
  linked to a single-cell/fiber/oocyte/neuron/blastomere branch or when explicit reporter roles are
  present;
- non-isobaric single-cell context detection for label-free/DIA/CE-MS/MALDI/top-down branches;
- `relation_false_positive_candidate` for datasets where single-cell evidence is locally linked to
  a non-isobaric workflow while TMT/iTRAQ evidence is absent from, or unlinked to, that branch;
- explicit relation diagnostics (`linked_iso`, `linked_noniso`, and `dataset_iso`) in the TSV and
  compact console summary.

This is intentionally conservative. `relation_false_positive_candidate` does not rewrite the SDRF;
it returns the accession to a non-isobaric/mixed-design review lane so the production generator does
not invent reporter-channel mappings for a different experiment in the same deposit.

Run:

```bash
./scripts/run_gt105_pride_sdrf_mapping_relation_recheck.sh
```

Expected output root:

```text
data/sdrf_mapping_relation_recheck_gt105_pride_v041/
```

Important outputs are the refreshed and merged publication manifests plus the v0.2 audit inventory.
Only accessions that remain `multiplex_supported` and have explicit channel-role evidence should be
considered for the first deterministic multiplex row generator.

## v0.4.2: branch-scoped relation scaffold and real-corpus reporter-role repair

The accepted v0.4.1 relation recheck separated the former 17-member mapping lane into nine true
multiplex-supported studies, four high-confidence false multiplex relation candidates, two studies
with unlinked dataset-level isobaric evidence, and two relation/source rechecks. v0.4.2 keeps row
generation disabled and makes two bounded corrections.

### Rust relation scoping

For de-novo study design, `multiplexed_cells_per_data_file` is now asserted only when true isobaric
reporter evidence (TMT/TMTpro/iTRAQ or explicit reporter/carrier/reference-channel evidence) is
locally linked to the single-cell branch in sentence-scale evidence. Dataset-level chemistry in a
separate sub-study remains diagnostic but no longer establishes sample-to-file cardinality.
`plexDIA` and the lexical substring `plex` are not treated as isobaric reporter chemistry.

If the scoped deterministic scaffold remains `uncertain`, a de-novo Ollama proposal is not allowed
to restore `multiplexed_cells_per_data_file` from unscoped evidence. Such a proposal is downgraded
to `uncertain`. An unresolved non-multiplex scaffold reports
`sample_to_file_relation_unresolved`; `sample_to_channel_mapping_unresolved` is reserved for
scaffolds that actually support reporter multiplexing.

The four-accession acceptance rerun is:

```bash
./scripts/run_gt105_pride_sdrf_relation_scope_recheck_v042.sh
```

It reuses the v0.4.1 materialized publication manifest and reruns only PXD004892, PXD017755,
PXD035339, and PXD056528. Becoming locally valid is not required for acceptance; the required
behavior is removal of an unsupported reporter-multiplex blocker and exposure of the true remaining
file-role/metadata state.

### Reporter-role parser repair

PXD028040 is the mandatory real-corpus regression. Its public article uses `TMT-128 ... as the
analyte` and `TMT-131 ... carrier`, while the supplementary Methods also describe the TMT-128 digest
as the analytical channel and the TMT-131 digest as the carrier channel. The v0.4.1 auditor did not
recognize `analyte` as the analytical/single-cell role and could then apply a broad-context fallback
that assigned an otherwise-unbound channel to a nearby carrier role.

Auditor v0.3 therefore:

- recognizes `analyte` as an analytical/single-cell reporter-role synonym;
- binds role phrases locally to each reporter token, preferring a following role phrase before a
  preceding one;
- removes the broad-context role fallback that could relabel an unbound reporter;
- retains PDF line-break/hyphenation and reporter-token normalization protections; and
- treats an unbound reporter as unresolved rather than guessing its role.

Rerun only the nine v0.4.1 `multiplex_supported` accessions with:

```bash
./scripts/run_gt105_pride_sdrf_mapping_role_recheck_v042.sh
```

This remains non-generative. A subsequent reporter-row generator is allowed only if real accessions
now satisfy a narrow explicit evidence contract: one explicit analytical/single-cell reporter
channel per RAW plus an explicit carrier channel, with an optional explicit reference channel. A
general multi-single-cell-per-RAW generator remains out of scope until run/sample/channel mappings
are explicit.

## v0.4.2c: PDF-layout-tolerant reporter-role binding

The real v0.4.2b nine-accession recheck correctly recovered PXD028040's analytical reporter
(`TMT-128`) but still failed to recover its explicit carrier (`TMT-131`). Inspection of the
normalized publication text showed that this was not missing evidence. Two-column PDF extraction
inserted a hard newline and unrelated adjacent-column words between `TMT-131 ... which served as`
and `the multiplexing carrier`.

v0.4.2c remains non-generative. Auditor v0.4 adds a dedicated reporter-role binding segment that
interprets PDF hard line breaks as soft whitespace while retaining periods/semicolons and the next
reporter token as hard boundaries. This is deliberately narrower than a broad context fallback:
roles still cannot leak across another reporter token or into a following sentence.

The source-derived regression includes the observed PXD028040 extraction form and requires:

```text
TMT-128 -> analytical/single-cell
TMT-131 -> carrier
ambiguous -> none
```

Run the nine true multiplex-supported studies with:

```bash
./scripts/run_gt105_pride_sdrf_mapping_role_recheck_v042c.sh
```

No deterministic reporter-row generator should be implemented unless the user's real local corpus
passes the PXD028040 regression and at least one accession satisfies
`single_analytical_channel_per_run` from explicit public evidence.

## v0.4.3a: reporter run/file-scope audit before any row generation

The real v0.4.2c reporter-role rerun accepted PXD028040 as the first high-confidence narrow
reporter-layout candidate:

```text
mapping_class = single_analytical_channel_per_run
analytical/single-cell channel = 128
carrier channel = 131
ambiguous channels = none
```

That result establishes reporter **roles**, not yet reporter **row scope**. PXD028040 contains many
RAW files from different technical and biological phases of the study. A deterministic generator
must therefore not apply the 128/131 layout to every deposited RAW simply because the publication
contains that chemistry.

v0.4.3a remains non-generative and introduces `scripts/sdrf_reporter_run_scope_audit.py`. For the
single accepted narrow-contract candidate it:

- reads the current repository file snapshot;
- discovers small deposited experimental-design/metadata support files from explicit repository
  filenames rather than guessing paths;
- downloads only those small public support files when a repository URI is available;
- parses `.xlsx` content with Python's standard library (no spreadsheet dependency is added);
- emits the complete normalized support-table rows for source review;
- cross-references exact deposited RAW basenames/stems against support-table rows;
- inventories filename-level TMT/single-neuron hints only as diagnostics, never as sample truth;
- separately verifies that the publication contains a scoped 128/131 single-cell context; and
- leaves the reporter-row generator gate at `manual_design_row_review_required` even when exact file
  matches exist.

Run:

```bash
./scripts/run_gt105_pride_sdrf_reporter_run_scope_audit_v043a.sh
```

Expected output root:

```text
data/sdrf_reporter_run_scope_audit_gt105_pride_v043a/
```

Key outputs:

```text
audit/sdrf_reporter_run_scope_audit_summary.json
audit/support_file_status.json
audit/support_rows.tsv
audit/raw_support_matches.tsv
audit/publication_scope_contexts.json
```

The next deterministic generator is permitted only after the deposited design rows themselves show
which biological single-neuron samples map to which RAW acquisitions and that the accepted
TMT-128 analytical/TMT-131 carrier layout applies to those specific rows. Filename semantics alone
must never authorize SDRF rows or biological identities.

## v0.4.3b: structured experimental-design semantic audit

The real v0.4.3a PXD028040 run-scope audit found one deposited experimental-design workbook with 21
parsed rows, but only one of the 16 RAW basenames appeared exactly in the workbook. None of the seven
RAW filenames containing both TMT and single-neuron semantics had an exact support-row match.
Therefore exact-basename matching is not an adequate representation of the workbook's linkage
scheme, but filename semantics alone are still insufficient to authorize SDRF rows.

v0.4.3b remains non-generative. `scripts/sdrf_reporter_design_semantic_audit.py` preserves workbook
sheet, column and cell structure and tests only bounded source-grounded run identifiers:

1. exact deposited RAW basename/stem; then
2. an explicit acquisition date plus explicit `SCxx` run code occurring in both the deposited RAW
   basename and the deposited workbook row.

A date+SC match is only a candidate **run-to-design-row** link. It does not establish a biological
sample identity by itself. TMT/single-neuron words in filenames are diagnostic only and cannot create
a workbook match. Multiple workbook rows with the same structured key remain ambiguous.

The audit also records whether each candidate row explicitly contains sample/neuron semantics, TMT
semantics, replicate terms, reporter 128/131 values, or analyte/carrier role language and emits the
adjacent workbook rows for source review. Generation stays blocked until the relevant candidate rows
explicitly establish biological sample and replicate semantics.

Run:

```bash
./scripts/run_gt105_pride_sdrf_reporter_design_semantic_audit_v043b.sh
```

Expected output root:

```text
data/sdrf_reporter_design_semantic_audit_gt105_pride_v043b/
```

Key outputs:

```text
audit/sdrf_reporter_design_semantic_audit_summary.json
audit/design_rows_structured.tsv
audit/raw_design_candidates.tsv
audit/header_candidates.json
audit/candidate_row_contexts.json
```

No SDRF generator should consume these candidates automatically. The next implementation decision
must be based on the real workbook rows printed by the bounded audit.

## v0.4.4: source-grounded explicit row mapping for the first narrow multiplex design

The real v0.4.3b PXD028040 workbook review closes the biological/run-scope evidence gap.  The
single worksheet contains an explicit `Application for single neuron analysis` section followed by
nine design rows.  Those rows describe three biological neurons (`DA neuron #1`, `#2`, and `#3`),
each measured as technical replicate measurements 1-3.  Every row explicitly states approximately
100 pg of neuron digest tagged with TMT 128 together with approximately 10 ng of diluted tissue
digest tagged with TMT 131.

This also corrects the filename-derived diagnostic cohort from v0.4.3b.  Two source-supported
single-neuron acquisitions do not contain `single_neuron` in their RAW filename:

```text
2018-08-27_SC02.RAW
2018-09-04_SC05_10_ng_tmt.RAW
```

Therefore v0.4.4 does **not** use filename TMT/single-neuron words to select biological runs.
`scripts/sdrf_reporter_design_manifest.py` derives membership from the deposited workbook section,
requires each design row to contain one explicit date+SC run key, one explicit DA-neuron identity,
one explicit technical-replicate measurement, an explicit TMT128 neuron digest and an explicit
TMT131 tissue digest, and then requires that date+SC key to identify exactly one repository RAW.
The observed workbook contract is frozen at three biological samples x three technical replicates =
nine authorized rows.  Any source-layout change fails closed for manual review.

The manifest written by the script is provenance-rich and contains only source-supported row facts:

```text
accession
raw_file
source_name
cell_identifier
biological_replicate
technical_replicate
sample_type
cells_per_well
label
carrier_channel
reference_channel
design_source
design_ref
mapping_key
mapping_confidence
```

For PXD028040, the deterministic normalization is:

```text
DA neuron #1 -> source/cell identifier DA_neuron_1 -> biological replicate 1
DA neuron #2 -> source/cell identifier DA_neuron_2 -> biological replicate 2
DA neuron #3 -> source/cell identifier DA_neuron_3 -> biological replicate 3
technical replicate measurement N -> comment[technical replicate] = N
neuron digest tagged with TMT 128 -> comment[label] = TMT128
tissue digest tagged with TMT 131 -> comment[carrier channel] = TMT131
no reference channel stated -> comment[reference channel] = not applicable
```

The Rust annotator adds `--explicit-row-mapping-manifest`.  This is a generic source-grounded row
serialization path; it does not hard-code PXD028040.  A manifest row is accepted only when its RAW
file is present in the PRIDE snapshot, its mapping confidence is high, its source key is an allowed
explicit key (`exact_raw_name` or `date_sc_run_key`), its source/cell identifiers are template-safe,
its replicate/cell counts are numeric, its row is a single-cell row, and its analytical label,
carrier channel and design provenance are explicit.  Duplicate biological assignments to the same
RAW are rejected under this one-analytical-channel-per-run contract.

When explicit mappings exist, Rust serializes only those biological rows and validates them with the
normal strict generated-SDRF policy.  It no longer emits the synthetic
`sample_to_channel_mapping_unresolved` blocker merely because the study relation remains
`multiplexed_cells_per_data_file`.  The audit records the mapping-manifest path and row count, and
the review TSV records a `source_grounded_explicit_row_mapping_applied` provenance warning.

An explicit manifest is not allowed to silently make a mixed deposit look complete.  If fewer RAW
files are source-mapped than exist in the PRIDE repository inventory, Rust emits the blocking error
`explicit_row_mapping_repository_scope_incomplete` and completeness status
`incomplete_explicit_row_mapping_repository_scope`.  This allows the nine PXD028040 biological
reporter rows to be accepted as a deterministic mapping reconstruction without prematurely adding
the accession to the locally-valid count while seven development/control RAWs remain role-unresolved.

This is intentionally **not** a general TMT/TMTpro multi-cell generator.  It supports the narrow
architecture in which one explicitly identified analytical single-cell channel is represented by
one biological row per RAW and the explicit carrier is recorded as set/run metadata.  Unrelated
PXD028040 development/control RAWs are not forced into the reconstructed single-neuron branch.

Run the bounded reconstruction with:

```bash
./scripts/run_gt105_pride_sdrf_explicit_mapping_reconstruction_v044.sh
```

The wrapper first regenerates and validates the nine-row source manifest, then reruns only
PXD028040 through Rust.  Acceptance requires:

- exactly nine generated SDRF biological rows;
- exactly three source/cell identities, each with technical replicates 1, 2 and 3;
- every row linked to a repository RAW by the deposited design's unique date+SC key;
- `comment[label]=TMT128` and `comment[carrier channel]=TMT131` on all nine rows;
- no `sample_to_channel_mapping_unresolved` error;
- no `data_file_not_in_pride_raw_inventory` error; and
- generation mode `generated_source_grounded_explicit_row_mapping` with nine audited manifest rows.

For the first v0.4.4 run, PXD028040 is expected to remain outside the locally-valid count because the
manifest intentionally covers the nine source-proven single-neuron acquisitions while seven
development/control RAWs remain outside that branch.  Those seven must be role-mapped from the same
deposited design workbook (or another public source) before repository scope is complete.  In
addition, the publication-supported sampling method is patch-clamp-guided microaspiration and the
pinned single-cell 1.0.0 isolation-method vocabulary has no faithful value for that method.  Do not
map it to an unrelated allowed isolation term merely to obtain a green validator result.

## v0.4.5: close PXD028040 repository RAW scope without inventing cell identities

The real v0.4.4 run proved the nine-row single-neuron reconstruction but intentionally remained
repository-scope incomplete because PXD028040 contains seven additional RAW acquisitions.  The
same deposited workbook resolves those seven rows explicitly by acquisition date + `SCxx` key:

- three rows describe `100 pg of protein digest (diluted whole tissue)` with no explicit reporter
  assignment in the workbook row; and
- four rows describe `100 pg of protein digest (diluted whole tissue, tagged with TMT 128)` mixed
  with `10 ng of protein digest (diluted whole tissue tagged with TMT 131)`.

These rows are method-development/reference material rather than the biological DA-neuron branch.
They must therefore not inherit `dopaminergic neuron`, a single-cell isolation method, an individual
identifier, or a cell identifier from the single-neuron proposal.

`scripts/sdrf_reporter_full_design_manifest.py` extends the accepted v0.4.4 manifest to all 16 RAWs.
Membership is still determined only by the deposited workbook and a unique date+SC run key; filename
terms do not create a mapping.  The seven pre-single-neuron rows are serialized conservatively as
`study sample` rows with:

```text
cell identifier       = not applicable
biological replicate  = not applicable
cells per well        = not applicable
technical replicate   = 1
```

The `technical replicate=1` value is a structural single-measurement value for each unique source
alias; the manifest does not invent an unsupported replicate grouping for these development rows.
The three workbook rows with no explicit reporter assignment retain `comment[label]=not available`
and `comment[carrier channel]=not applicable`.  The four rows with explicit reporter assignments use
`TMT128` and `TMT131` exactly as recorded by the workbook.

The Rust explicit-manifest loader now accepts only the two bounded row classes required by this
source:

```text
single cell
study sample  (explicit non-single row with cell identifier/cells-per-well = not applicable)
```

Single-cell rows keep the stricter requirements from v0.4.4: numeric biological replicate/cell
count and explicit isobaric carrier.  Non-single study rows must use `not applicable` for biological
replicate, cells per well, and cell identifier.  Isobaric labels still require an explicit carrier;
reserved carrier values are allowed only when the row has no explicit isobaric label.

When serializing an explicit non-single row, Rust deliberately sets cell type, single-cell isolation,
individual, and sample-preparation batch to `not applicable` rather than leaking global single-cell
metadata into whole-tissue material.  `row_explicit_non_single_cell_role()` recognizes this bounded
`study sample` form so the local validator does not demand a single-cell isolation method for those
rows.

Run:

```bash
./scripts/run_gt105_pride_sdrf_full_repository_mapping_v045.sh
```

Acceptance requires:

- 16 explicit manifest rows and 16 generated SDRF rows;
- 9 `single cell` rows preserving the accepted 3-neuron x 3-technical-replicate architecture;
- 7 non-single `study sample` whole-tissue rows;
- unique coverage of every PRIDE RAW acquisition;
- no `explicit_row_mapping_repository_scope_incomplete`;
- no `sample_to_channel_mapping_unresolved`;
- no `data_file_not_in_pride_raw_inventory`; and
- no validation errors other than the nine expected `single_cell_isolation_unresolved` errors for
  the real patch-clamp microaspiration method that the pinned single-cell 1.0.0 isolation vocabulary
  cannot faithfully encode.

If these criteria pass, PXD028040 should move out of the reporter/file-mapping lane and into a
narrow template-vocabulary exception state with completeness
`incomplete_template_isolation_method_gap`.  Do not substitute `manual picking`, FACS, or another
allowed isolation value merely to mark the accession locally valid.

## v0.4.6 residual multiplex support-asset audit

After the accepted PXD028040 v0.4.5 reconstruction, PXD028040 is no longer an unresolved
reporter/file-mapping case. All 16 repository RAWs have explicit source-grounded workbook mappings;
its remaining blocker is the single-cell isolation vocabulary gap for patch-clamp microaspiration.

The active true-multiplex reconstruction cohort is therefore the eight remaining accessions selected
from the accepted v0.4.2c `multiplex_supported` audit after excluding PXD028040.

`scripts/sdrf_multiplex_support_asset_audit.py` and
`scripts/run_gt105_pride_sdrf_multiplex_support_asset_audit_v046.sh` perform the next bounded,
non-generative source audit. They inventory repository support files, download only small public
non-RAW design/metadata/readme/SDRF/tabular assets, and surface explicit reporter roles, channel
numbers, single-cell semantics, run/file linkage, sample identifiers, and replicate semantics.

Chemistry-only hits and filename-only words remain diagnostic. They never authorize SDRF row
serialization. The audit is intended to identify which of the remaining eight accessions has a
PXD028040-like deposited design source that can support the next deterministic reconstruction, and
which accessions instead require publication/source recovery.

## v0.4.7: high-specificity residual-multiplex support-asset recheck

The real v0.4.6 eight-accession run is an accepted support-discovery checkpoint, but its semantic
hit counts are **not** accepted mapping evidence.  The permissive v0.1 scanner selected many
search/result tables and then matched ordinary proteomics-result vocabulary.  PXD029320 was the
clearest regression fixture: 66 support candidates produced 15,733 hits, including protein names
containing phrases such as `solute carrier`, `RUN and SH3 domain-containing protein`, and
`cell division control protein`, plus bare numbers such as 128/131/132 that were unrelated to
reporter-channel assignments.  Across the eight-accession cohort v0.4.6 recovered no explicit
reporter-role hits, so no accession was authorized for row generation.

v0.4.7 keeps the audit non-generative and tightens two independent gates.

### Asset triage

Repository files are classified before semantic scanning.  Explicit experimental-design, metadata,
sample-map/sheet, run/file-map, channel-layout, reporter-layout, SDRF, manifest and annotation names
are high-priority support assets.  Readme/methods-like text and non-result workbooks are retained as
bounded secondary sources.  Search-engine, peptide/protein, PSM, spectral, feature, quantification,
identification and `realtimesearch` result assets are excluded unless the filename itself explicitly
identifies a design/metadata source.

The v0.4.7 runner reuses already downloaded v0.4.6 assets when possible, so tightening this gate does
not require redownloading the prior result-table corpus.

### High-specificity mapping evidence

A number in the 126-135 reporter range is no longer evidence by itself.  It is recognized only when
prefixed by TMT/TMTpro or locally anchored by reporter/channel/tag/label syntax.  `TMEM131`,
`C6orf132`, and ordinary numeric result columns therefore cannot create reporter-channel evidence.

Role vocabulary is also context-bounded.  Protein-result phrases such as `solute carrier`,
`mitochondrial carrier`, `carrier protein`, `RUN and SH3 domain`, and `cell division control protein`
are explicitly protected false-positive regressions.  Run linkage requires an explicit RAW token or
phrases such as `RAW file`, `file name`, `run ID/name`, `sample ID/name`, `acquisition`, `batch`, or
replicate semantics; the bare word `run` is insufficient.

Hits are separated into evidence tiers:

```text
reporter_role_and_run
explicit_reporter_role
single_cell_reporter_layout
run_or_sample_linkage
source_triage_only
```

Only the first four are written to the compact credible-evidence output.  Chemistry-only prose is
retained as `source_triage_only` for navigation but cannot promote an accession toward SDRF row
generation.

Run the bounded recheck with:

```bash
./scripts/run_gt105_pride_sdrf_multiplex_support_asset_recheck_v047.sh
```

Expected output root:

```text
data/sdrf_multiplex_support_asset_recheck_gt105_pride_v047/
```

Key outputs:

```text
audit/sdrf_multiplex_support_asset_audit_summary.json
audit/sdrf_multiplex_support_asset_audit.tsv
audit/support_asset_status.tsv
audit/support_asset_evidence_hits.tsv
audit/support_asset_credible_evidence_hits.tsv
```

A `support_asset_reconstruction_candidate` requires at least one high-specificity support unit that
contains explicit reporter role(s) and channel(s) together with explicit run/sample linkage.  A
study with separate role and run evidence remains a review candidate rather than an automatic
mapping source.  If no accession reaches that gate, the next step is accession-specific source or
publication recovery, not a broader reporter parser and not a generic multiplex generator.

## v0.4.8: publication-accession reverse recovery for residual multiplex studies

The real v0.4.7 run is an accepted negative repository-support checkpoint.  After strict result-table
triage, none of the eight residual `multiplex_supported` accessions had a high-value repository
experimental-design/sample-map/channel-layout asset.  The active evidence source therefore moves from
PRIDE support files to publication/source recovery; the reporter generator remains unchanged and no
row generation is authorized by this phase.

A web/source spot check exposed a second generic failure mode in the older publication manifest:
publication metadata can be stale, absent, or incorrect even when a full-text paper is publicly
available.  Examples include publications whose data-availability text contains PXD029320,
PXD034370, PXD041328/PXD048347, PXD041399, PXD045500, and PXD073405.  Conversely, an exact accession
string is not sufficient by itself: corrected or erroneous accession citations can point at a
biologically unrelated article.

v0.4.8 adds `scripts/sdrf_publication_accession_recovery.py`.  It performs a bounded Europe PMC reverse
lookup for each residual multiplex accession and requires all of the following before creating a
recovered publication-manifest row:

1. the exact PXD accession is verified in the candidate PMC full text;
2. the candidate is compatible with the current PRIDE project title, **or** its DOI/PMID matches a
   current PRIDE publication association;
3. only the strongest compatible article is retained for downstream Stage-03 materialization.

An exact-accession article with strong project-title mismatch is retained as a rejected diagnostic.
If the same DOI/PMID is currently attached to the PRIDE project, the v0.4.8 wrapper quarantines that
publication association rather than allowing it to become SDRF evidence.  This is specifically a
source-integrity guard; it does not use GT metadata and it does not substitute another publication by
similarity alone.

Run the bounded recovery with:

```bash
./scripts/run_gt105_pride_sdrf_publication_accession_recovery_v048.sh
```

The wrapper:

1. derives the eight residual multiplex accessions from the accepted v0.4.2c relation audit;
2. refreshes current PRIDE publication metadata with Stage 01;
3. reverse-resolves exact accession mentions through Europe PMC;
4. merges accepted recovered publications with useful historical local-PDF rows;
5. quarantines directly contradicted current-PRIDE publication identifiers;
6. materializes full text through the existing Stage-03 contract;
7. reruns the non-generative reporter-role/mapping auditor.

Expected output root:

```text
data/sdrf_multiplex_publication_accession_recovery_gt105_pride_v048/
```

Key outputs:

```text
publication_recovery/publication_accession_recovery_summary.json
publication_recovery/publication_accession_recovery_candidates.tsv
publication_recovery/recovered_publications.tsv
publication_manifest_quarantine.tsv
publication_manifest_with_text.tsv
audit/sdrf_mapping_evidence_audit_summary.json
audit/sdrf_mapping_evidence_audit.tsv
```

A recovered full-text publication is still only an evidence source.  Reporter-row generation remains
blocked unless the refreshed corpus exposes explicit source-grounded reporter roles and run/sample
relationships.  If publication text improves but no explicit mapping contract emerges, the next step
is accession-specific supplementary/source recovery rather than a broader generic parser.

## v0.4.9: local-first publication corpus reconciliation across all 105 PRIDE accessions

The real v0.4.8 run improved publication-text coverage for the residual eight multiplex studies to
7/8 and correctly quarantined the incompatible current-PRIDE publication association for PXD069039.
However, that iteration also highlighted a source-ordering issue: the repository already contains a
large manually curated/local manuscript corpus under `manual_pdfs/`, the Stage-02 managed PDF cache,
and normalized Stage-03 text caches.  Later bounded SDRF wrappers generally preserved those sources
only when they were already represented by an earlier manifest.  They did not re-index the complete
local corpus before initiating publication recovery.

v0.4.9 makes local publication evidence explicit and auditable before any further network recovery.
`scripts/sdrf_local_publication_corpus_reconcile.py` is deliberately network-free.  For the accepted
105-primary-PRIDE cohort it reconciles, in order:

```text
manual_pdfs/manual_pdf_manifest.tsv
manual_pdfs/PXDxxxxxx.pdf
validated existing Stage-02 / legacy publication PDFs
validated existing normalized publication-content text
trustworthy historical/current publication-manifest metadata
```

The wrapper derives the 105-accession cohort from the accepted v0.3.1 full-run source-grounded
checkpoint rather than reopening GT196/GT179.  GT metadata/labels are not read by this phase.

### Local source safety

A local file is not allowed to bypass an accepted publication quarantine.  v0.4.9 imports quarantine
identifiers from prior recovery outputs and also hashes local PDF/text content.  A manual/cache file
whose exact content matches a quarantined publication remains quarantined even if it is named by the
PXD accession.  This protects the local-first path from reintroducing the PXD069039 incompatible
Arabidopsis publication through an older cached/manual copy.

Explicit `manual_pdf_manifest.tsv` mappings have highest source priority, followed by accession-named
manual PDFs.  Existing manifest metadata is merged onto those files when publication identity is
already known.  DOI/PMID-named managed PDF/text caches are then matched through publication identity.
Metadata-only manifest rows remain visible but are not treated as local manuscript content.

Selected local PDFs without an existing normalized text file are extracted locally into the v0.4.9
output.  The reconciler does **not** invoke Stage 01, Stage 02 network download, Stage 03 PMC fallback,
Europe PMC, Crossref, or any other network service.

Run:

```bash
./scripts/run_gt105_pride_sdrf_local_publication_corpus_reconcile_v049.sh
```

Expected output root:

```text
data/sdrf_local_publication_corpus_reconcile_gt105_pride_v049/
```

Key outputs:

```text
local_publication_corpus_summary.json
local_publication_source_inventory.tsv
local_publication_candidate_rows.tsv
local_publication_manifest_selected.tsv
local_publication_manifest_all.tsv
local_publication_quarantine.tsv
external_recovery_needed.txt
metadata_only_accessions.txt
unresolved_accessions.txt
```

`local_publication_source_inventory.tsv` is the authoritative per-accession local-source audit.  It
records whether each accession resolves through an explicit manual mapping, accession-named manual
PDF, validated cached PDF, cached normalized text, metadata-only publication row, quarantine-only
state, or no local source.

The wrapper also reruns the **non-generative** reporter/mapping audit for the eight residual true
multiplex studies using only the reconciled selected local corpus.  This is a diagnostic comparison:
it determines whether publication evidence already present locally was missed by the recent v0.4.x
manifests.  It does not authorize reporter-row generation by itself.

After v0.4.9, external publication recovery should be restricted to
`external_recovery_needed.txt` rather than re-querying every accession.  A locally resolved and
non-quarantined publication should not be searched/downloaded again during routine SDRF annotation.

### v0.4.8 quarantine merge hotfix carried with v0.4.9

The real v0.4.8 output exposed one additional merge defect: the refreshed PXD069039 PRIDE row was
correctly quarantined, but the merge subsequently preserved the older local-PDF-backed v0.4.1 row
for the same quarantined DOI.  Stage 03 therefore still materialized the incompatible Arabidopsis
paper and the reporter audit reported `pub_text=1` for PXD069039.

v0.4.9 also patches `run_gt105_pride_sdrf_publication_accession_recovery_v048.sh` so historical/base
manifest rows whose DOI/PMID matches an accepted quarantine identity are quarantined instead of
being re-added through the local-PDF preservation path.  The local-first reconciler independently
reapplies the same quarantine and adds local-content hash checks, so the bad publication cannot
re-enter through either route.

## v0.5.0: bounded external publication recovery for source-sensitive residuals

The real v0.4.9 local-first reconciliation established an explicit corpus state across all 105
primary PRIDE accessions:

```text
67 selected local publication content
31 manifest-metadata-only publication records
 6 unresolved local publication states
 1 quarantine-only state (PXD069039)
```

This produced 38 accessions in `external_recovery_needed.txt`, but publication incompleteness is not
a current SDRF blocker for every one of those datasets.  v0.5.0 therefore does **not** perform a
blanket 38-accession web sweep.  It derives a source-sensitive residual union from accepted SDRF
outputs (remaining true multiplex studies, relation/source-recheck cases, and unresolved
required-metadata cases) and intersects that union with the v0.4.9 external-recovery queue.

`scripts/sdrf_residual_external_publication_recovery.py` then performs bounded external recovery only
for that intersection.  Recovery order is:

```text
known DOI / PMID / publication title
        -> Europe PMC identity lookup
exact PXD accession
        -> Europe PMC reverse lookup
project title
        -> review-only fallback unless accession/known identifier corroborates the paper
```

A candidate publication is accepted only when it has a verifiable source identity:

* exact PXD accession in PMC full text plus project-title identity/compatibility; or
* a known non-quarantined publication DOI/PMID plus compatible project/publication title.

An exact accession string is **not** sufficient when the article is scientifically incompatible with
the PRIDE project.  Previously accepted quarantine identifiers remain hard blockers.  This preserves
the PXD069039 safeguard: the corrected Arabidopsis publication cannot become evidence merely because
its original article text contained the erroneous PXD069039 string.

Accepted Europe-PMC JATS XML and normalized text are cached immediately in the v0.5.0 output, so the
same article is not downloaded again by a second content-resolution stage.  The recovery stage also
inventories supplementary-material and external-data links (including Zenodo references) to support a
later bounded sample/channel-design recovery iteration.

Run:

```bash
./scripts/run_gt105_pride_sdrf_residual_external_publication_recovery_v050.sh
```

Expected output root:

```text
data/sdrf_residual_external_publication_recovery_gt105_pride_v050/
```

Key outputs:

```text
source_sensitive_residual_union.txt
active_external_recovery_queue.txt
deferred_external_recovery.txt
publication_recovery/residual_external_publication_recovery_summary.json
publication_recovery/publication_recovery_candidates.tsv
publication_recovery/recovered_publications.tsv
publication_recovery/combined_publication_manifest.tsv
publication_recovery/publication_supplementary_links.tsv
publication_recovery/unresolved_after_external_recovery.txt
residual_multiplex_audit/sdrf_mapping_evidence_audit.tsv
relation_source_audit/sdrf_mapping_evidence_audit.tsv
```

The combined publication manifest preserves the v0.4.9 local-first selections and adds only accepted
external content for active residuals.  v0.5.0 remains non-generative: it reruns the residual
multiplex and relation/source auditors but does not write SDRF reporter rows or force missing metadata.

The next implementation must be chosen from the real recovered sources.  If a recovered paper exposes
supplementary design/sample/channel material, recover only those assets and build a source-grounded
mapping contract.  If a source-limited required-metadata accession gains a trustworthy publication,
rerun that accession alone through the existing deterministic annotation scaffold.  Do not reopen
already-solved reporter or isolation architecture lanes.

## v0.5.1: multiplex evidence graph and structured-analysis-artifact reconstruction

The real v0.5.0 run recovered four source-sensitive publications but did **not** improve the
reporter mapping gate for the eight residual studies: all eight remained
`chemistry_or_partial_channel_evidence` and no medium/high reporter-role candidate emerged.  That
is treated as an architectural stop condition.  The pipeline must not continue with broader regexes,
more publication searches, or repeated support-file triage.

The core failure is the assumption that a usable SDRF design must be stated as a local prose
relationship such as `reporter 126 = single cell`.  For unsupported PRIDE submissions, design truth
is often distributed across several source types:

1. publication methods establish the **single-cell branch/modality** and global labeling contract;
2. result workbooks and vendor analysis databases retain **sample/channel/file structure**;
3. analysis-code/data repositories linked by the authors retain cell/sample identities and input
   tables;
4. reporter abundance columns can validate carrier/blank/reference behavior quantitatively; and
5. related PRIDE accessions can represent separate branches, reannouncements, or duplicate study
   deposits and must be resolved before generation.

`scripts/sdrf_multiplex_evidence_graph.py` implements this architecture as a non-generative evidence
graph.  It never reads GT/reference resources and it does not write SDRF rows.

### Layer A — branch/modality contract

The graph first asks which repository branch is actually single-cell proteomics and whether that
branch is label-free or reporter multiplexed.  Dataset-level TMT evidence is not sufficient.  This is
a mandatory correction to the earlier relation model: mixed studies can contain TMT experiments in
one branch and label-free SCP in another.

A high-confidence label-free SCP branch is removed from the reporter-multiplex reconstruction lane
and routed to repository/run segmentation instead.

### Layer B — chemistry-aware global reporter contract

Reporter roles are parsed as **sets**, not nearest-token assignments.  The parser supports explicit
channel lists and set language such as:

```text
single cells labeled with 126, 127, 128 and 129
all channels except 126 and 127C
carrier labeled with TMT126
127C left empty
```

Chemistry-specific channel universes are represented for TMT6plex, TMT10plex, TMTpro16 and
TMTpro18.  A channel cannot silently acquire two roles.  A complete global design requires an
explicit analytical set plus carrier/reference role and no contradiction.

Mandatory regression fixtures include:

* TMT6: analytical `126,127,128,129`, blank `130`, carrier `131`;
* TMTpro18: carrier `126`, blank `127C`, analytical = the other 16 channels;
* partial RETICLE-style contract: carrier `126`, expected 14 analytical cells, but exact analytical
  channel set remains open until a structured source closes it.

### Layer C — structured repository analysis artifacts

v0.4.7 was correct to reject result-table *semantic* noise but too aggressive in excluding result
artifacts entirely.  v0.5.1 restores them as structured sources:

* `.pdResult`, `.msf`, `.pdStudy`: SQLite schema/table inspection and bounded reads of
  sample/channel/file/quantification fields;
* `.sky`: Skyline XML replicate/file/sample relationships;
* `.xlsx`: sheet/row structure, sample/file/channel cells;
* `.csv/.tsv/.txt`: headers and bounded structured rows; result tables are never scanned as free
  prose;
* reporter abundance/intensity/SN columns: bounded per-channel median summaries for quantitative
  validation.

Repository acquisition is bounded by per-accession file count and maximum file size.  Large or
unsupported artifacts remain explicit blockers rather than being silently skipped.

### Layer D — publication-linked external analysis sources

GitHub/Zenodo/Mendeley links are accepted only when they are explicitly present in publication
content or accepted supplementary-link inventories.  For GitHub repositories the stage may perform
a bounded tree query and retrieve only small high-value files whose names indicate cell/sample/input/
design/metadata/channel information.  This is author-linked source evidence, not generic web search.

Review-only publication candidates from v0.5.0 can enter this evidence graph only after their full
text is fetched and the exact accession is independently re-verified.  Title similarity by itself is
not publication evidence.

### Layer E — cross-accession relation/integrity graph

Highly similar project titles are paired and their RAW filename inventories compared.  The stage
emits relation-review candidates such as same-study/related-deposition or probable duplicate/
reannouncement.  It never chooses a canonical accession automatically.

### Layer F — generation authorization

The evidence graph emits one of the following states rather than directly generating SDRF rows:

```text
branch_reclassification_ready
global_design_closed_run_mapping_open
structured_artifact_evidence_partial
external_analysis_source_identified
explicit_row_manifest_candidate
source_graph_open
```

Only `explicit_row_manifest_candidate` is eligible for a future source-grounded explicit-row manifest.
Even then, biological sample identities must come from a source table/code input or another explicit
source; quantitative reporter behavior can validate channel roles but cannot invent biological
identity.

Run:

```bash
./scripts/run_gt105_pride_sdrf_multiplex_evidence_graph_v051.sh
```

Expected output root:

```text
data/sdrf_multiplex_evidence_graph_gt105_pride_v051/
```

Key outputs:

```text
audit/multiplex_evidence_graph_summary.json
audit/multiplex_evidence_graph.tsv
audit/branch_contracts.tsv
audit/design_contracts.tsv
audit/artifact_inventory.tsv
audit/structured_artifact_evidence.tsv
audit/external_analysis_sources.tsv
audit/relation_candidates.tsv
audit/review_publication_promotions.tsv
```

The next implementation must follow the graph result.  Do not return to token-local reporter regexes
or blanket publication/support searches if a graph layer remains open.
