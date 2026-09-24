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
