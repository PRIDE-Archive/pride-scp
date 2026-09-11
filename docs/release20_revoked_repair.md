# Release20 revoked-SDRF repair and scientific-guard hardening

This iteration repairs three release20 exact hashes that were revoked after upstream adversarial review:

- `PXD019515` — HeLa / spinal-neuron metadata were collapsed together and a truncated nanoPOTS protocol fragment was written into a version field.
- `PXD019958` — synthetic `U87_donor`, biological identity on zero-cell controls, and blank single-cell recommended metadata.
- `PXD062702` — HeLa method-development acquisitions and Xenopus biological single-cell runs were collapsed into one human/HeLa/DIA metadata model.

The repair is deliberately separate from compatibility normalization. `pride-scp-bigbio-readiness-v0.5.14.3` remains frozen.

## Generic prevention layer

`crates/pride-scp-sdrf` draft validation and `scripts/sdrf_scientific_guard.py` now fail closed on:

1. `characteristics[individual]` duplicating organism-part / cell-line / cell-type / developmental-stage semantics;
2. synthetic `<cell-line>_donor`-style individual identifiers;
3. explicit DDA/DIA filename tokens contradicting acquisition metadata;
4. zero-cell/blank controls inheriting concrete individual/cell-type/cell-line identity;
5. blank single-cell preparation/carrier/reference fields where a reserved word is required;
6. truncated methods prose copied into narrow model/version metadata fields;
7. a multi-organism or multi-organism-part PRIDE project collapsed into one uniform candidate value.

These checks only detect contradictions or missing representation. They do not infer row-level biology.

The readiness normalizer additionally performs only two schema-safe reserved-word operations:

- blank `comment[sample preparation batch]` -> `not available`;
- in an all-label-free single-cell candidate, blank `comment[carrier channel]` / `comment[reference channel]` -> `not applicable`.

## Repair safety contract

`scripts/repair_release20_revoked_sdrfs.py` refuses to run unless each input matches the revoked frozen release20 SHA-256. Every changed cell is recorded with a reason in a repair manifest, and every repaired candidate must pass the generic scientific guard before it is emitted.

A repaired candidate is **not approved** merely because the repair script succeeds. It must pass the hardened readiness run and a new independent exact-hash review.

`scripts/update_release20_repair_prs.py` therefore requires a fresh independent approval manifest before it will publish bytes to GitHub.

- `PXD019958` updates draft PR #463.
- `PXD062702` updates draft PR #476.
- `PXD019515` opens a new corrective PR because #462 is already merged.

## DIA validator drift

This iteration does not modify the specification-correct DIA representation:

`NT=Data-independent acquisition;AC=PRIDE:0000450`

The known `sdrf-pipelines` issue #345 remains a validator/tooling issue and is not worked around by corrupting the SDRF metadata.
