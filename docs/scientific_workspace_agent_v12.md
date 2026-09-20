# PRIDE-SCP Scientific Workspace Agent v1.2

Scientific Workspace Agent v1.2 is a control-flow repair of the v1.1 explicit-command redesign. It does not add accession-specific scientific rules and does not change the frozen model, cohort, or budgets.

## Why v1.2 exists

The v1.1 frozen arch4 output proved that the explicit tool protocol was reachable: 48 `read_evidence` commands executed across the cohort and the compiler-owned PXD025634 microwell template gap was restored. However, the agent spent all 12 turns of every accession on `task:study_structure`, emitted no `edit_workspace`, `search_evidence`, or `escalate` commands, and repeatedly requested evidence that Rust had already expanded. Validation remained 212.

The concrete control-flow failure was that task bookkeeping stored the *materialized context ref* (for example E0040) but not reliably the *requested ref* (for example E0021). The action executor knew a repeat was a duplicate, but the task board still presented the original ref as unread. Qwen therefore entered an evidence-read loop.

v1.2 makes evidence gathering a finite-state protocol.

## Finite-state task loop

Each scientific task now has a Rust-owned `decision_required` flag.

```text
GATHER
  |
  | read_evidence(unread E####)
  v
DECIDE
  |\
  | +--> edit_workspace  -> reduce/adjudicate -> compile if material
  | +--> search_evidence -> if genuinely new evidence, return to GATHER
  | +--> escalate        -> human review / next task
  |
  +---- read_evidence is not present in the JSON schema
```

The model cannot bypass this merely by ignoring prompt prose: when `decision_required=true`, Rust supplies an Ollama JSON schema whose `oneOf` no longer contains `read_evidence`.

## Read receipts

For every `read_evidence` command Rust now records all of:

- the requested E#### refs;
- any E#### ref named by the action receipt;
- any newly materialized context E#### refs.

All are deduplicated into the task's `evidence_reads` ledger.

A later request for an already-consumed ref is filtered before tool execution. If no unread valid refs remain, Rust enters the decision state and returns feedback requiring an edit, genuinely different search, or escalation.

This fixes the v1.1 case where:

```text
request E0021 -> context already materialized as E0040
```

left E0021 apparently unread at the task level even though the action executor had already consumed it.

## Tool-budget accounting

`duplicate_skipped` action receipts are no longer counted as completed tool actions.

A tool action is charged only when it actually executes rather than when Rust short-circuits a repeated request. This preserves the global action budget for real evidence work.

## Search transition

`search_evidence` remains available during the decision state.

- If the search produces matched evidence refs, Rust reopens the gather state so the model may read one relevant new source-context step.
- If the search does not produce new matched evidence, the task remains in the decision state; repeated reads do not become available again automatically.

## Scientific observation/compiler boundary

The v1.1 separation remains unchanged.

The model records source-faithful scientific observations. Rust alone maps them to:

```text
canonical SDRF value
template_gap
unresolved
conflict
```

The v1.2 control-flow changes do not add new biological vocabulary mappings.

## Study-structure ownership

`task:study_structure` still comes first.

A material source-grounded `edit_workspace` resolves the study-structure task and Rust advances to the next validator-backed scientific task. If evidence remains ambiguous, `escalate` advances fail-closed. The task is no longer allowed to consume all turns by repeatedly reading the same source.

This preserves the intended ordering:

```text
understand study structure
        -> commit or escalate structure
        -> work validator-backed scientific fields
```

## Safety contracts retained

v1.2 preserves all prior contracts:

- GT196/GT179 evaluation-only;
- deterministic structural row scaffold;
- typed scientific concepts;
- Rust-owned evidence adjudication and canonicalization;
- template-gap precedence;
- branch heterogeneity masking;
- trusted exact RAW linkage only;
- no filename-derived biological identity;
- no accession-specific scientific hacks;
- unchanged-fingerprint validator guard;
- bounded turns/actions/validator cycles.

## Frozen arch4 experiment

Use exactly:

```text
PXD035339
PXD046467
PXD041388
PXD025634
```

Keep:

```text
model=qwen3.5:9b
max_agent_turns=12
max_tool_actions=20
max_validator_cycles=3
metadata17 snapshot
same publication annotations
```

## Primary v1.2 gates

1. The explicit evidence tool protocol remains exercised.
2. No task enters the v1.1 repeated-read pathology:
   - requested refs are recorded in `evidence_reads`;
   - no consecutive `read_evidence` decisions are permitted without an intervening search/edit/escalation decision;
   - `duplicate_skipped` reads should approach zero and never consume tool budget.
3. At least one `edit_workspace` or `escalate` command occurs in the frozen cohort. A run with only reads/searches is a control-flow failure.
4. `task:study_structure` must not consume all 12 turns for every accession; downstream validator-backed tasks must receive turns when study structure is committed or escalated.
5. PXD035339 must retain surfaced hydrodynamic evidence and reach Rust isolation adjudication if a source-faithful observation is committed. If the generic deterministic rule applies, canonical `manual picking` should appear without a wording-only workspace conflict.
6. PXD025634 must retain `microwell-chip single-cell transfer -> template_gap`.
7. PXD046467 must preserve branch/linkage safety. Distinct HeLa/Xenopus branches are desirable only when the model actually commits them from trusted evidence; unresolved RAW linkage must remain fail-closed.
8. PXD041388 may escalate to human review if the evidence/template mapping remains insufficient.
9. Structural non-regression:

```text
cell_identifier_invalid_or_unresolved = 0
required_integer_invalid = 0
duplicate active observation identities = 0
```

10. Validation below 212 is desirable, but the causal v1.2 question is whether the agent now progresses from **read -> decision -> edit/search/escalate** rather than looping on read.

## Next decision

If v1.2 reaches real workspace edits/escalations, preserves the safety sentinels, and still cannot improve the SDRFs despite having the right evidence, the next controlled diagnostic should keep the v1.2 harness fixed and compare against a stronger model. Do not change both the harness and model in the same experiment.
