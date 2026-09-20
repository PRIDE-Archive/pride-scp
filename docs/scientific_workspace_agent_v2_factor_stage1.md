# Scientific Workspace Agent v2 — factor-graph Stage 1

This is a bounded structural proof-of-concept. It does **not** annotate SDRF fields, run validators, or enter a conversational repair loop.

## Motivation

The first v2 StudyGraph Stage-1 experiment eliminated the v1.x retry loop and safely preserved unresolved RAW linkage, but its monolithic branch object collapsed orthogonal scientific axes. In particular, PXD035339 merged the single-cell hydrodynamic-loading and low-input spray-voltage regimes because they shared material/acquisition context. PXD025634 similarly combined explicit Human and Mouse materials into one branch.

The factor-graph representation removes that forced choice.

## Contract

One model call proposes evidence-backed nodes and relations:

- `MaterialNode` — organism/material/cell line/type plus material-specific experimental role.
- `ExperimentalRegimeNode` — experimental role, isolation/loading/sampling method, and input/cell-count regime.
- `AcquisitionNode` — acquisition method/platform.
- `Relation` — only `material_to_regime`, `regime_to_acquisition`, or `material_to_acquisition`.
- optional exact `raw_links`, which remain independent of the scientific graph.

Every node and relation must cite trusted `E####` evidence. Rust assigns canonical accepted IDs (`M001`, `R001`, `A001`), rejects invalid/orphan relations, de-duplicates nodes, and strips exact RAW links unless cited trusted evidence explicitly names the basename.

Absence of exact RAW linkage is not a reason to merge scientific nodes and is not a reason for human review.

## One-shot orchestration

```text
trusted evidence
-> one factor-graph synthesis call
-> Rust acceptance/sanitization
-> accepted factor graph OR human_review
-> STOP
```

There is no Phase B in this implementation.

Enable with:

```text
PRIDE_SCP_SCIENTIFIC_AGENT_MODE=study_factor_graph_v2_stage1
```

Harness version:

```text
pride-scp-scientific-workspace-agent-v2-factor-stage1
```

## Frozen arch4 gate

Phase B is allowed only if the single frozen run satisfies all of the following:

- PXD035339 contains distinct hydrodynamic-single-cell and spray-voltage-low-input regime nodes.
- PXD046467 contains HeLa/reference and Xenopus materials plus a capillary-microsampling/aspiration regime.
- PXD041388 retains an evDISCO/tDISCO regime.
- PXD025634 keeps Human and Mouse as distinct material nodes and retains the microwell/picking regime.
- one synthesis call per accession;
- zero tool actions and zero validator cycles;
- no orphan relations or unsafe exact RAW inference.

If this gate fails, stop before Phase B and reassess the agentic study-structure lane rather than entering another prompt/version loop.
