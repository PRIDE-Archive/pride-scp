# PRIDE-SCP Scientific Workspace Agent v1.3

Scientific Workspace Agent v1.3 is a bounded **typed-edit affordance repair** of v1.2. It does not add accession-specific scientific rules, does not loosen Rust evidence/linkage safety, and does not change the finite-state read/search controller.

## Why v1.3 exists

The frozen v1.2 harness was tested with both Qwen3.5-9B and Qwen3.6-27B on the same arch4 cohort. Both models converged on the same decisive behavior:

- zero workspace edits;
- zero targeted searches;
- eight escalations;
- unchanged 212 validation errors.

Qwen3.6-27B nevertheless demonstrated that it understood several study structures correctly. In particular, it identified heterogeneous experimental branches but then claimed that branch creation was unavailable. Source inspection showed that v1.2 already exposed `branch_upserts` inside the large generic `edit_workspace` command and Rust already supported conceptual branches with unresolved RAW linkage.

The causal failure is therefore treated as **tool/schema affordance**, not primarily model scale and not missing reducer capability.

## Design rule

The model should see exactly the material edit that makes sense for the active task.

### Study-structure task

Available commands:

```text
read_evidence
search_evidence
edit_study_structure
escalate
```

`edit_study_structure` contains:

```text
branch_upserts              required; at least one
open_question_additions
open_question_resolutions
notes
```

It cannot contain scientific observations.

A conceptual branch is valid when source evidence supports the branch even if exact RAW-file linkage is not known:

```text
linked_raw_files=[]
linkage_status="unresolved"
```

Exact RAW linkage still requires trusted source evidence explicitly naming the exact RAW basename. Filename wording remains a search hint or contradiction detector only.

### Validator-backed scientific field task

Available commands:

```text
read_evidence
search_evidence
edit_scientific_observation
escalate
```

`edit_scientific_observation` contains:

```text
observation_upserts         required; at least one
observation_retractions
open_question_additions
open_question_resolutions
notes
```

It cannot contain branch upserts.

The observation is the source-faithful scientific statement, not the SDRF controlled-vocabulary answer. Rust remains sole owner of canonicalization, template-gap decisions, scope masking, serialization, and validation.

## Isolation observation contract

v1.3 removes an unnecessary requirement that the model recover a deeper mechanical substep whenever a trusted source already explicitly names or defines an isolation method/platform.

For example, a source statement such as:

```text
evDISCO (ex vivo-digital microfluidic isolation of single cells for -Omics)
```

is sufficient to record an `isolation_method` scientific observation. It is **not** automatically a canonical SDRF value. Rust still adjudicates it to a canonical term, template gap, unresolved state, or conflict.

When the source provides a more explicit physical operation, preserve it. Do not invent pseudo-vocabulary or substitute an allowed SDRF term merely to satisfy validation.

## Trusted search semantics

`search_evidence` searches only registered/trusted sources available to the agent:

- publication evidence;
- supplements;
- structured design evidence;
- repository metadata;
- exact RAW-name evidence;
- registered knowledge-graph terms;
- conflicting trusted evidence.

It is not arbitrary web browsing. `search_blocked=false` therefore means the model may still issue a targeted trusted search.

## Preserved v1.2 controller behavior

v1.3 keeps:

- one executable command per turn;
- read receipt tracking;
- no repeated read after a successful read until a decision/search/edit/escalation;
- duplicate-read lock;
- bounded action budget;
- automatic compile/validate only after material edits;
- unchanged-draft fingerprint guard;
- human-review fallback.

## Preserved scientific safety

Unchanged:

- GT is evaluation-only and never runtime input;
- Rust owns evidence admissibility;
- Rust owns canonical SDRF vocabulary;
- compiler-owned template-gap precedence;
- branch heterogeneity masks unsafe project broadcasts;
- exact RAW linkage requires trusted source evidence;
- structural fields remain compiler-owned;
- no filename-derived biological identity;
- no accession-specific repair rules.

## Frozen v1.3 experiment

Use the same arch4 cohort:

```text
PXD035339
PXD046467
PXD041388
PXD025634
```

Use the same:

```text
model=qwen3.6:27b
max_agent_turns=12
max_tool_actions=20
max_validator_cycles=3
metadata17 snapshot
publication annotation root
```

Only the v1.3 typed edit affordance changes.

## Primary progress gate

The first v1.3 run is not allowed to become another open-ended iteration cycle. The audit defines an explicit stop gate.

Required progress:

```text
study_structure_edit_commands > 0
scientific_observation_edit_commands > 0
at least one sentinel scientific state moves forward
all safety gates remain clean
```

Sentinel progress includes at least one of:

- PXD035339 source-faithful isolation observation plus Rust adjudication;
- PXD046467 distinct HeLa/reference and Xenopus conceptual branches;
- PXD041388 evDISCO isolation observation recorded;
- PXD025634 source-faithful isolation observation recorded while the compiler-owned microwell template gap remains intact.

Validation errors below 212 are desirable but secondary for this iteration. v1.3 first tests whether the model can finally express the scientific state it already understands.

## Mandatory stop rule

If the v1.3 frozen arch4 audit reports:

```text
narrow_iteration_stop_rule_triggered=true
```

then **do not implement a v1.4 prompt/schema tweak and do not run another larger model against the same narrow harness**. Reassess the architecture at a higher level instead.

This stop rule exists specifically to prevent repeated low-yield iterations with no material scientific-state progress.
