# Agentic SDRF annotation v0.4

This iteration is a narrow correction to the relation/cardinality contract exposed by the v0.3 mixed8 diagnostic. It does not add accession-specific rules and it does not weaken biological row-linkage provenance requirements.

## Why v0.4 exists

The v0.3 relation diagnostic showed that the design agent was usually able to infer acquisition cardinality. Several accessions finished with an explicit high-confidence `one_cell_per_data_file` relation at `scope=branch`, while the deterministic study-design scaffold independently reported the same concrete mode. Rust nevertheless treated every non-project relation scope as an unconditional veto and serialized the final relation as `uncertain`.

The same diagnostic showed a second semantic failure: biological group identity was still leaking into relation reasoning. A dataset could move from `one_cell_per_data_file` to `mixed` simply because control-vs-stroke, organism, sex, or cell-line linkage to exact files was missing. Those are row-identity questions, not acquisition-cardinality questions.

Finally, typed retrieval worked, but the planner occasionally emitted `match=raw_exact` under `SEARCH_REPOSITORY_METADATA`. v0.3 correctly rejected that combination, but the rejection wasted a bounded evidence round.

v0.4 fixes those three issues without relaxing the mapping contract.

## 1. Relation consensus arbitration

Relation arbitration is now compatibility-based rather than scope-only.

The model relation `M` is compared with the trusted deterministic relation hint `D`:

```text
M == D and both are concrete
    -> ALLOW_CONFIRM

M == mixed
    -> BLOCK_PROJECT_RELATION

M == unresolved with evidence-backed acquisition-cardinality uncertainty
    -> BLOCK_UNRESOLVED

M conflicts with D
    -> BLOCK_CONFLICT

missing/default model relation
    -> DEFER
```

A concrete branch-scoped relation that agrees with deterministic evidence no longer becomes `uncertain` merely because its scope is `branch`.

The result is logged with the arbitration state. When model and deterministic relation agree, the review records:

```text
agent_relation_consensus_confirms_scaffold
```

## 2. Acquisition cardinality is separate from biological row identity

The design prompt now defines `relation_assessment` as answering only:

> How many biological acquisition units/channels are represented by each data file?

It does **not** answer which organism, condition, sex, cell type, treatment, replicate, or other biological attribute belongs to the file.

Therefore:

```text
unknown control-vs-stroke mapping
    != mixed relation

unknown HeLa-vs-THP1 mapping
    != mixed relation

unknown organism mapping
    != mixed relation

unknown sex/replicate mapping
    != mixed relation
```

`mixed` is reserved for genuine acquisition-structure differences such as one-sample-per-file acquisitions coexisting with pooled/composite or reporter-multiplexed acquisitions.

The legacy mode name `one_cell_per_data_file` is interpreted as one biological acquisition unit/sample per data file. The acquisition unit may be a cell, fiber, digest, or other sample; the mode does not itself assert a biological identity.

If the same acquisition cardinality applies across all repository acquisitions, the model is instructed to use `scope=project` even when biological groups differ. `scope=branch` is reserved for cases where acquisition cardinality is established only for one branch.

## 3. Branch relation does not authorize global row fabrication

v0.4 deliberately separates *relation resolution* from *row serialization permission*.

A relation may be resolved as:

```text
one_cell_per_data_file / branch
```

while exact branch membership or biological row linkage remains unresolved.

In that state:

- the proposal/audit retains the concrete relation mode;
- `mapping_status` becomes `relation_resolved_branch_mapping_unresolved`;
- generation uses an unresolved row skeleton rather than filename-derived cell/source identities;
- the review emits `sample_to_file_branch_mapping_unresolved`;
- the accession remains incomplete until branch membership or explicit row linkage is source-grounded.

Only a project-scoped one-per-file relation, a complete explicit mapping manifest, or an authoritative existing SDRF may authorize global one-row-per-file serialization.

This preserves the historical linkage contract: a run key or filename token is not biological identity.

## 4. Typed RAW-action normalization

Before duplicate detection and execution, Rust now canonicalizes a typed query when all of the following are true:

```text
query.match == raw_exact
requested action != SEARCH_EXACT_RAW_NAME
query.value exactly matches a repository RAW/mzML acquisition basename
```

The effective action becomes:

```text
SEARCH_EXACT_RAW_NAME
```

The result records:

```json
"normalized_from_action": "SEARCH_REPOSITORY_METADATA"
```

and the summary includes `action_normalized`.

This normalization happens before action-history deduplication, so the same RAW query cannot consume multiple rounds simply by changing the requested action label.

Exact-RAW retrieval searches the trusted repository project/files JSON as well as registered publication/annotation sources. Arbitrary semantic strings still cannot use this path.

## 5. Readiness remains orthogonal

v0.4 keeps the v0.3 readiness dimensions:

```text
mapping_status
template_status
metadata_status
evidence_status
agent_terminal_status
```

A concrete acquisition relation does not automatically mean the biological row mapping is resolved.

For example:

```text
relation_mode = one_cell_per_data_file
mapping_status = relation_resolved_branch_mapping_unresolved
```

is a valid and intentional state.

## Safety invariants retained

- filenames remain search hints, never biological truth;
- GT accessions/labels are not runtime truth;
- concrete metadata requires provenance;
- explicit group/row scope still blocks ordinary project-wide metadata broadcast;
- branch-scoped relation consensus does not authorize filename-derived biological identifiers;
- multiplexed relations still require locally linked reporter/channel evidence;
- mixed relation remains a legitimate blocker when acquisition regimes truly differ;
- unresolved row linkage remains a valid result;
- retrieval remains bounded to three rounds;
- no accession-specific mappings or exceptions are added;
- Ollama structured calls remain `think=false`.

## Regression gate

Use the same frozen mixed8 cohort.

Expected relation behavior:

- PXD015175: concrete one-per-file relation may be retained; biological group linkage may remain unresolved;
- PXD035339: concrete one-per-file relation may be retained; biological group linkage remains independently guarded;
- PXD041388: missing control-vs-stroke mapping must not itself force `mixed`;
- PXD046467: concrete one-per-file relation may be retained;
- PXD025634: relation may resolve while organism conflict remains separate;
- PXD030607: relation may resolve while organism/instrument mapping remains separate;
- PXD001641: may legitimately remain `mixed` if single-fiber and pooled/library acquisition regimes are both supported;
- PXD045844: may legitimately remain `mixed` if the aggregate/comparison acquisition is structurally distinct, while the template-vocabulary gap remains orthogonal.

Do not proceed to Residual73 until this same mixed8 demonstrates the intended separation between relation/cardinality and biological row mapping.
