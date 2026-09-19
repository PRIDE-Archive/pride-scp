# PRIDE-SCP Scientific Workspace Agent v1.1

Scientific Workspace Agent v1.1 is a focused repair of the v1.0 redesign. It does **not** return to the stopped v0.x prompt/validator-patch line.

The v1.0 frozen arch4 experiment changed scientific behavior in useful ways: it surfaced PXD035339 hydrodynamic evidence, produced a real Rust isolation adjudication, and formed distinct source-grounded HeLa/Xenopus branches for PXD046467 while preserving unresolved RAW linkage. However, validation remained 212, all model tool actions remained zero, PXD025634 lost the visible microwell template-gap adjudication, and PXD035339 exposed a representation conflict between an evidence-faithful method description and Rust's controlled-vocabulary mapping.

v1.1 targets those architectural failures directly.

## Design goal

Make the harness behave more like an interactive research/coding agent:

```text
Rust-selected task
      |
      v
one explicit model command
      |
      +--> ReadEvidence  -> trusted larger source context -> next turn
      +--> SearchEvidence -> trusted targeted retrieval   -> next turn
      +--> EditWorkspace -> source-faithful observations  -> Rust adjudication
      +--> Escalate      -> human review / next task
                                      |
                                      v
                         Rust compile / validator only
                         when an edit can change SDRF
```

The model does not schedule compilation or validation, does not mark validator-backed tasks resolved, and does not emit SDRF controlled-vocabulary answers as scientific observations.

## Explicit one-command protocol

Every model turn must return exactly one `AgentCommand`:

```text
read_evidence
search_evidence
edit_workspace
escalate
```

This replaces the v1.0 pattern where a model could narrate "I will read E0021 next" in notes without actually invoking the context reader.

### `read_evidence`

Inputs:

- active `task_id`;
- 1-4 existing trusted E#### references;
- reason.

Rust executes `READ_EVIDENCE_CONTEXT` immediately. The reader remains restricted to source files already registered for the accession and cannot open arbitrary model paths.

`read_evidence` never triggers SDRF compilation or a validator cycle.

### `search_evidence`

Inputs:

- active `task_id`;
- one or more typed targeted search actions;
- reason.

Rust executes only the existing allow-listed evidence actions. Search cannot mutate scientific workspace state directly.

`search_evidence` never triggers SDRF compilation or a validator cycle.

### `edit_workspace`

Inputs:

- active `task_id`;
- source-grounded branch upserts;
- scientific observation upserts/retractions;
- open-question edits;
- notes.

This is the only model command that may cause a new compile. Rust first reduces the edit into persistent state and independently adjudicates the observations. If the resulting deterministic SDRF fingerprint is unchanged, Rust skips the validator cycle.

### `escalate`

Routes the active task to human review and advances to the next task when available.

`escalate` never triggers compilation or validation.

## Scientific observations are not SDRF vocabulary values

The v1.0 PXD035339 run exposed a category error. The model proposed `hydrodynamic_loading` while the existing deterministic rule canonicalized the same source evidence to `manual picking`; persistent state interpreted the different strings as a scientific conflict.

v1.1 separates these layers explicitly.

Model-owned state now uses the term `observed_value`:

```text
ScientificObservation {
  concept_type,
  observed_value,
  scope,
  branch_id,
  status,
  evidence_refs,
  confidence,
  reason,
  supersedes_observed_value
}
```

For example, a valid observation is:

```text
an individual intact cell was manually loaded into the separation capillary
using hydrodynamic pressure
```

Rust alone decides whether the cited evidence maps to:

```text
canonical: manual picking
template_gap
unresolved
conflict
```

The model is explicitly told not to invent pseudo-vocabulary such as `hydrodynamic_loading` and not to choose a validator term merely because it expects the schema to require one.

Internally, the mature reducer/compiler still uses the historical claim structs for compatibility, but the model protocol, serialized v1.1 workspace/audit aliases, prompt, and documentation use observation terminology.

## Observation-equivalence reducer

Stable identity remains:

```text
(concept_type, scope, branch_id)
```

Different observation strings for the same identity do **not** automatically create a conflict. Before conflict creation, Rust independently adjudicates both observations against their cited evidence.

If both resolve to the same deterministic semantic outcome, for example:

```text
"manual picking"

and

"manual loading of an intact cell by hydrodynamic pressure"
```

both independently canonicalize to:

```text
manual picking
```

