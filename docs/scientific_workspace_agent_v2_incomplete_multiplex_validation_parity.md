# Scientific Workspace Agent v2 — incomplete multiplex validation parity

## Scope

This change restores the mature SDRF-generator validation contract for de-novo
reporter-multiplexed studies whose exact sample/channel mapping is unresolved.

The trigger is deliberately narrow:

- no existing SDRF is available;
- no explicit row mapping manifest was supplied;
- `relation_mode == multiplexed_cells_per_data_file`;
- `study_design.multiplex_mapping_status` ends in `mapping_unresolved`.

## Behaviour

Under those conditions the scientific-agent compiler uses the existing
`validate_incomplete_mapping_scaffold()` validator rather than validating the
one-row-per-RAW placeholder skeleton as a finalized SDRF.

The result is one causal error:

`sample_to_channel_mapping_unresolved`

instead of dependent row-level errors such as:

- `single_cell_isolation_unresolved`;
- `cell_identifier_invalid_or_unresolved`;
- `required_integer_invalid`.

No channel, cell, fraction, replicate, or biological identity is inferred.

Existing SDRFs and explicit source-grounded row mappings continue through full
row validation.

## Motivation

The accepted22 row-role-hardened audit identified five accessions with the same
state:

- PXD025481
- PXD034370
- PXD048052
- PXD048347
- PXD073405

All are high-confidence `multiplexed_cells_per_data_file` designs with unresolved
channel mapping. Their 759 placeholder RAW rows produced 3,036 dependent row
errors. This patch represents the causal blocker directly without weakening
scientific fail-closed behaviour.

The FactorGraph, isolation/acquisition semantic-fidelity gates, and row-role
hardening remain frozen.
