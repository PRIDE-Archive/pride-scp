# Agentic SDRF annotation v0.7 — task-aware repair evidence and canonicalization

## Status

This iteration is intentionally narrow. It keeps the accepted v0.4 relation/cardinality logic, the v0.5 deterministic individual sanitizer, and the v0.6 PLAN → RETRIEVE → DECIDE validator-repair lifecycle.

v0.7 addresses four issues isolated by the frozen mixed8 v0.6 run:

1. repair actions were not fairly allocated across independent validation tasks;
2. matched repair evidence was not retained by field/task for deterministic normalization;
3. an LLM `template_gap` classification could bypass an already-supported deterministic canonical value;
4. repair `terminal_status` reflected model wording instead of post-repair validation outcomes.

This is the final narrow harness iteration before a broader redesign should be considered if the frozen mixed8 does not materially improve.

## Versions

- generator: `pride-scp-sdrf-v0.5.5`
- design agent: `pride-scp-design-agent-v0.4`
- validator repair agent: `pride-scp-validator-repair-v0.3`
- design rounds: 3
- validator repair retrieval rounds: 1
- repair tasks per accession: 4
- repair queries per task: 4
- auditor: `pride-scp-sdrf-auditor-v0.3`
- Ollama structured calls: `think=false`, `temperature=0`

## 1. Task-aware evidence actions

Each validator-repair evidence action now targets exactly one repair field. The repair schema enforces one `target_field` per action.

Before execution, Rust builds a fair action set:

- safely terminal tasks do not consume retrieval budget;
- every nonterminal task receives one action before another task can consume additional query specificity;
- a model-supplied action is used only once;
- if the model omits an action for a nonterminal task, Rust synthesizes the existing conservative field-specific fallback;
- each task may carry up to four typed queries;
- at most four repair tasks are active, so the absolute repair-search bound remains finite.

The intent is to prevent a multi-problem accession from spending its complete repair budget on one field while starving the others.

## 2. Evidence action results retain target fields

`EvidenceActionResult` now records the repair `target_fields` that caused each search.

This lets the repair stage associate retrieved E#### evidence with the task that requested it instead of treating all newly retrieved evidence as one undifferentiated pool.

Historical design-agent action results deserialize with an empty target list, preserving backward compatibility.

## 3. Field-specific task evidence pools

Before final repair application, each final decision receives a deterministic task evidence pool composed of:

- evidence refs from the PLAN decision for that field;
- evidence refs from the DECIDE decision;
- matched E#### refs from repair actions explicitly targeted at that field.

Only valid, field-relevant evidence refs are retained.

Correctness no longer depends on Qwen repeating every useful retrieved E#### in its final structured decision.

## 4. Deterministic canonicalization precedes model template-gap wording

For project-scoped isolation/acquisition repair decisions, Rust now attempts deterministic canonicalization after building the complete task evidence pool and before applying template-gap outcomes.

For isolation this reuses `infer_isolation_method_scaffold()`.

For acquisition this reuses the source-corroborated DDA/DIA repair normalizer; filename/file-manifest evidence remains excluded as annotation truth.

If:

- the task is project scoped;
- the design scope guard does not block project application;
- all retained refs are field relevant;
- no already-confirmed template gap exists; and
- the evidence pool uniquely canonicalizes to a supported SDRF value;

then the canonical value supersedes model wording such as `template_gap` or `no_change`.

Example target behavior:

```text
model intent: capillary/manual hydrodynamic loading
trusted evidence: single cells loaded manually using hydrodynamic pressure
Rust canonical value: manual picking
```

The model still does not control the final SDRF vocabulary.

An already source-confirmed unsupported isolation method, such as capillary microsampling under the pinned template, remains a template gap and is not overridden.

## 5. Deterministic repair terminal status

The final `ValidatorRepairTrace.final_assessment.terminal_status` is recomputed after repair application and deterministic revalidation.

Semantics:

- `resolved`: no original repair-task validation class remains;
- `partial`: some work was safely applied, or a source-backed row-mapping/template blocker was established, while validation blockers remain;
- `evidence_exhausted`: one repair retrieval round ran but no task was safely closed and the original validation class remains;
- `abstained`: the repair stage explicitly abstained.

An unconfirmed model-declared template gap does not count as a closed blocker.

This makes trace status reflect the actual post-repair SDRF rather than the model's self-description.

## 6. Scientific safety retained

Unchanged policies:

- filenames never establish biological identity;
- filename DDA/DIA tokens are contradiction detectors only;
- multi-organism projects cannot collapse to one organism without trusted row/group linkage;
- `row_mapping_required` carries no project value;
- non-individual semantic concepts are sanitized from `characteristics[individual]`;
- relation/cardinality logic from v0.4 is untouched;
- only one validator-repair retrieval round is allowed;
- template gaps require deterministic source-backed corroboration.

## 7. Frozen mixed8 acceptance bar

This iteration is not considered successful merely because trace structure changes.

Material success requires:

1. PXD041388 remains locally valid;
2. PXD046467 keeps the 83 invalid-individual errors eliminated;
3. nonterminal repair tasks receive task-specific retrieval opportunities rather than a flattened global budget;
4. PXD035339 clears its 15 isolation errors if the combined task evidence safely canonicalizes to project-wide `manual picking`;
5. PXD001641 remains unresolved rather than acquiring an unsupported template gap when no isolation method is evidenced;
6. PXD046467 acquisition remains row-mapped/unresolved unless trusted non-filename evidence supports a safe technical mapping;
7. organism conflicts remain fail closed without trusted row linkage;
8. final repair terminal status agrees with the post-repair validation result.

If the frozen mixed8 does not show material scientific improvement under these criteria, stop adding narrow per-field/per-accession repair patches and redesign the repair harness/interface before continuing toward Residual73.
