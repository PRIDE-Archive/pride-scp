# v0.5.14.8 — generic single-cell channel reserved-word normalization

The v0.5.14.7 native auditor correctly rejected blank `comment[carrier channel]` and
`comment[reference channel]` values, but readiness only repaired them when the entire dataset was
label-free. Mixed non-isobaric designs (for example label-free + dimethyl) therefore exposed an
auditor/projection inconsistency.

This release makes the contract generic and row-wise:

- blank `comment[sample preparation batch]` -> `not available`;
- blank carrier/reference on clearly non-isobaric rows (`label free`, `dimethyl`, `SILAC`) -> `not applicable`;
- blank carrier/reference on any other/unknown label -> `not available`;
- existing concrete channel values are preserved;
- no carrier/reference channel identity is inferred.

The same rule is applied during existing-SDRF enrichment and readiness projection. The native v0.3
auditor remains strict: recommended single-cell fields may not remain blank.
