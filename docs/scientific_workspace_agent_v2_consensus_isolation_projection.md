# Scientific Workspace Agent v2 — consensus-safe isolation projection

## Scope

This change closes a deterministic projection gap without adding a new model call
or weakening factor/row linkage safeguards.

A row may already be deterministically classified as `single cell` while its
exact RAW-to-regime identity remains unnecessary for a field whose value is
identical across every compatible accepted single-cell regime. Isolation method
is projected only under a strict consensus rule.

## Rule

Consensus isolation projection is enabled only in the frozen
`study_factor_graph_v2_row_role_hardened` path and only when all of the following
hold:

1. the draft is de-novo (`existing_sdrf_path` is empty);
2. no explicit row mapping manifest is present;
3. the deterministic row scaffold emitted no
   `scientific_agent_raw_file_role_unresolved` error;
4. the study is not an unresolved `multiplexed_cells_per_data_file` design;
5. at least one branch-scoped accepted regime is explicitly `single cell`;
6. every such single-cell regime has an isolation claim adjudicated by Rust as
   `Canonical`;
7. all canonical isolation values are identical.

If any condition fails, projection is skipped and the draft remains fail-closed.

When the rule holds, the common canonical isolation value is written only to
rows whose deterministic sample type is already `single cell` and whose
isolation field is unresolved. Existing concrete isolation values and
non-single-cell rows are never overwritten.

Exact source-backed branch linkage is applied before consensus projection, so
exact linkage always has precedence.

## Scientific safety

The rule never:

- chooses a material or regime from filename semantics;
- infers reporter-channel identity;
- resolves an unknown RAW role;
- broadcasts across conflicting single-cell regimes;
- substitutes a template-compatible value for a template gap;
- overwrites deposited SDRFs or explicit mappings.

The projection warning is:

`scientific_agent_consensus_single_cell_isolation_projected`

and records the canonical value, accepted single-cell regime IDs, projected row
count, and supporting evidence references.

## Tests

Synthetic regression coverage includes:

- one canonical single-cell regime -> project;
- multiple single-cell regimes with the same canonical isolation -> project;
- conflicting canonical isolation values -> fail closed;
- any unresolved/template-gap isolation regime -> fail closed;
- empty/bulk/QC rows -> unchanged;
- unresolved multiplex mapping -> unchanged;
- unresolved RAW-role state -> unchanged;
- existing SDRF or explicit row mapping -> unchanged.

The current accepted22 cohort audit identifies PXD073250 as the only strict
real-world regression sentinel, but no accession identifier appears in the
implementation.
