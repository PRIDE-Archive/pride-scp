# PRIDE-SCP v0.5.14.4 residual85 pipeline hardening

## Purpose

v0.5.14.4 incorporates generic lessons from the release20 upstream review/CI cycle before processing
the remaining 85 PRIDE single-cell-proteomics accessions.

This is not an accession-specific repair release. The immutable source candidate remains scientific
provenance; only the publication derivative may apply explicitly audited compatibility serialization.
Missing biology, chemistry, mapping or sample/file/channel evidence remains fail-closed.

## Generic rules now enforced

1. Explicit DIA values are serialized in publication artifacts as `Data-independent acquisition` while
   the maintained `sdrf-pipelines` DIA validator requires that form. The source candidate is unchanged.
2. Newly generated drafts use the same validator-compatible DIA serialization, preventing recurrence.
3. `open_sdrf_annotated_dataset_prs.py` validates structural, `ms-proteomics`, and every declared leaf
   template before a branch is pushed.
4. Repeated SDRF headers are preserved. Repeated identical cleavage-agent or modification values are
   scientific-guard blockers rather than silently collapsed columns.
5. Exact zero-cell controls are recognized only by the strict conjunction:
   sample type in `{empty, blank, negative control}`, cell identifier `empty`, cells per well `0`.
   Only cell-specific identity fields are cleared; organism/organism-part/disease context remains.
6. `study sample` is projected fail-closed as `not available` while the maintained validator lacks the
   advertised term.
7. No source-missing mapping, channel assignment, donor, cell type, instrument, chemistry or other
   scientific metadata is inferred to improve readiness counts.

## Residual85 population

The frozen post-review v0.5.14.3 baseline is:

```text
 3 independently rejected exact hashes requiring source repair
11 mapping incomplete
17 metadata incomplete
10 BigBio/ontology blocked
 4 parse/content blocked
 5 divergent trusted-candidate conflicts
35 no source-closed candidate
---------------------------------------------------------------
85 residual
```

v0.5.14.4 should first be run over this same population and the same trusted candidate set. Do not
rerun KG/LLM/GPU discovery merely to recreate the baseline.

## Acceptance gates

The v0.5.14.4 residual85 rerun is accepted only if:

- policy reports `pride-scp-bigbio-readiness-v0.5.14.4`;
- immutable source-candidate SHA-256 values are unchanged;
- exact DIA source values can no longer create the known dia-acquisition false-red publication state;
- no automatic sample/file/channel relation is introduced;
- no missing scientific value is guessed;
- zero-cell normalization is limited to the strict role signature;
- repeated chemistry defects are detected before independent review;
- PR publication automation locally validates all declared leaf templates;
- any newly projectable artifact still requires fresh exact-hash independent review.

## Recovery order after the rerun

1. Repair the three REVIEW23 rejects from source evidence only: PXD031955, PXD043473, PXD046357.
2. Resolve the 11 mapping-incomplete accessions as one population.
3. Resolve the 17 metadata-incomplete accessions as one population.
4. Reclassify the 10 BigBio/ontology blockers after v0.5.14.4 removes generic compatibility noise.
5. Inspect the 4 parse/content blockers.
6. Source-reconcile the 5 divergent trusted candidates.
7. Use the existing local-first/external-publication recovery architecture for the 35 with no
   source-closed candidate.

GT196/GT179 remain evaluation-only and may not be used as production lookup truth.
