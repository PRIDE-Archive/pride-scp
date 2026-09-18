# PRIDE-SCP Scientific Annotation Agent v0.4

Scientific Agent v0.4 is the final narrow harness iteration before the mandatory architecture review. It changes one ownership boundary only: the model no longer returns a complete workspace snapshot. Rust owns the canonical scientific workspace and the model proposes bounded `WorkspaceDelta` mutations.

## Motivation

Scientific Agent v0.2 reduced the frozen arch4 benchmark from 920 to 212 validation errors by preserving the deterministic SDRF baseline, introducing typed concepts, protecting structural rows, and enforcing branch-safe scope. v0.3 added compiler-owned scientific evidence adjudication, but remained at 212 errors while increasing total agent turns from 29 to 35 and tool actions from 22 to 32.

The v0.3 trace exposed a state-management failure. A useful PXD035339 isolation hypothesis existed in v0.2, but disappeared in v0.3 because every model response reconstructed and replaced the complete workspace. Rust therefore never had an opportunity to adjudicate that retained claim.

v0.4 makes state persistence an environment property rather than a model-memory requirement.

## Rust-owned canonical workspace

Per turn:

```text
canonical Rust workspace
        +
model WorkspaceDelta
        |
        v
deterministic reducer
        |
        v
new canonical Rust workspace
```

The model sees the canonical workspace as read-only context. It returns only a delta containing:

- `branch_upserts`;
- `claim_upserts`;
- `claim_retractions`;
- `open_question_additions` / `open_question_resolutions`;
- `conflict_additions` / `conflict_resolutions`;
- `next_evidence_actions`;
- `next_step`;
- `notes`.

Omission is never deletion. Prior claims, branches, conflicts, and open questions survive until an explicit mutation resolves them.

Per-delta schema limits bound model output, but canonical state is not truncated by those limits. This prevents an old scientific item from disappearing merely because later turns accumulated additional state.

## Stable claim identity and deterministic reducer

An active claim is identified by:

```text
(concept_type, scope, branch_id)
```

Reducer behavior is deterministic:

1. same identity + same value merges evidence, reason, status, and confidence; evidence refs are deduplicated;
2. same identity + changed value + exact `supersedes_value` replaces the active value;
3. same identity + changed value without valid supersession retains the active value and creates a structured conflict;
4. a claim is directly deleted only through `claim_retractions`;
5. a recorded conflict is cleared only by an explicit conflict resolution or valid supersession.

Unresolved workspace conflicts block publication for that claim identity. Model proposal/status/evidence stay in canonical workspace state; Rust adjudication remains a separate compiler-owned record.

## Evidence changes and adjudication

Search actions mutate the trusted evidence inventory, never the workspace snapshot. After every applied delta and again after every evidence search, Rust re-adjudicates all retained claims.

The agent receives adjudication feedback only when the stable identity's proposed value, evidence refs, or adjudication outcome changes. Identical adjudication results are not repeated on every turn.

Artifacts now include:

```text
workspaces/<ACC>/delta.turnNN.json
workspaces/<ACC>/state.turnNN.json
workspaces/<ACC>/state.json
workspaces/<ACC>/adjudications.json
workspaces/<ACC>/adjudication_history.json
workspaces/<ACC>/trace.json
```

`trace.json` includes both model deltas and Rust-owned state snapshots, allowing direct proof that a claim discovered at turn N survives at turn N+k even when it is omitted from intervening deltas.

## Safety contracts retained

v0.4 intentionally keeps the v0.2/v0.3 scientific and structural safety boundaries:

- deterministic baseline before model turn 1;
- deterministic structural row scaffold;
- typed scientific concepts only;
- compiler-owned controlled-vocabulary canonicalization;
- template-gap precedence over unsafe substitution;
- branch heterogeneity masking unsafe project broadcasts;
- trusted exact-RAW linkage only;
- no filename-derived biological identity;
- unchanged-draft fingerprint guard;
- GT196/GT179 evaluation-only policy.

No accession-specific scientific rule is added by v0.4. In particular, the existing generic hydrodynamic single-cell evidence rule remains the only path by which suitable source evidence can canonicalize to `manual picking`.

## v0.4 regression tests

The harness tests cover:

- omitted hydrodynamic isolation claim persists across a later delta and remains available for Rust adjudication;
- the existing deterministic hydrodynamic evidence rule can adjudicate the preserved hypothesis to `manual picking` when its evidence really satisfies that rule;
- same-value upserts merge and deduplicate evidence;
- changed values without supersession produce a conflict and retain the active value;
- explicit supersession replaces an active value;
- explicit retraction is required to remove a retained claim;
- omitted branches persist and unresolved RAW linkage stays unresolved;
- identical adjudication feedback is emitted only once;
- the structured-output schema is delta-only and does not expose full-state `claims`, `branches`, or `relation` replacement fields;
- existing template-gap, branch-linkage, structural-row, dynamic-bootstrap, and unchanged-draft tests remain in place.

## Frozen arch4 decision run

Use exactly:

```text
PXD035339
PXD046467
PXD041388
PXD025634
```

Keep Qwen3.5 9B, the metadata17 snapshot, publication annotations, and the exact budgets:

```text
max_agent_turns = 12
max_tool_actions = 20
max_validator_cycles = 3
```

The decision run must verify that PXD035339's isolation hypothesis survives without model repetition and is actually adjudicated; PXD025634 retains the microwell template gap; PXD046467 retains HeLa/Xenopus branch safety and unresolved RAW linkage; structural invalid-cell/integer regressions and duplicate active claims remain zero; and turn/action churn improves relative to v0.3 where possible.

## Mandatory stop rule

v0.4 is not the start of another incremental harness series. If the frozen arch4 result is flat or only marginally better, do not implement a small v0.5 patch. Stop and review model capacity, evidence retrieval, tool surface, scientific intermediate representation, true editable-workspace architecture, template limitations, and the human-review boundary.
