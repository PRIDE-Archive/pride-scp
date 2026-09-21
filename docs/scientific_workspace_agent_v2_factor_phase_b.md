# Scientific Workspace Agent v2 — factor-scoped Phase B

## Purpose

Phase B is the first annotation stage that consumes the accepted Rust-owned factor graph from v2 factor Stage 1.

It is intentionally bounded:

```text
accepted FactorGraph
-> one ObservationBundle synthesis call
-> Rust validation/adjudication
-> deterministic SDRF projection
-> one validator pass
-> terminal result
```

There is no conversational validator-repair loop.

## Runtime mode

```text
PRIDE_SCP_SCIENTIFIC_AGENT_MODE=study_factor_graph_v2_phase_b
PRIDE_SCP_FACTOR_GRAPH_STAGE1_ROOT=/path/to/completed/factor-stage1-output
```

The factor graph root must contain:

```text
study_factor_graphs/<PXD>/accepted_graph.json
```

and each graph must have been accepted by:

```text
pride-scp-scientific-workspace-agent-v2-factor-stage1
```

## Model-owned output

The model may return exactly one `FactorObservationBundleProposal` per accession.

Each observation contains only:

```text
target_factor_id
concept_type
observed_value
evidence_refs
confidence
notes
```

The model does not choose:
- branch IDs;
- project/branch/row scope;
- RAW-file mapping;
- canonical SDRF controlled-vocabulary values;
- validator repair actions.

`target_factor_id` must be one of the already accepted Rust-owned IDs such as `M001`, `R002`, or `A001`.

## Scientific scope rules

Material factors can receive source-faithful observations for:
- organism part;
- disease;
- cell type / cell line;
- sample type;
- control role;
- biological condition.

Experimental-regime factors can receive:
- single-cell isolation/loading method;
- sample type;
- labeling;
- cleavage agent;
- control role;
- biological condition.

Acquisition factors can receive:
- proteomics acquisition mode;
- instrument.

The model must preserve source wording. In particular:
- it must not expand an acronym unless cited evidence explicitly provides that expansion;
- injection/loading of a prepared digest is not a single-cell isolation method;
- metabolomics-only direct ESI is not a proteomics acquisition-mode observation;
- it must omit unsupported fields rather than guess.

## Rust-owned acceptance

Rust rejects an observation when:
- the target factor ID is not in the accepted graph;
- the concept is incompatible with that factor type;
- the value is empty/reserved;
- no field-relevant trusted evidence remains;
- the same factor/concept identity receives conflicting values in the same bundle.

Rust derives projection scope.

A value is eligible for project projection only when its target factor is unique on that factor axis. Otherwise it remains factor-scoped and is exposed to the deterministic compiler as a scoped claim.

This means multiple experimental regimes automatically prevent unsafe project-wide broadcasting.

## Deterministic compiler shim

Accepted factor nodes are exposed internally as Rust-owned scoped compiler branches using their canonical factor IDs.

This is not model-created branch structure. It exists only so the existing deterministic compiler can:
- mask unsafe project-level defaults when factor-scoped scientific evidence is heterogeneous;
- apply a factor-scoped value only when trusted exact RAW linkage exists;
- otherwise retain unresolved linkage and emit a curator-review state.

No filename semantics are used to infer linkage.

## Adjudication

Rust remains the sole owner of:
- isolation-method canonicalization;
- template-gap detection;
- acquisition-mode canonicalization;
- supported/unresolved/conflict state;
- row construction;
- final validation.

A source-faithful observation is useful even when Rust cannot publish it into a pinned SDRF vocabulary. `template_gap` and `unresolved` are legitimate terminal outcomes.

## No-loop invariant

Per accession Phase B performs:

```text
1 ObservationBundle model call
0 evidence-tool calls
1 deterministic compile/validator pass
0 repair retries
```

Validator output is terminal review information. It never causes another model turn.

## Frozen arch4 gate

Phase B is considered successful only if safety holds and at least one real downstream gain is demonstrated.

Required safety checks include:
- all observation targets are Rust-owned factor IDs;
- no orphan observations;
- PXD035339 hydrodynamic and spray-voltage observations remain on their distinct regimes;
- PXD046467 receives Xenopus capillary-microsampling evidence without inventing HeLa isolation;
- PXD025634 retains its isolation template gap and does not coerce to `manual picking`;
- one call and one validator pass per accession;
- no conversational retry loop.

Progress requires either:
- at least one accession has fewer validation errors than the frozen v1.3 reference; or
- the four sentinels all terminate with materially improved factor-scoped review state.

If the frozen arch4 Phase-B gate fails, stop before a broader cohort run rather than adding another prompt-only retry architecture.
