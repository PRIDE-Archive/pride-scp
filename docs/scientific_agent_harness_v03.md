# PRIDE-SCP Scientific Annotation Agent v0.3

Scientific Agent v0.3 keeps the baseline-preserving, typed, persistent agent architecture from v0.2 and changes one ownership boundary: model claim status is advisory for scientific concepts that Rust can independently adjudicate.

## Motivation

The v0.2 arch4 benchmark reduced validation errors from 920 to 212, removed the structural per-RAW error storm, eliminated duplicate claim accumulation, preserved fail-closed branch linkage, and prevented repeated validation of unchanged drafts. The remaining failures were almost entirely isolation-method semantics.

A representative v0.2 state contained a high-confidence `isolation_method` hypothesis with direct evidence for hydrodynamic/on-capillary loading, but the compiler refused deterministic normalization because the model had called the claim `hypothesis` rather than `supported`.

That gives the model too much authority over publication. The model should propose the scientific interpretation and cite evidence. Rust should decide whether those refs are sufficient to publish a canonical SDRF value, expose a template gap, record a conflict, or stay unresolved.

## Claim adjudication

Every typed claim can now be reduced to one compiler-owned outcome:

- `canonical` — trusted field-relevant evidence deterministically maps to one supported SDRF value;
- `template_gap` — evidence supports a scientifically faithful method that is absent from the pinned template vocabulary;
- `supported_concept` — a non-canonicalized concept has a model-supported value with relevant evidence;
- `unresolved` — evidence is insufficient or scope is unsafe;
- `conflict` — evidence supports incompatible scientific alternatives.

The model's `supported` / `hypothesis` label is retained in the workspace for reasoning, but it is not the final publication gate for `isolation_method` or `acquisition_mode`.

## Isolation-method adjudication

For a claim's cited isolation evidence, Rust applies this order:

1. verify field-relevant trusted refs;
2. preserve project/branch scope safety;
3. detect a known source-backed template gap;
4. otherwise run the deterministic isolation canonicalizer;
5. otherwise keep the claim unresolved.

Template-gap detection precedes canonical substitution. A real method such as capillary microsampling or microwell-chip single-cell transfer must not be coerced into a nearby allowed term merely to satisfy validation.

Hydrodynamic loading of individual single cells can still canonicalize to `manual picking` when the existing deterministic evidence rule is satisfied.

Project-scoped isolation claims are not promoted in projects exposing multiple explicit organisms; those must remain branch/row scoped unless evidence explicitly establishes one invariant method across all biological branches.

## Acquisition adjudication

A `hypothesis` acquisition claim may be promoted only when cited evidence deterministically supports exactly one acquisition mode. Evidence supporting both DDA and DIA remains a conflict and is not broadcast project-wide.

## Dynamic semantic bootstrap

The deterministic metadata scaffold is constructed before the agent loop, but new evidence may be discovered on later turns. v0.3 therefore re-runs the deterministic isolation/acquisition semantic canonicalizers on the current trusted evidence inventory during every compile.

This lets newly retrieved source evidence become compiler-owned semantics without requiring the model to output the exact controlled term.

The bootstrap remains fail-closed:

- source-backed template gaps stay unresolved;
- multi-organism projects do not receive a project-wide isolation value from bootstrap alone;
- ambiguous DDA/DIA evidence does not receive a global acquisition value.

## Adjudication trace

The workspace now writes `adjudications.json` containing, for every typed claim:

- concept type;
- scope and branch;
- model status;
- proposed value;
- cited evidence refs;
- deterministic adjudication outcome.

The same records are included in the scientific-agent audit JSON. Compiler adjudication feedback is also returned to the next model turn so the agent can search for stronger field-specific evidence rather than repeating an unpublishable claim.

## Persistent agent behavior retained

v0.3 retains the v0.2 architecture:

- deterministic baseline compile before turn 1;
- typed scientific concepts rather than arbitrary SDRF fields;
- canonical/deduplicated workspace state;
- deterministic row scaffold;
- branch heterogeneity masking unsafe project values;
- trusted RAW-to-branch linkage only;
- bounded evidence tools;
- exact-RAW action normalization;
- compile fingerprinting and unchanged-draft stall protection.

## Scientific distinction: isolation vs downstream processing

The prompt now explicitly distinguishes the operation that isolates or selects a single cell from downstream lysis, digestion, droplet handling, or injection of an already isolated lysate. A droplet containing lysate is not, by itself, evidence for a single-cell isolation method.

This is a generic scientific distinction and prevents downstream sample handling from being promoted into the isolation-method field.

## Arch4 decision gates

Use the same frozen architecture cohort:

- `PXD035339` — hydrodynamic/manual isolation canonicalization sentinel;
- `PXD046467` — heterogeneous HeLa/Xenopus branch/linkage sentinel;
- `PXD041388` — known-good/non-regression semantic bootstrap control;
- `PXD025634` — multiorganism + potential template-gap sentinel.

Primary v0.3 gates:

1. no regression of v0.2 structural fields, deduplication, or stall detection;
2. PXD035339's evidence-backed isolation hypothesis should canonicalize to `manual picking` only if the existing deterministic rule is satisfied;
3. PXD025634/PXD046467 unsupported isolation methods should become explicit template-gap or unresolved adjudications rather than false allowed values;
4. PXD046467 branch heterogeneity and unresolved RAW linkage must remain fail-closed;
5. PXD041388 should recover a valid isolation value only if current trusted evidence independently supports it; previous valid output is not runtime truth;
6. if direct evidence cannot resolve PXD041388, the agent should search for the actual isolation operation instead of repeatedly compiling a downstream droplet/lysis interpretation.
