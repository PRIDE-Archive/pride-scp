# PRIDE-SCP Scientific Workspace Agent v1.0

Scientific Workspace Agent v1.0 is a redesign of the Scientific Agent harness after the v0.4 decision gate closed the narrow JSON/delta iteration line.

It is intentionally **not** Scientific Agent v0.5. The redesign changes the agent environment and information flow rather than adding another prompt exception, accession-specific rule, or validator repair.

## Why redesign

The frozen v0.4 arch4 run preserved fail-closed safety but remained flat at 212 validation errors. PXD035339 never emitted the useful isolation hypothesis that had appeared in v0.2, so the new persistent reducer had nothing to preserve or adjudicate. The model was still being asked to reason over a broad compressed evidence inventory and a large generic workspace rather than working a focused scientific problem the way an interactive research/coding agent would.

Strong interactive agents typically succeed by:

1. identifying one concrete unresolved task;
2. inspecting the most relevant source material;
3. opening larger context around promising evidence;
4. recording a durable interpretation;
5. compiling/testing it;
6. moving to the next task or escalating when evidence is insufficient.

v1.0 implements that pattern inside the existing PRIDE-SCP safety boundary.

## Architecture

```text
deterministic SDRF baseline
        |
        v
validator / compiler issues
        |
        v
Rust-owned scientific task board
        |
        +--> study-structure task first
        |
        v
one active scientific task
        |
        +--> focused evidence candidates
        |        |
        |        +--> READ_EVIDENCE_CONTEXT
        |        +--> targeted trusted search
        |
        v
sparse model WorkspaceDelta
        |
        v
Rust canonical reducer + adjudicator
        |
        v
compile / validate
        |
        +--> task resolved -> next task
        +--> evidence insufficient -> human review
```

Rust continues to own canonical state, provenance, evidence admissibility, controlled-vocabulary canonicalization, template-gap precedence, branch masking, RAW linkage, SDRF serialization, and validation.

## Scientific task board

The deterministic baseline is compiled and validated before model turn 1. Rust first creates a generic `task:study_structure` planning task, then clusters scientific validation failures into field tasks. This forces the agent to establish the source-grounded study/branch/scope model before it starts repairing individual validator fields. Task objects contain:

- stable task id;
- typed concept;
- corresponding SDRF field;
- current error codes/count;
- representative rows/messages;
- task objective;
- ranked evidence candidates;
- expanded evidence reads;
- attempts and status.

The study-structure task asks the model to establish biological/experimental branches, acquisition cardinality, field scope, and only source-supported RAW linkage. It may resolve with unresolved RAW linkage when the publication supports conceptual branches but not exact file mapping. This task is workflow planning only: branch content is still subject to the existing Rust evidence/linkage guards.

The field-task mapping intentionally covers the scientific fields for which the existing compiler already has fail-closed repair/adjudication contracts:

- `single_cell_isolation_method -> isolation_method`
- `proteomics_data_acquisition_method -> acquisition_mode`
- `organism -> organism`
- `individual -> individual`

This is generic task construction from validator semantics, not accession dispatch.

The model works one Rust-selected active task at a time. Every `WorkspaceDelta` must name the active `task_id` and a workflow `task_status` of `continue`, `resolved`, or `human_review`. For `task:study_structure`, `resolved` advances the workflow after the source-grounded study model is recorded. For validator-backed field tasks, model `resolved` means "ready to compile/test"; Rust marks the task truly resolved only when compilation/validation clears its error. Model-declared completion therefore cannot hide a failing SDRF. A task may be `open`, `investigating`, `resolved`, or `human_review`.

## Focused evidence ranking

v0.4 exposed a broad evidence inventory. v1.0 instead ranks the existing trusted evidence inventory for the active task.

For isolation, high-information method language such as hydrodynamic/on-capillary manipulation, micropipette/microaspiration, microfluidic/microwell/nanowell isolation, CellenONE, FACS, and laser capture is prioritized as **retrieval evidence**, not annotation truth.

The model still must cite the trusted E#### evidence and Rust must independently adjudicate it. Retrieval ranking cannot set an SDRF value.

