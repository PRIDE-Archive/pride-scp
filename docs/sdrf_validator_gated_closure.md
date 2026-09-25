# Validator-gated SDRF closure harness

The closure harness wraps the existing PRIDE-SCP bridge-v2 repair controller with a hard
`sdrf-pipelines` validation boundary after every candidate mutation.

## Invariant

No candidate advances across a mutating boundary until the existing project validation path
has been rerun on the exact candidate bytes.

`scripts/sdrf_validator_gate.py` dynamically imports `scripts/sdrf_bigbio_readiness.py`
and directly reuses its `read_sdrf`, `derive_templates`, `validate_parse_sdrf`, and
`command_dict` helpers. This intentionally keeps the validator semantics identical to the
frozen readiness policy, including the project's narrowly audited validator compatibility
handling.

Final `submission_ready` requires both frozen PRIDE-SCP/BigBio readiness and a green
`sdrf-pipelines` validator gate on the exact final bytes.

The supporting resolvers are fail-closed:

- Cellosaurus accepts only one unique exact recommended-name/synonym match.
- The independent reviewer is non-editing and exact-SHA-bound.
- Ambiguous row mappings, ontology mappings, or missing evidence remain explicit terminal
  states instead of being guessed.

Each accession receives append-only validator receipts under `validator_receipts/` and the
cohort receives `validation_ledger.tsv`, `closure_ledger.tsv`, and `closure_summary.json`.

## Closure dispatch v2 (cohort-level, validator-gated)

The first closure controller revision intentionally stopped at bridge-v2 terminal queues.  That
was useful as an integration smoke, but it could not improve the cohort beyond bridge-v2.  The v2
controller treats those queues as bounded closure actions instead:

```text
bridge-v2
  |
  +-- needs_independent_review
  |     -> exact projected-byte validator
  |     -> non-editing exact-hash reviewer
  |     -> TSV approval manifest: accession / sha256 / status
  |     -> frozen v0.5.14.8 readiness with --review-approved-manifest
  |     -> exact final submission-byte validator
  |     -> submission_ready only if every gate is green
  |
  +-- ontology_mapping_required
  |     -> exact Cellosaurus resolver
  |     -> canonical characteristics[cellosaurus accession]
  |     -> validator
  |     -> next frozen bridge/readiness pass
  |
  +-- row_mapping_required
  |     -> bounded evidence-escalation harness
  |     -> generalized/public supplementary structured evidence
  |     -> explicit structured row/channel mapping resolver
  |     -> validator
  |     -> next frozen bridge/readiness pass
  |
  +-- unsupported_or_exhausted
        -> one bounded evidence-escalation attempt
        -> evidence-produced candidate, if any
        -> validator
        -> one final bridge/readiness pass
```

### Structured row/channel resolver safety

`scripts/sdrf_structured_mapping_resolver.py` is non-generative. It reads only explicit structured
records from JSON, JSONL, TSV, CSV and XLSX sources. A mapping record must include an explicit
RAW/data-file identity plus one or more recognized SDRF fields.

It never:

- derives identity from RAW filename semantics;
- uses row order as evidence;
- overwrites a different concrete SDRF value;
- resolves a conflicting structured mapping;
- combines records explicitly tagged for another PXD accession.

One-to-many multiplex row expansion is permitted only when the structured source explicitly
contains multiple distinct reporter labels for one RAW and every channel record also contains a
concrete biological/sample identity field. The expanded candidate is immediately validator-gated.

### Evidence escalation budget

Each accession receives at most one evidence-escalation dispatch from the closure controller.
The evidence harness may perform its existing bounded publication/full-text recovery,
supplementary/external-analysis graph construction, semantic annotation and Rust `sdrf-annotate`
pass. The closure controller then allows at most the configured bridge-pass budget. No repeated
no-new-evidence loop is introduced.

### Validator provenance

The controller queries `sdrf-pipelines` from the actual frozen readiness Singularity runtime and
passes that version and immutable SIF SHA256 into every validator receipt. Host Python package
metadata is not used as production validator provenance.

### Cellosaurus canonical column

The exact Cellosaurus resolver writes the project-standard BigBio field:

```text
characteristics[cellosaurus accession]
```

A historical `characteristics[cell line accession]` header is canonicalized to that field without
changing its row values.

### Acceptance criterion

A closure run is considered successful only when it improves closure classes, not merely state
labels. In particular, a review-ready case must either reach exact-hash-approved
`submission_ready` or produce a real reviewer hold/insufficient-evidence receipt; row/ontology
queues must show resolver artifacts and validator receipts; evidence-exhausted cases must show the
bounded evidence attempt before terminal exhaustion.

### Explicit composite-value narrowing

For row-scoped biological fields, an existing project-level composite such as
`HeLa; K562` or `HeLa | Jurkat` is not treated as an immutable row value when a trusted
structured source explicitly maps that RAW/channel to exactly one member of the existing set.
The structured resolver may narrow only to an exact member already present in the composite,
never to a new value. This rule is limited to biological identity fields and still requires
an exact source-backed row mapping; it does not authorize filename or row-order inference.
