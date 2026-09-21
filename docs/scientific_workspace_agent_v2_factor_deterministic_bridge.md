# Scientific Workspace Agent v2 — deterministic factor bridge

## Purpose

This mode tests whether the accepted v2 factor graph can be translated into useful SDRF scientific state **without a second LLM call**.

The preceding frozen arch4 experiment showed that factor-graph Stage 1 can recover the important scientific structure, while the bounded Phase-B LLM produced zero accepted observations. The deterministic bridge therefore treats the accepted factor graph as the scientific state and removes the redundant re-interpretation layer.

## Mode

```text
PRIDE_SCP_SCIENTIFIC_AGENT_MODE=study_factor_graph_v2_deterministic_bridge
```

Harness:

```text
pride-scp-scientific-workspace-agent-v2-factor-deterministic-bridge
```

The accepted factor graph root is supplied through:

```text
PRIDE_SCP_FACTOR_GRAPH_STAGE1_ROOT
```

## Bounded flow

```text
accepted factor graph
-> deterministic Rust factor-to-observation bridge
-> Rust adjudication / canonicalization
-> deterministic SDRF projection
-> one validator pass
-> terminal review state
```

There is no Ollama call, no evidence-tool loop, no conversational repair, and no validator-to-model retry.

Expected frozen arch4 totals:

```text
total_agent_turns=0
total_tool_actions=0
total_validator_cycles=4
```

## Deterministic source observations

Rust derives only properties already accepted into the factor graph.

### Material nodes

`MaterialNode.organism` becomes an `organism` observation with the node's accepted evidence refs.

A material experimental role containing explicit reference/control/validation semantics becomes a `control_role` observation. The compiler currently keeps this as scientific state because there is no safe one-to-one SDRF project field.

### Experimental-regime nodes

`ExperimentalRegimeNode.isolation_or_loading_method` becomes an `isolation_method` observation only when the accepted regime itself has cellular handling semantics and a supported sampling/isolation/loading signal.

The generic gate requires both a cellular regime signal (for example single-cell, intact cells, low-number cells, subcellular material) and a handling signal (for example isolation, hydrodynamic loading, spray voltage, microsampling, aspiration, microwell, picking, microfluidics, DISCO terminology).

This deliberately prevents a commercial prepared digest entering CE/LC from being treated as a single-cell isolation method.

An explicit accepted `single cell` / `single cells` input regime also produces a `sample_type = single cell` observation. Subcellular input does not.

### Acquisition nodes

An acquisition node produces an `acquisition_mode` observation only when its accepted acquisition text explicitly contains DIA/data-independent or DDA/data-dependent terminology. Generic direct ESI-MS or LC-MS/MS text is not converted into a proteomics acquisition mode.

## Scope ownership

The model never chooses scope because no model is called.

Rust reuses the factor-safe projection rule:

- unique structurally covering factors may project to project scope;
- heterogeneous factors remain factor-scoped;
- factor-scoped observations keep Rust-owned `M###`, `R###`, or `A###` identities;
- exact row application still requires trusted exact RAW linkage;
- unresolved RAW linkage never causes a factor observation to be broadcast to arbitrary rows.

## Scientific adjudication

The bridge does not write controlled-vocabulary values directly.

Derived claims pass through the existing Rust adjudicator:

```text
canonical
template_gap
unresolved
conflict
supported_concept
```

For example, an accepted source-faithful isolation method outside the pinned single-cell vocabulary remains a template gap rather than being coerced into a nearby allowed term.

Because the bridge uses the factor node's accepted evidence refs, an older derived annotation cannot silently veto a direct-source factor that Stage 1 already accepted. Conflicting evidence remains visible through adjudication/review rather than causing a second model to erase the factor fact.

## Outputs

Per accession:

```text
deterministic_bridge/<PXD>/evidence.json
deterministic_bridge/<PXD>/accepted_factor_graph.json
deterministic_bridge/<PXD>/derived_observations.json
deterministic_bridge/<PXD>/adjudications.json
deterministic_bridge/<PXD>/<PXD>.deterministic_bridge.sdrf.tsv
deterministic_bridge/<PXD>/VALIDATION.md
deterministic_bridge/<PXD>/REVIEW.md
deterministic_bridge/<PXD>/result.json
```

Aggregate:

```text
factor_deterministic_bridge_results.tsv
factor_deterministic_bridge_summary.json
scientific_agent_summary.json
```

## Frozen arch4 gate

The external audit checks the following without introducing accession-specific runtime rules.

PXD035339:
- spray-voltage observation remains on R001;
- hydrodynamic observation remains on R002;
- no cross-regime collapse.

PXD046467:
- capillary/microsampling observation remains on Xenopus regime R002;
- no isolation observation is manufactured for the HeLa commercial-digest regime R001.

PXD041388:
- the accepted tDISCO/evDISCO regime becomes a source observation;
- the observation reaches Rust adjudication.

PXD025634:
- the microwell/picked-cell regime becomes a source observation;
- the compiler-owned isolation template gap remains visible;
- no false canonical `manual picking` substitution is introduced;
- Human and Mouse material factors remain distinct.

Global safety:
- zero model calls;
- zero tool actions;
- exactly one validator pass/accession;
- frozen accepted factor graphs;
- only Rust-owned factor IDs;
- no orphan observations.

Progress requires safety plus either:
- at least one validation-error reduction relative to the frozen v1.3 references; or
- the complete materially improved factor-scoped review state above.

If the deterministic bridge still cannot improve validation or curator state, stop the v2 annotation lane and inspect the compiler/projection boundary directly rather than adding another LLM layer.
