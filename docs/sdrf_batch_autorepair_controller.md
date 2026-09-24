# Bounded SDRF batch autorepair controller

## Purpose

The controller closes the orchestration gap between the scientific workspace agent and the frozen BigBio readiness gate. It is designed for residual PRIDE-SCP cohorts where deterministic reconstruction or a trusted deposited SDRF exists but readiness exposes scientific blockers that previously required accession-by-accession manual debugging.

The controller does **not** weaken readiness, `parse_sdrf`, `sdrf-skills`, scientific guards, or hash-bound review. It also does not use ground-truth labels.

## Flow

```text
candidate or de-novo baseline
  -> readiness diagnostic bridge
  -> bounded scientific workspace agent
  -> audited candidate
  -> frozen v0.5.14.8 readiness
  -> at most two repair rounds
  -> terminal queue
```

The reusable scripts are:

```text
scripts/sdrf_autorepair_tasks.py
scripts/sdrf_batch_autorepair.py
```

The Rust scientific agent accepts optional external readiness tasks from:

```text
PRIDE_SCP_SCIENTIFIC_AGENT_READINESS_TASKS_DIR
```

Each file is `<ACCESSION>.json` and contains an accession plus one or more `readiness:*` task specifications. Readiness tasks are distinct from local validator tasks and remain open until the outer readiness controller reruns the frozen gate.


## Runtime isolation

The controller can run the repair agent and frozen readiness gate from different immutable Singularity images:

```text
--agent-sif       image containing the current scientific-agent repair implementation
--readiness-sif   frozen image containing the approved v0.5.14.8 readiness policy
```

On Codon the controller is intended to run under the host Python process and launch both images with `singularity exec`. This preserves a frozen readiness implementation while allowing the repair agent to evolve. The controller records both SIF paths and SHA256 values in `autorepair_summary.json`.

## Repair classes

The first bridge covers:

- single-cell isolation placeholders / source-backed template gaps;
- multi-organism-part collapse warnings;
- acquisition conflicts and DDA/DIA contradictions;
- unresolved multiplex/reporter identity when repeated acquisitions have no informative label column;
- trusted-source study-scope / provenance conflicts from Stage1 decisions;
- acquisition/isolation failures surfaced by `parse_sdrf`.

This is deliberately generic. No PXD accession appears in runtime logic.

## Evidence-backed concrete acquisition correction

A trusted existing SDRF normally preserves concrete deposited values. One narrow exception is now available to the scientific workspace agent for acquisition-mode repair:

1. an acquisition repair task must be active and have been worked by the agent;
2. the model must record a project-scoped acquisition observation supported by field-relevant trusted evidence;
3. Rust must deterministically adjudicate that observation to one canonical acquisition mode;
4. the existing SDRF must contain exactly one concrete acquisition value across all rows;
5. only then may Rust replace that one uniform value across the rows.

Filename tokens alone can never authorize this override. If existing acquisition values are heterogeneous, the broadcast is rejected and a row/branch mapping is required instead.

This enables source-backed correction of a uniformly wrong deposited DDA/DIA field while remaining fail-closed for mixed designs.

## Template gaps

A source-supported experimental method that the pinned SDRF template cannot faithfully represent is a successful terminal classification:

```text
template_gap
```

The agent must not substitute an unrelated allowed vocabulary value merely to satisfy the validator.

## Multiplex mapping

The task bridge detects repeated acquisition groups with multiplex roles but no informative `comment[label]` value across any duplicate label column. The agent is instructed to search publication/supplement/structured-design evidence for exact reporter mapping.

Row order is never evidence. If exact reporter identity cannot be established, the controller terminates the accession as:

```text
row_mapping_required
```

rather than inventing channels.

## Provenance conflicts

When a registered Stage1 `human_review` decision explicitly says that trusted evidence does not support single-cell proteomics for the accession, the bridge emits a `study_structure` provenance task. The agent must reconcile accession/publication/repository linkage from registered trusted sources. Unresolved conflicts terminate as:

```text
provenance_conflict
```

## Bounded execution

Recommended defaults:

```text
max_repair_rounds=2
max_agent_turns=4
max_tool_actions=6
max_validator_cycles=2
```

The controller stops on:

- `submission_ready`;
- `needs_independent_review` with no remaining repair task;
- `template_gap`;
- `row_mapping_required`;
- `provenance_conflict`;
- unchanged candidate fingerprint after a source-grounded repair attempt;
- repair-round exhaustion;
- infrastructure/tool failure.

## Outputs

Every cohort produces:

```text
autorepair_summary.json
autorepair_ledger.tsv
queues/
round01/
round02/
```

The ledger records accession, rounds, model turns, tool actions, validator cycles, input/output candidate hashes, projected hash, terminal state, reason, review state, submission state, and artifact paths.

Independent review remains a separate exact-hash phase. The controller intentionally stops at `needs_independent_review` until the batch reviewer is connected in a later layer; it does not self-approve its own scientific edits.

## Bridge/controller v2

Bridge v2 is driven by the Tier1-18 cohort gap audit. It keeps the frozen
BigBio readiness policy unchanged and changes only orchestration/task coverage.

Key changes:

- run a frozen-readiness diagnostic preflight before repair round 1;
- merge fresh preflight diagnostics with optional historical seed diagnostics;
- make both bounded repair rounds readiness-informed;
- expose additional scientific reasoning concepts for row-scoped metadata without
  adding unsafe project-wide compiler mappings:
  - `cell_identifier`
  - `biological_replicate`
  - `dissociation_method`
- bridge recurring readiness/guard classes into tasks:
  - cell identifier placeholders;
  - biological replicate required values;
  - cell-line / Cellosaurus validation and skills issues;
  - organism and organism-part collapse;
  - DDA/DIA acquisition conflicts;
  - Q Exactive CID/HCD conflicts;
  - empty-control biological identity leakage;
  - individual semantic leakage;
  - parse-sdrf acquisition/isolation/cell-line/Cellosaurus failures.

The new row-scoped concepts are intentionally **not** mapped to project-wide
`SdrfProposal` fields. If trusted evidence cannot establish an exact safe mapping,
the controller terminates in `row_mapping_required`, `ontology_mapping_required`,
or another explicit fail-closed queue rather than broadcasting a value.

The controller also emits a `bridge_gap` terminal queue if frozen readiness is
still blocked after the bounded rounds and no repair task represents that blocker.
That queue is an orchestration-development signal and must not be interpreted as
scientific exhaustion.

The v2 sequence is:

```text
candidate
  -> frozen readiness diagnostic preflight (no LLM repair round consumed)
  -> bridge-v2 task board
  -> repair round 1
  -> frozen readiness
  -> refreshed task board
  -> repair round 2
  -> frozen readiness
  -> terminal queues
```

No readiness, scientific-guard, ontology, or M+R policy is weakened.

## Runtime-domain separation for frozen readiness

The bridge has two distinct Python execution domains:

- `--python` is the outer/orchestrator Python. It may be a host-side dispatcher.
- `--readiness-python` is the Python executable *inside* `--readiness-sif` and defaults to `python`.

When `--readiness-sif` is supplied, the bridge invokes readiness as:

```text
singularity exec <readiness.sif> <readiness-python> <readiness-script> ...
```

It must never propagate a host-side Python wrapper into the frozen readiness image. This separation prevents nested-dispatch/runtime lookup failures while leaving the scientific and readiness policies unchanged.
