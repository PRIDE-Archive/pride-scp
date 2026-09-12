# Release20 repair4 v1 — bounded scientific fixes after six-PR reconciliation

Date: 2026-09-12

This repair is intentionally limited to three accessions. It does **not** change the accepted
v0.5.14.3 BigBio compatibility architecture and it does not touch the independently approved
PXD062702 v5 hash.

## Why a new repair lane is used

The prior release20 repair helper used `csv.DictReader`/`csv.DictWriter`. SDRF allows repeated headers
for multiple cleavage agents and modification parameters. A dictionary representation collapses those
columns, which caused the v5 repair to repeat the last cleavage/modification value across every
occurrence. Repair4 uses positional rows and preserves duplicate headers exactly.

## PXD019515

The v5 biology/sample split remains frozen. Repair4 changes only search/sample-preparation metadata:

- `comment[precursor mass tolerance]` -> `not available`: the publication reports `<5 ppm`, while the BigBio `ms-proteomics` field accepts an exact numeric value plus unit and explicitly permits `not available`; the repair therefore does not invent `5 ppm` or false precision;
- first cleavage-agent slot -> `NT=Trypsin;AC=MS:1001251`;
- any additional cleavage-agent slot -> `not applicable` because no second enzyme is source-supported;
- three modification slots -> variable methionine oxidation, variable protein N-terminal acetylation,
  fixed cysteine carbamidomethylation.

Source: Cong et al., Chemical Science 2021, DOI 10.1039/D0SC03636F.

## PXD019958

The v5 biological/control/template repair remains frozen. Repair4 changes only proteomics search fields:

- first cleavage-agent slot -> `NT=Trypsin;AC=MS:1001251`;
- additional cleavage-agent slot(s) -> `not applicable`;
- three modification slots -> variable methionine oxidation, variable protein N-terminal acetylation,
  fixed cysteine carbamidomethylation.

The publication explicitly states that the specific proteolytic enzyme was trypsin.

Source: Lamanna et al., Nature Communications 2020, DOI 10.1038/s41467-020-19394-5.

## PXD054066

Upstream PR #472 has the same known `sdrf-pipelines` #345 DIA-validator drift as five other red PRs,
but its independent Qodo review also identified genuine source-grounding defects.

Repair4 therefore:

- resets `comment[sample preparation batch]` to `not available` rather than assigning the same three
  May-22 HeLa run identifiers to every acquisition;
- clears impossible cell-specific biological identity from exactly the four zero-cell blank controls
  (`individual`, `cell type`, `cell line`, optional Cellosaurus identity, and `material type` ->
  `not applicable`; cell identifier remains `empty`). Study-level organism/organism-part context and the
  technical isolation workflow are not erased without an independent source finding.

The repair does **not** infer batch identity from filename tokens such as `Batch1`.

The normative DIA value remains:

```text
NT=Data-independent acquisition;AC=PRIDE:0000450
```

and must not be degraded merely to satisfy the stale validator.

## Publication gate

All three repaired candidates must pass:

1. scientific guard;
2. ontology-backed local `parse_sdrf` validation;
3. v0.5.14.3 readiness;
4. `needs_independent_review` state;
5. fresh source-level exact-hash adjudication.

Only after approval may PXD019958/#463 and PXD054066/#472 be updated. PXD019515 requires a new
corrective PR referencing merged #462.
