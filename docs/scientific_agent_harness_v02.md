# PRIDE-SCP Scientific Annotation Agent v0.2

Scientific Agent v0.2 keeps the persistent tool-using agent loop introduced in v0.1, but changes the compiler contract so the model augments a proven deterministic SDRF baseline instead of recreating the full annotation from scratch.

## Motivation

The v0.1 architecture benchmark showed useful model reasoning about study structure, including explicit HeLa/Xenopus branches and fail-closed unresolved RAW linkage. However, its compiler discarded deterministic row structure that the legacy generator already handled correctly. This caused systematic `cell_identifier`, `fraction_identifier`, `technical_replicate`, and isolation regressions, including a regression of a previously valid control accession.

v0.2 therefore treats the scientific agent as a semantic overlay on a deterministic compiler baseline.

## Loop

For each accession:

1. build the trusted evidence inventory;
2. construct the deterministic relation/metadata/row baseline;
3. compile and validate that baseline before the first model turn;
4. if already valid, finish without invoking the scientific agent;
5. otherwise give the agent the persistent study model, evidence/action history, baseline validation diagnostics, and harness feedback;
6. let the model choose bounded evidence searches, revise typed scientific claims/branches, compile, finish, or abstain;
7. reduce model output into canonical state;
8. overlay only source-backed scientific claims on the deterministic baseline;
9. apply branch values only where RAW-to-branch linkage is source-grounded;
10. validate only when the normalized compiler input/draft materially changes.

## Typed scientific state

The model no longer emits arbitrary SDRF field names. It reasons with these concepts:

- `organism`
- `organism_part`
- `disease`
- `cell_type`
- `cell_line`
- `sample_type`
- `isolation_method`
- `individual`
- `sample_preparation`
- `acquisition_mode`
- `labeling`
- `instrument`
- `cleavage_agent`
- `control_role`
- `biological_condition`

Rust owns the mapping from supported concepts to SDRF fields. Concepts such as `cell_line`, `control_role`, and `biological_condition` remain scientific workspace state until a safe deterministic SDRF mapping exists.

Structural fields such as `characteristics[cell identifier]`, `comment[fraction identifier]`, and `comment[technical replicate]` are deterministic compiler responsibilities and are not exposed as model concepts.

## Canonical state reducer

Every model response is normalized before it becomes persistent state:

- invalid evidence refs are removed;
- branch RAW linkage is retained only when cited source evidence explicitly names the exact RAW basename;
- duplicate branches are merged by branch id;
- duplicate claims are merged by `(concept_type, scope, branch_id, value)`;
- competing supported values for the same `(concept_type, scope, branch_id)` become explicit conflicts and are demoted from `supported`;
- open questions and conflicts are deduplicated;
- state size is bounded.

This prevents the append-only assertion explosion seen in v0.1.

## Baseline-preserving compiler

Compilation starts from:

- deterministic acquisition cardinality;
- deterministic repository/file structure;
- deterministic metadata scaffold;
- deterministic row identifiers and one-cell-per-file row structure;
- existing vocabulary/provenance normalization and safety guards.

The agent is then applied as a scientific overlay.

A supported project claim may fill an unresolved baseline value. It does not silently overwrite a conflicting source-backed deterministic value.

A source-backed branch-scoped claim prevents unsafe project-wide broadcasting for that concept. Branch values are serialized only to RAW files with trusted source linkage; otherwise affected rows remain unresolved.

## Tool normalization

The workspace uses concept-targeted actions and converts them to the existing evidence executor. Impossible RAW queries are normalized before execution:

- an exact repository basename with `raw_exact` is routed to `SEARCH_EXACT_RAW_NAME`;
- a non-exact phrase incorrectly emitted as `raw_exact` is reformulated as a normal phrase/terms search rather than consuming a failed exact-RAW tool turn.

## Validator-loop stall detection

The compiler fingerprints the normalized proposal/headers/rows. If a later `compile` request produces exactly the same draft as the previous validated compile:

- no additional validator cycle is counted;
- the harness records explicit feedback;
- the agent must search, materially revise state, finish, or abstain instead of repeatedly validating the same draft.

## Safety retained

v0.2 preserves all prior safety contracts:

- GT is evaluation-only;
- filenames do not establish biological identity;
- DDA/DIA filename strings are contradiction/search hints only;
- model hypotheses are never serialized;
- branch values require source-grounded RAW linkage;
- project broadcast is fail-closed under branch-specific scientific claims;
- controlled-vocabulary canonicalization remains deterministic;
- exact evidence provenance is retained;
- LLM actions and turns remain bounded.

## Architecture benchmark

Use the same four-accession cohort as v0.1:

- `PXD035339` — isolation/canonicalization sentinel;
- `PXD046467` — heterogeneous branch/linkage/acquisition sentinel;
- `PXD041388` — known-good non-regression control;
- `PXD025634` — multiorganism fail-closed sentinel.

Primary v0.2 gates:

1. PXD041388 must not regress from its known-good behavior;
2. structural row fields must no longer create one error per RAW file;
3. PXD035339 manual/hydrodynamic handling should appear as an `isolation_method` claim and canonicalize to `manual picking` only when source evidence supports it;
4. PXD046467 should preserve HeLa/Xenopus branch structure without unsafe file linkage or project-wide organism/acquisition collapse;
5. PXD025634 should remain explicit/fail-closed under unresolved multiorganism linkage;
6. duplicate-claim explosion and identical repeated validation cycles must disappear.
