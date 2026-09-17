# Agentic SDRF annotation v0.5 — bounded validator-driven evidence repair

## Status

This iteration keeps the accepted v0.4 acquisition-cardinality / relation arbitration unchanged and adds one bounded post-draft scientific repair stage.

Versions:

- generator: `pride-scp-sdrf-v0.5.3`
- design agent: `pride-scp-design-agent-v0.4` (unchanged)
- validator-repair agent: `pride-scp-validator-repair-v0.1`
- design evidence rounds: 3
- validator repair rounds: 1
- validator repair tasks/accession: 4
- queries/repair task: 4
- auditor: `pride-scp-sdrf-auditor-v0.3`
- Ollama/Qwen structured calls: `think=false`, temperature 0

No accession-specific runtime rules are introduced.

## Why this iteration

The v0.4 frozen mixed8 showed that acquisition cardinality was no longer the bottleneck. The remaining validation failures collapsed into a small set of repeated scientific errors:

- unresolved single-cell isolation method repeated over many rows;
- multi-organism projects collapsed to one organism candidate;
- non-individual semantic concepts propagated into `characteristics[individual]`;
- acquisition-mode contradictions detected from RAW filename tokens versus metadata.

The architecture therefore needed the originally intended post-draft loop:

```text
design assessment
  -> proposal
  -> deterministic row construction
  -> scientific validation
  -> aggregate repeated failures by field
  -> one bounded evidence-repair pass
  -> revise only source-supported affected fields
  -> re-render
  -> revalidate
  -> resolve or remain fail-closed
```

## Error clustering

Rust converts repeated row errors into at most four `ValidatorRepairTask` objects.

Current repair classes:

| validation error | repair field |
| --- | --- |
| `single_cell_isolation_unresolved` | `single_cell_isolation_method` |
| `multiorganism_project_collapsed_to_single_candidate_organism` | `organism` |
| `individual_duplicates_nonindividual_semantic_field` | `individual` |
| `data_file_name_acquisition_contradiction` | `proteomics_data_acquisition_method` |

Each task records:

- unique error codes;
- total repeated error count;
- a bounded sample of affected row numbers;
- a bounded sample of representative messages.

The LLM never receives tens or hundreds of duplicate validator messages.

## Deterministic individual semantic sanitization

Before de-novo row serialization, Rust checks the project proposal for a dataset-level `individual` value that:

- duplicates organism / organism part / disease / cell type / sample type; or
- is an obvious non-individual concept such as `Embryo`, `Brain`, `Neuron`, `HeLa`, `K562`, etc.

Such a value is replaced with:

```text
not available
```

and its evidence refs are removed. A warning is recorded:

```text
validator_repair_individual_semantic_sanitized
```

This sanitizer does not create donor identifiers and does not use filenames.

## Validator-repair agent

The repair agent is intentionally narrower than the design agent.

It sees:

- clustered repair tasks;
- the current proposal;
- the final v0.4 design assessment;
- trusted evidence inventory;
- bounded repair-action history.

It may return per-field decisions:

```text
set_project_value
keep_unresolved
template_gap
row_mapping_required
no_change
```

with scope:

```text
project
role
row
unresolved
```

Only `single_cell_isolation_method` and `proteomics_data_acquisition_method` are eligible for automatic project-level repair, and only when all of the following hold:

1. the repair decision is `set_project_value`;
2. repair scope is `project`;
3. trusted E#### refs are present;
4. every cited ref is relevant to that field;
5. the prior design assessment does not explicitly establish group/row scope;
6. the proposed value passes deterministic semantic checks;
7. the field is not blocked by a known template vocabulary gap.

`organism` and `individual` are never project-assigned by the repair LLM when the validator identifies a row/group mapping problem.

## Isolation-method repair

The repair prompt distinguishes evidence-supported project-wide isolation from branch-specific or unrepresentable methods.

A concrete isolation value can be repaired only when the same template-supported method applies to all affected study/single-cell rows.

If evidence supports a real method outside the pinned single-cell template vocabulary, the decision must be:

```text
template_gap
```

rather than substituting a nearby template term.

## Multi-organism guard

A `multiorganism_project_collapsed_to_single_candidate_organism` task can request evidence retrieval, but the repair stage cannot turn one organism mention into a project-wide organism assignment.

Safe outputs are generally:

```text
row_mapping_required
keep_unresolved
```

until trusted source evidence provides row/group linkage.

## Acquisition contradiction guard

A filename token such as `DIA` or `DDA` remains diagnostic-only.

It may trigger publication/repository/structured evidence retrieval, but it cannot directly change `comment[proteomics data acquisition method]`.

A project acquisition method is applied only with corroborating trusted evidence and project-wide scope. Otherwise the conflict remains unresolved.

## One-round bound

The repair stage is intentionally finite:

```text
VALIDATOR_REPAIR_MAX_ROUNDS = 1
VALIDATOR_REPAIR_MAX_TASKS = 4
VALIDATOR_REPAIR_MAX_QUERIES_PER_TASK = 4
```

There is no open-ended validator/LLM loop.

## Provenance outputs

When the stage runs, it writes:

```text
proposals/<PXD>.validator_repair.json
proposals/<PXD>.validator_repair_actions.json   # only when actions execute
```

The dataset audit records:

- validator repair agent version;
- trace path;
- action path;
- rounds completed;
- repair task count.

Applied repairs emit:

```text
validator_repair_project_value_applied
```

Rejected or unresolved decisions remain visible as warnings and the deterministic validator remains authoritative.

## Prompt design

The repair prompt uses the useful principles from the prompt-review discussion without introducing a runtime dependency:

- one explicit scientific task per clustered field;
- hard evidence/provenance constraints;
- structured output;
- bounded actions;
- explicit unresolved/template-gap/row-mapping outcomes;
- no optimization for validator-green output;
- concise context for Qwen non-thinking mode.

The main design-agent v0.4 prompt is intentionally unchanged so mixed8 comparison isolates the effect of the new repair stage.

## Frozen mixed8 gate

Use the same frozen mixed8 cohort and evidence view used for v0.4.

Primary questions:

1. Does the repair stage collapse row storms into a few traceable repair tasks?
2. Does `individual=Embryo` stop propagating into all PXD046467 rows?
3. Are multi-organism conflicts still fail-closed?
4. Are filename DDA/DIA tokens used only to seek corroborating evidence?
5. Are isolation-method repairs applied only when truly project-wide and source-backed?
6. Does PXD041388 remain locally valid?
7. Does relation/cardinality behavior remain unchanged from accepted v0.4?

Do not run the wider Residual73 cohort until this gate is reviewed.