then Rust merges the observations/provenance instead of creating a workspace conflict. This rule is generic and based on compiler adjudication, not accession identity or string-specific special casing.

A genuinely different supported meaning still requires explicit supersession or remains an explicit conflict.

## Compiler-owned template-gap persistence

Template-gap safety is not model-owned.

If the trusted evidence inventory independently proves an isolation method outside the pinned single-cell template vocabulary, Rust now keeps an evidence-level adjudication visible even when the model has not successfully restated that observation.

This restores the required PXD025634 fail-closed behavior:

```text
microwell-chip single-cell transfer
-> template_gap
```

without forcing a false allowed isolation term and without making PXD025634 an accession-specific rule.

## Scope contract

A conceptual branch existing does not imply that every observation must be branch-scoped.

The model may use project scope only when the trusted evidence supports one invariant value across relevant material and no heterogeneous branch evidence contradicts it. It must use branch scope when biology/method genuinely differs by branch.

Rust retains final authority:

- heterogeneous branch evidence masks unsafe project-wide broadcasts;
- branch-scoped values reach SDRF rows only through trusted exact RAW linkage;
- filename-derived biological identity remains forbidden.

## Task and compile ownership

Rust still creates `task:study_structure` first and then validator-backed scientific tasks.

In v1.1:

- the model cannot set `task_status`;
- the model cannot set `next_step`;
- the model cannot schedule validators;
- Rust derives task status from evidence, edits, adjudication, and validator results;
- only a material `edit_workspace` can trigger compilation;
- an unchanged deterministic SDRF fingerprint consumes no validator cycle.

This is intended to make the three-cycle frozen validator budget behave like a real edit/test loop rather than a model-controlled retry counter.

## Persistent workspace artifacts

Per accession, v1.1 writes or preserves:

```text
NOTEBOOK.md
TASKS.json
state.json
state.turnNN.json
command.turnNN.json
command_history.json
action_history.json
validation_history.json
adjudications.json
adjudication_history.json
trace.json
```

The final audit exposes both v1.1 terminology and backward-compatible aliases:

```text
scientific_observations
observation_adjudications
claims
claim_adjudications
```

## Safety contracts retained

v1.1 does not change the safety boundary:

- GT196/GT179 remain evaluation-only;
- deterministic structural row scaffold remains compiler-owned;
- scientific concepts remain typed;
- Rust owns evidence admissibility and canonicalization;
- template-gap precedence is fail-closed;
- heterogeneous branches mask project broadcast;
- exact RAW linkage must be trusted;
- filenames never establish biological identity;
- no accession-specific annotation logic;
- unchanged-draft validator guard remains active;
- model/tool/validator budgets remain bounded.

## Frozen v1.1 architecture experiment

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

Do not change model and interaction architecture simultaneously.

## Primary v1.1 gates

1. At least one explicit `read_evidence` or `search_evidence` command must actually execute in the frozen cohort. A note saying that evidence will be read does not count.
2. PXD035339 E0021/hydrodynamic evidence must remain surfaced; an isolation observation must reach Rust adjudication. If the generic rule supports it, the result should be canonical `manual picking` without a workspace conflict caused only by observation wording versus canonical vocabulary.
3. PXD025634 must again expose `microwell-chip single-cell transfer -> template_gap` and must not substitute a false allowed term.
4. PXD046467 must retain genuinely distinct HeLa/Xenopus branches, unresolved RAW linkage, and no unsafe project-wide biological collapse.
5. PXD041388 may terminate in human review when the template vocabulary cannot safely represent the evidence.
6. Structural non-regression must remain:

```text
cell_identifier_invalid_or_unresolved = 0
required_integer_invalid = 0
duplicate active observation identities = 0
```

7. Read/search/escalate commands must not consume validator cycles. Validator cycles should occur only after publishable workspace edits that change the deterministic SDRF fingerprint.

Validation errors below the v1.0 reference of 212 are desirable, but the decisive v1.1 question is whether the **tool protocol actually executes and the observation/compiler boundary stops producing false semantic conflicts**.

## What comes after the run

If v1.1 executes real read/search commands, preserves the safety sentinels, and cleanly separates scientific observation from Rust canonicalization, then the harness environment has finally been exercised as designed.

If the correct context is then available and Qwen3.5 9B still reasons poorly, compare the exact same v1.1 harness with a stronger model as a controlled model-capacity diagnostic. Do not simultaneously change model, evidence, and harness architecture.
