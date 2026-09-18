# Agentic SDRF annotation v0.6: repair control and deterministic canonicalization

Version line:

- generator: `pride-scp-sdrf-v0.5.4`
- design agent: `pride-scp-design-agent-v0.4` (unchanged)
- validator repair agent: `pride-scp-validator-repair-v0.2`
- design evidence rounds: 3
- validator repair evidence rounds: 1
- validator repair tasks/accession: 4
- validator repair queries/task: 4

## Purpose

v0.5 proved that validation-error clustering and deterministic semantic sanitation work, but the validator-repair LLM often returned terminal decisions on its first call. As a result, the intended evidence-retrieval round was never executed. It also exposed an ownership problem: Qwen could identify the correct experimental concept while emitting a non-template phrase such as `capillary loading`, which Rust then rejected.

v0.6 fixes those two problems without changing the accepted v0.4 relation/cardinality architecture.

## Two-phase repair control

Validator repair now has explicit phases.

### `PHASE=PLAN`

The model receives clustered validation tasks, current proposal/evidence, the final design assessment, and the complete prior design-agent evidence-action history.

It may make a provisional decision immediately only when the current evidence is already safely terminal. Rust preflights every task. A task is considered safely terminal only when one of the following holds:

- a project isolation/acquisition repair is source-backed, scope-safe, and deterministically canonicalizable;
- a template gap is deterministically corroborated;
- an organism/individual problem is source-backed and genuinely requires row mapping.

All other tasks require the single bounded repair-retrieval round. If the model supplies no usable evidence action, Rust synthesizes one conservative field-specific retrieval action rather than silently terminating the repair stage.

### `PHASE=DECIDE`

After the one retrieval round, the model receives the new evidence/action results and returns final decisions. Further actions are cleared deterministically. Unsupported fields must remain unresolved, become a corroborated template gap, or explicitly require row mapping.

## Prior search history

The repair prompt now includes the design-agent action history. The repair executor seeds its duplicate-action set from that history, preventing the repair stage from blindly repeating already-exhausted searches.

## Deterministic repair-value canonicalization

Qwen no longer owns the final SDRF controlled-vocabulary value for the two automatically repairable fields.

### Isolation method

The repair decision identifies evidence and intent. Rust runs the cited evidence through the same deterministic isolation normalizer used by the main metadata scaffold.

For example, source text describing hydrodynamic loading/injection of individual single cells can canonicalize to:

```text
manual picking
```

A raw model phrase such as `capillary loading` is never written directly into the SDRF.

### Acquisition method

Rust canonicalizes cited source evidence to one of the supported acquisition representations only when the cited evidence supports one unambiguous mode. Filename-only evidence is excluded from acquisition repair canonicalization. Conflicting DDA/DIA evidence remains unresolved/row-mapped rather than being globally overwritten.

## Decision normalization

Non-project decisions are normalized before use:

```text
row_mapping_required -> no concrete project value; scope row/role
keep_unresolved       -> no concrete value; scope unresolved
template_gap          -> no annotation value
no_change             -> no annotation value
```

This prevents contradictory outputs such as `row_mapping_required + scope=project + concrete organism`.

## Template-gap propagation

A validator-repair `template_gap` now updates deterministic readiness only when Rust independently corroborates the unsupported method from trusted evidence. An LLM declaration alone cannot create a template gap.

The same deterministic unsupported-isolation detector is shared by initial scaffold construction and validator repair.

## Safety invariants retained

- GT remains evaluation-only.
- Filename semantics are search/contradiction hints, not biological or acquisition truth.
- Multi-organism collapse cannot be repaired by choosing one global organism.
- Row/group biological mappings still require source-grounded linkage.
- Relation/cardinality behavior from v0.4 is unchanged.
- The deterministic `individual` semantic sanitizer from v0.5 is unchanged.
- Maximum validator repair retrieval rounds remains exactly one.
- Maximum repair tasks remains four.
- Maximum queries per repair task remains four.
- Ollama remains `think=false`, `temperature=0`.

## Expected mixed8 signals

The frozen mixed8 rerun should verify:

1. PXD041388 remains locally valid.
2. PXD046467 keeps the v0.5 removal of the 83 invalid `individual` errors.
3. At least the repair tasks that are not already safely terminal record `rounds_completed=1` and non-empty repair action results.
4. PXD035339 can use its hydrodynamic-loading evidence through deterministic canonicalization rather than serializing `capillary loading`.
5. PXD001641 cannot create a template gap unless unsupported-method evidence is deterministically corroborated.
6. Multi-organism cases remain fail-closed unless actual row/group linkage is recovered.
7. DDA/DIA conflicts are not repaired from filenames alone.
