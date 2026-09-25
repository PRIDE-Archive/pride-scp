# Specialized structured-design ingestion

`scripts/sdrf_specialized_design_ingest.py` is a fail-closed preparation layer for
structured PRIDE sidecars that are not already production-ready SDRFs.

It does **not** replace the frozen validator-gated closure controller.  Its only
job is to turn explicit source structure into a common evidence graph and, when
a base SDRF is supplied, a conservative candidate that can be passed into the
existing closure v3/substrate v3 stack.

Supported adapters:

- XLS/XLSX compact experimental-design sheets;
- CSV/TSV run/channel annotation tables (delimiter auto-detected);
- Proteome Discoverer `.pdStudy` XML.

Safety contract:

- exact joins only;
- no row-order inference;
- no filename semantic inference;
- concrete SDRF values are never overwritten by conflicting sidecar values;
- missing/placeholder values may be filled from exact source-backed joins;
- `.pdStudy` topology supplies RAW/channel/sample structure only and is not
  treated as biological identity when factor values are absent;
- XLSX run identifiers become row mappings only if they match a unique base-SDRF
  assay name or RAW stem;
- all candidate SDRFs must still pass the frozen canonical serializer, validator,
  readiness policy and exact-hash review before submission.

Outputs per accession:

- `<PXD>.structured_design_graph.json`
- `<PXD>.structured_mapping.tsv`
- `<PXD>.ingest_summary.json`
- `<PXD>.specialized_candidate.sdrf.tsv` when `--base-sdrf` is supplied

The current specialized3 acceptance cohort is intentionally format-diverse:

- PXD028040 — XLSX experimental design;
- PXD046211 — semicolon run/channel sample annotation tables plus deposited SDRF;
- PXD029320 — fourteen primary `.pdStudy` XML files.

The adapter is reusable for later hard-tail accessions with the same source
formats.  No accession-specific scientific values are embedded in the code.

## Runtime robustness (v1.1)

- `.xlsx` parsing prefers `openpyxl` when available, but has a standard-library
  ZIP/XML fallback so the immutable production SIF does not require `openpyxl`.
- If the requested `--base-sdrf` has zero data rows and the structured source
  directory contains exactly one non-empty `*.sdrf.tsv`, that deposited source
  SDRF is selected as the effective base. A substantive requested base is never
  replaced automatically.
- If an exact annotation pass applies zero values, the candidate is copied
  byte-for-byte from the effective base; formatting/newline reserialization is
  never counted as scientific progress.
