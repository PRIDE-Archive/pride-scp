# Agentic SDRF annotation v0.2

This iteration strengthens the evidence-seeking SDRF planner without adding accession-specific scientific rules.

## Changes

- `DatasetDesignAssessment` now uses canonical `field_scopes` records rather than free-text project-global/row-local field lists.
- Candidate groups explicitly distinguish `supported`, `search_hint`, and `rejected` states plus `source_evidence` versus `filename_hint` provenance.
- Filename-only groups are deterministically downgraded to search hints and cannot provide row linkage or annotation truth.
- Project-level field claims require trusted evidence references; unsupported project scope is downgraded to unresolved.
- The design planner runs for at most three evidence rounds and receives the full action history.
- Repeated failed `(action, query)` pairs are blocked with `duplicate_skipped`; the model must escalate to another evidence source or abstain.
- Evidence actions search the in-memory evidence packet first. If needed, selected actions also search the full trusted manuscript/annotation source files already registered for that dataset and add recovered snippets back into `DatasetEvidence` with provenance IDs.
- Deterministic dataset-level metadata scaffolds and relation hints no longer override a design-agent claim that the corresponding field is group/row/unresolved.
- Confidence is attached to field-scope and candidate-group claims rather than one global dataset confidence.

## Scientific contract

The LLM plans and interprets evidence. Deterministic code enforces provenance, scope, retrieval budgets, serialization, and validation. Filename semantics may formulate search queries but never create biology. Missing source-backed row linkage is a valid terminal result.

## Retrieval limits

The current v0.2 loop is deliberately bounded:

- maximum three retrieval rounds;
- maximum six actions per assessment;
- maximum eight queries per action;
- duplicate failed action/query pairs cannot execute twice;
- source-file fallback only searches trusted source paths already registered in `DatasetEvidence`.

This is still not unrestricted web retrieval. Supplement/design/KG providers can be expanded behind the same typed action contract later.
