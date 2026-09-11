# Release20 repair3 v3: explicit sample-role guard regression fix

This incremental overlay corrects a v2 implementation typo in the explicit row-mapping contract.

The intended contract is:

- `single cell` is accepted for single-cell rows.
- `not available` is accepted for non-single-cell rows whose more specific PRIDE sample role is not source-backed / validator-backed.
- literal `study sample` is rejected because current `sdrf-pipelines` ontology validation does not resolve it under the required PRIDE sample-type hierarchy.

The v2 code computed `non_single_study_row = sample_type == "not available"` correctly but accidentally bailed when `sample_type == "not available"`. v3 changes that guard to reject only `study sample`.

A focused regression test now verifies both sides of the contract: `not available` loads successfully, while `study sample` fails with the bounded validator-gap message.

This overlay does not change the scientific meaning of the repair3 candidates and does not alter v0.5.14.3 compatibility policy.