## Source-context reader

New model tool:

```text
READ_EVIDENCE_CONTEXT
```

The model supplies one or more existing E#### refs. Rust then:

1. accepts only refs already present in the trusted evidence inventory;
2. searches only source files registered for that accession by `build_evidence`;
3. anchors the selected evidence excerpt into a registered manuscript, semantic/annotation bundle, or repository metadata source;
4. materializes a bounded larger context window as a new provenance-tracked E#### item;
5. makes that expanded context available in the persistent task notebook.

The reader cannot open arbitrary model-supplied filesystem paths.

Search tools remain available when focused candidates/context are insufficient.

## Persistent inspectable workspace

Each accession workspace now includes:

```text
NOTEBOOK.md
TASKS.json
state.json
state.turnNN.json
delta.turnNN.json
action_history.json
validation_history.json
adjudications.json
adjudication_history.json
trace.json
```

`NOTEBOOK.md` is regenerated from Rust-owned state and contains the task board, active task, focused evidence, canonical claims, Rust adjudications, and latest validation state. It is an inspectable scientific work artifact rather than hidden model memory.

## Task completion and human review

After compile/validation Rust refreshes the task board.

- if the task's validation failures disappear, it becomes `resolved` and Rust advances;
- if the model explicitly abstains or asks to finish while the active task remains unresolved, Rust routes that task to `human_review` and advances rather than repeatedly searching/compiling;
- if the tool budget is exhausted, the active task is routed to human review;
- if no repairable scientific task remains, the harness terminates partial rather than fabricating metadata.

This creates an explicit boundary between automation and expert review.

## Safety contracts retained

v1.0 must not regress:

- GT196/GT179 evaluation-only policy;
- deterministic structural row scaffold;
- typed scientific concepts;
- Rust-owned canonicalization/adjudication;
- template-gap precedence;
- branch heterogeneity masking;
- trusted RAW linkage only;
- no filename-derived biological identity;
- no accession-specific annotation logic;
- unchanged-draft validator guard;
- evidence provenance and bounded tool/turn budgets.

## Frozen redesign benchmark

Use the same arch4 cohort first so architecture can be compared without changing model/cohort simultaneously:

```text
PXD035339
PXD046467
PXD041388
PXD025634
```

Keep for the first v1.0 run:

```text
model=qwen3.5:9b
max_agent_turns=12
max_tool_actions=20
max_validator_cycles=3
metadata17 snapshot
frozen publication annotations
```

This is not a continuation of the v0.4 stop gate. It is the first experiment of the redesigned workspace architecture.

## Primary v1.0 observables

The first run should answer:

1. Does the initial study-structure task produce an inspectable, source-grounded study model before field repair, without inventing RAW linkage?
2. Does PXD035339's isolation task surface the hydrodynamic/on-capillary evidence in the focused candidate set?
3. Does the model read or otherwise use that evidence to emit an isolation claim?
4. Does Rust reach an explicit isolation adjudication, and canonicalize to `manual picking` only if the generic deterministic rule is genuinely satisfied?
5. Does PXD025634 retain the `microwell-chip single-cell transfer -> template_gap` result?
6. Does PXD046467 retain fail-closed RAW linkage and avoid project-wide biological collapse? If distinct HeLa/Xenopus branches are claimed, they must be distinct branch identities; notes mentioning another organism do not count.
7. Do structural errors and duplicate active claims remain zero?
8. Are remaining unresolved tasks explicitly visible as task/human-review state rather than repeated undirected search?

Total validation error count remains useful, but the decisive redesign question is whether the agent now **sees, reads, reasons from, and records the relevant scientific evidence** in a way that resembles an interactive research agent.

## Capacity diagnostic after v1.0

If v1.0 clearly surfaces the correct evidence/context but Qwen3.5 9B still fails to form the scientifically appropriate claim, do not immediately redesign the harness again. That would be evidence for a model-capacity bottleneck.

At that point, compare the **same v1.0 harness, same inputs, same evidence, and same budgets** with a stronger frontier model as a controlled capacity diagnostic. Do not change model and harness architecture simultaneously.
