# Scientific Workspace Agent v2 — Stage 1 StudyGraph prototype

This document defines the bounded first stage of the v2 redesign. It is intentionally not an SDRF repair loop.

## Why Stage 1 exists

The v1.x line demonstrated that the model can extract useful scientific observations but that downstream field repair becomes unsafe and repetitive when study structure has not first been accepted. v2 therefore makes the study graph a prerequisite artifact.

Stage 1 answers one question only:

> Can the model synthesize a small source-grounded conceptual study graph that Rust can safely accept before any SDRF field annotation occurs?

If the answer is no on the frozen sentinel cohort, Phase B is not implemented.

## Execution mode

The existing `sdrf-agent` CLI remains available. Stage 1 is enabled only when:

```text
PRIDE_SCP_SCIENTIFIC_AGENT_MODE=study_graph_v2_stage1
```

The reported harness version is:

```text
pride-scp-scientific-workspace-agent-v2-stage1
```

The default/unset mode remains the v1.3 workspace agent. This keeps the architectural proof-of-concept isolated.

## Bounded execution contract

For each accession Stage 1 performs exactly one model call:

```text
build trusted evidence inventory
-> rank study-structure evidence
-> one StudyGraphProposal call
-> Rust acceptance/sanitization
-> accepted StudyGraph OR human_review
-> STOP accession
```

There are no:

- `read_evidence` turns;
- `search_evidence` turns;
- scientific-observation edits;
- SDRF compile/repair loops;
- validator-driven retries.

`turns=1`, `tool_actions=0`, and `validator_cycles=0` are therefore expected Stage-1 properties.

## StudyGraphProposal

A proposal contains:

```text
decision = propose_graph | human_review
branches[]
open_questions[]
reason
```

Each proposed branch contains:

```text
label
biological_material
experimental_role
isolation_context
acquisition_context
evidence_refs[]
linked_raw_files[]
linkage_status
notes
```

Every branch needs at least one valid trusted `E####` evidence reference.

## Conceptual branch rule

Conceptual branch creation does not require exact RAW linkage.

A valid source-grounded branch may be emitted as:

```text
linked_raw_files=[]
linkage_status="unresolved"
```

Unresolved RAW linkage alone is not a reason to return `human_review`.

## Exact RAW-linkage safety

Rust independently verifies every proposed RAW basename.

A link is retained only when:

1. the basename exists in the PRIDE RAW inventory; and
2. one of the branch's cited trusted evidence items explicitly names that basename outside repository-manifest-only evidence.

Otherwise Rust removes the RAW link and downgrades linkage to `unresolved` rather than inferring biological identity from filename words.

## Rust-owned branch IDs

The model does not create the IDs consumed by future phases. Rust normalizes, de-duplicates and sorts accepted conceptual branches, then assigns deterministic IDs:

```text
B001
B002
...
```

A future Phase B must use only these accepted Rust-owned IDs.

## Human-review stop

If the model returns `human_review`, or Rust cannot accept any source-grounded branch, the accession terminates at Stage 1.

Downstream annotation must not proceed.

## Frozen Stage-1 gate

The first experiment uses the existing arch4 sentinels and Qwen3.6-27B.

Required gate:

- PXD035339: accepted graph captures distinct single-cell/hydrodynamic and spray-voltage/low-input regimes, or a defensible human-review stop;
- PXD046467: accepted graph captures distinct HeLa/reference and Xenopus material, or a defensible human-review stop;
- no unsupported exact RAW links survive Rust acceptance;
- one model call per accession;
- no downstream annotation/validator loop occurs.

If the obvious structural sentinels cannot yield meaningful accepted graphs, stop before implementing Phase B.
