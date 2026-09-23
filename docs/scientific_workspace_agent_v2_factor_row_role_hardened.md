# Scientific Workspace Agent v2 — factor row-role hardening

## Purpose

This iteration is a deterministic compiler hardening step after the frozen FactorGraph Stage 1 and semantic-fidelity bridge.

It addresses a specific row-construction bug: under `one_cell_per_data_file`, the prior deterministic scaffold converted `RawFileRole::Unknown` into `SingleCell`. That could serialize `characteristics[sample type] = single cell` for PRIDE files whose row role was not actually resolved.

The bug was exposed by PXD057685, where the deposited inventory contains:

- `200pgHeLa_raw.zip`
- `500pgHeLa_raw.zip`
- `xenopus.zip`

while the accepted factor graph contains both a commercial HeLa digest branch and a Xenopus single-cell branch, with no exact RAW links.

## New mode

```text
study_factor_graph_v2_row_role_hardened
```

Harness:

```text
pride-scp-scientific-workspace-agent-v2-factor-row-role-hardened
```

The previous semantic-fidelity mode remains unchanged for reproducibility.

## Invariants

1. `RawFileRole::Unknown` is never promoted to `SingleCell` merely because the project-level relation hint is `one_cell_per_data_file`.
2. Unknown rows retain unresolved sample type / cell identifier / cells-per-well values and emit:

```text
scientific_agent_raw_file_role_unresolved
```

as an error, so scientific uncertainty cannot disappear merely by avoiding a template-triggering value.
3. Fraction identifier and technical replicate scaffolding remain deterministic.
4. Existing explicit file-role signals remain unchanged: single-cell, few-cell, blank, QC, and bulk/reference.
5. Compact PRIDE mass-amount archive names such as `200pgHeLa_raw.zip` and `500pgHeLa_raw.zip` are treated as bulk/reference inputs, matching the existing policy for delimiter-bounded pg/ng amount tokens.
6. The mode does **not** infer biological branch identity from names such as `xenopus.zip` or `HeLa...`.
7. Branch-scoped scientific claims still require trusted RAW-to-branch linkage before row projection.
8. Isolation and acquisition semantic-fidelity hardening remain active.

## Expected PXD057685 behavior

```text
200pgHeLa_raw.zip   -> bulk control / isolation not applicable
500pgHeLa_raw.zip   -> bulk control / isolation not applicable
xenopus.zip         -> row role unresolved; do not assume single cell
```

This should replace the previous three `single_cell_isolation_unresolved` errors with one explicit row-role/linkage blocker rather than manufacturing a validator-clean SDRF.

## Non-goals

This mode does not:

- infer `xenopus.zip -> M002/R002` from the filename;
- broadcast `manual picking` to all rows;
- change the frozen Stage-1 factor graph;
- invoke an LLM;
- add validator retry loops.

The next scientific step after this hardening is explicit deterministic RAW-to-factor linkage only where trusted deposited/source evidence supports it.

## Runtime overwrite hardening (fix1)

The first regression4 run exposed an ordering bug: an earlier project-level scaffold could
pre-populate generated rows with `sample type = single cell` before deterministic row-role
hardening ran. The hardening correctly detected `Bulk` or `Unknown`, but its original
`set-if-unresolved` writes could not replace the stale role fields.

In row-role-hardened mode, deterministic file-role classification is now authoritative for
generated row-role fields. Recognized bulk/QC/blank/few-cell/single-cell roles overwrite
prior generated scaffold values, and `Unknown` explicitly resets sample type, isolation,
cell identifier, and cells-per-well to unresolved values before emitting
`scientific_agent_raw_file_role_unresolved`. Existing/deposited SDRFs remain protected
because deterministic row scaffolding is skipped whenever an existing SDRF is present.

Regression tests now start from the real pre-populated state so this ordering bug cannot
recur silently.

## Trusted partial SDRF mapping evidence

Row-role-hardened compilation may now consume a **strict repository-subset SDRF**
from the existing `--resolved-sdrf-dir` as trusted multiplex mapping evidence.
This is intentionally narrower than the preservation-first existing-SDRF path.

A resolved SDRF is eligible only when all of the following hold:

- it is a usable SDRF with mapped `comment[data file]` rows;
- every SDRF data file resolves to the current frozen PRIDE RAW inventory;
- it covers at least one, but fewer than all, current repository RAW files;
- the FactorGraph relation mode is `multiplexed_cells_per_data_file`;
- no explicit row-mapping manifest is active;
- the row-role-hardened harness is running.

For eligible candidates, deposited rows replace the unresolved generated skeleton
for exactly matched repository RAW files. One RAW may therefore expand into many
source-grounded channel rows. Generated rows are retained only for repository
RAW files not represented in the deposited SDRF. Deposited-only RAW files,
ambiguous RAW aliases, duplicate normalized headers, and missing fallback RAW
rows fail closed.

The mapping-level `sample_to_channel_mapping_unresolved` guard is suppressed
only after this trusted partial fusion succeeds. All ordinary row-level
validators then run on the fused candidate. This means uncovered auxiliary RAW
files can still expose concrete metadata blockers rather than being hidden by
the former global multiplex error.

The feature does **not**:

- infer reporter channels from file names;
- infer channel identity from row or channel order;
- treat a partial SDRF as complete repository coverage;
- weaken the existing bidirectional full-SDRF preservation gate;
- alter the one-row-per-RAW explicit mapping manifest contract.

The initial evidence-backed pilot was PXD020586, PXD040455 and PXD063590. In
that pilot the trusted deposited SDRFs cleared `sample_to_channel_mapping_unresolved`
for all three and made PXD063590 locally valid. PXD020586 and PXD040455 retained
only concrete `required_integer_invalid` errors on repository-only auxiliary
files not represented in the deposited SDRFs.
