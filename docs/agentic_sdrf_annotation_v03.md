# Agentic SDRF annotation v0.3

This iteration addresses the mixed8 generalization failure of v0.2 without adding accession-specific scientific rules or relaxing provenance requirements.

## Why v0.3 exists

Agentic v0.2 behaved safely on the two difficult regression accessions, but the mixed8 experiment exposed a systemic over-conservative path: all eight datasets ended as `generated_skeleton_unresolved_mapping`, including previously-solvable controls. The root cause was that an ordinary planner omission was normalized to `scope=unresolved`, and that synthetic claim then vetoed trusted deterministic scaffolds as if the model had made an explicit evidence-backed scientific objection.

The same experiment also showed a mismatch between planner reasoning and retrieval execution. `SEARCH_EXACT_RAW_NAME` accepted semantic strings such as `K562`, while publication/design actions often received long natural-language sentences that were nearly impossible for the executor to match.

v0.3 fixes those contracts generically.

## 1. Tri-state scope arbitration

Ordinary field scope is now arbitrated as:

- `ALLOW`: the model explicitly supports project scope;
- `BLOCK`: the model explicitly asserts group/row/unresolved scope based on evidence/conflict;
- `DEFER`: the planner omitted the field or failed to provide an evidence-backed scope claim.

`DEFER` does **not** invent metadata. It permits an already-existing deterministic scaffold to supply a value only under that scaffold's normal evidence/provenance rules.

Each normalized scope claim records `claim_origin`:

- `model_explicit`
- `default_missing`

A `default_missing` claim cannot veto deterministic evidence.

## 2. Relation architecture is first-class

`relation_mode` is removed from the ordinary field-scope list. Every design assessment must instead return:

```json
{
  "relation_assessment": {
    "mode": "one_cell_per_data_file|multiplexed_cells_per_data_file|mixed|unresolved",
    "scope": "project|branch|unresolved",
    "evidence_refs": ["E0001"],
    "confidence": "high|medium|low",
    "reason": "...",
    "claim_origin": "model_explicit"
  }
}
```

The prompt explicitly distinguishes relation/cardinality from biological identity mapping. Missing animal, condition, cell-line, or treatment linkage does not by itself imply that the one-acquisition-per-sample relationship is unknown.

If the planner fails to provide an evidence-backed relation claim, relation arbitration is `DEFER`; a trusted deterministic relation scaffold may still resolve the architecture. An explicit branch/mixed/unresolved assessment still blocks unsafe project-level row serialization.

## 3. Typed evidence queries

Evidence actions no longer carry free-form search sentences. Queries are structured:

```json
{
  "match": "raw_exact|phrase|terms_all|terms_any|identifier|doi",
  "value": "...",
  "terms": ["..."],
  "document_hint": "..."
}
```

Important contracts:

- `SEARCH_EXACT_RAW_NAME` accepts only `match=raw_exact`.
- The `value` must exactly match an acquisition basename present in the repository file inventory.
- Semantic terms such as a cell line, organism, condition, sample ID, or developmental stage must use publication/repository/design/supplement actions.
- `terms_all` and `terms_any` operate on short atomic terms.
- overlong natural-language `phrase` queries are rejected as `invalid_query` rather than silently producing misleading `no_match` results.
- DOI/document hints prioritize matching registered source files.

Registered repository metadata search also includes the project/files JSON source directly. Publication/supplement/design retrieval remains bounded to trusted registered sources.

## 4. Explicit planner termination

Every assessment now includes:

```text
terminal_status = continue | resolved | partial | evidence_exhausted | abstained
```

The agent may continue only while it has executable evidence actions. If the three-round budget ends with additional requested actions, Rust moves them into `deferred_evidence_actions`, clears the executable action list, and sets `terminal_status=evidence_exhausted`.

This prevents a final assessment from appearing to request work that cannot execute in the current bounded run.

## 5. Orthogonal readiness dimensions

The audit/result output now preserves separate status dimensions:

```text
mapping_status
template_status
metadata_status
evidence_status
agent_terminal_status
```

`completeness_status` is retained for backward compatibility, but it is no longer the only diagnostic state.

This is important for cases that simultaneously have an unresolved mapping and a supported template-vocabulary gap.

## Safety invariants retained

v0.3 does not change the core scientific safety contract:

- filenames are search hints, never biological truth;
- GT accessions/labels are not runtime truth;
- concrete metadata needs provenance;
- candidate groups without source evidence remain non-generative `search_hint` groups;
- explicit model evidence that a field is group/row-specific blocks project-wide broadcast;
- missing row linkage remains a valid unresolved result;
- no accession-specific mappings or exceptions were added;
- Ollama remains `think=false` for structured output compatibility;
- evidence rounds remain capped at three.

## Regression gate

Use the same mixed8 cohort as the v0.3 gate. Expected behavior is intentionally asymmetric:

- PXD015175 / PXD035339 remain unresolved unless trusted row linkage is actually recovered;
- PXD001641 / PXD041388 must not become unresolved merely because the planner omitted an ordinary scope claim or relation field;
- PXD045844 / PXD046467 retain template-gap status independently of mapping status;
- PXD025634 / PXD030607 remain conservative around genuine biological/design conflicts;
- semantic queries must not execute through `SEARCH_EXACT_RAW_NAME`;
- final traces must have an explicit terminal state.
