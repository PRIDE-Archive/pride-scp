# PRIDE-SCP v0.5.14.7 — accession-independent SDRF semantic repair guards

## Motivation

Independent exact-hash review of the Mapping11 review10 population found two semantic defects that
passed structural/template validators:

1. `characteristics[individual]` could contain a non-individual biological category, including a
   dataset-wide evidence rendering such as `A | B` copied from heterogeneous organism-part values.
2. rows explicitly named as bulk samples in `source name` could be assigned
   `characteristics[sample type] = single cell` by the one-cell-per-file enrichment default.

The repair must not depend on PXD accession identity, frozen GT labels, or manual row numbers.

## v0.5.14.7 contract

### Individual/donor category leakage

The native Rust audit and Python scientific guard detect an individual value when it:

- exactly duplicates a row-local organism part, cell line, cell type, or developmental stage;
- is a pipe-delimited set exactly equal to the dataset-wide concrete values of one of those semantic
  columns; or
- is a donor token synthesized from a concrete cell-line name.

The readiness projection may remove only this deterministic false identity:

- a row with explicit cell-line material/Cellosaurus identity becomes `not applicable`;
- any other affected row becomes `not available`.

No donor/individual identifier is synthesized from source names, filenames, replicate numbers, cell
identifiers, or GT data.

The Rust proposal provenance repair also rejects multi-valued pipe-delimited dataset-level
`individual` proposals before they can be merged into an existing SDRF. The existing-SDRF merge path
then applies the same deterministic category-leakage test row-by-row, replacing false identity with
`not available` or `not applicable` before a newly enriched candidate is emitted.

### Explicit bulk sample role

A tokenized `bulk` marker in `source name` is treated as explicit sample-role evidence. This is not a
`comment[data file]` filename heuristic.

When all of the following hold:

- `source name` contains the standalone token `bulk`;
- sample type is blank, `not available`, or incorrectly `single cell`;
- `characteristics[cells per well]` is blank/reserved/inapplicable; and
- `characteristics[cell identifier]` is blank/reserved/inapplicable;

readiness serializes `characteristics[sample type] = pooled`.

If the same explicit bulk source row still carries concrete single-cell identity (`cells per well = 1`
or a concrete cell identifier), the scientific guard blocks rather than choosing between conflicting
claims.

The Rust existing-SDRF enrichment path applies the same bulk-role rule before its historical
`one_cell_per_data_file -> single cell` default, preventing the defect in newly regenerated candidates.
The native audit also reports a concrete `single cell`/explicit-bulk contradiction.

### Safety properties

- No accession-specific branches.
- No GT runtime truth.
- No data-file-name inference for the bulk correction.
- No donor identity inference.
- Source candidates remain immutable in readiness; corrections are hash-audited projection actions.
- Every automatic semantic correction is listed in the normalization manifest.
- Unknown or internally conflicting cases remain fail-closed.

## Mapping11 regression expectations

When applied to the frozen rejected projected artifacts as regression fixtures, v0.5.14.7 changes only:

- PXD053023: 35 `characteristics[individual]` cells:
  - 18 non-cell-line rows -> `not available`;
  - 17 cell-line rows -> `not applicable`.
- PXD059079: six `characteristics[sample type]` cells on explicit `*_bulk_library` rows -> `pooled`.

No other biological, file, acquisition, channel, factor, or replicate cells are changed.
