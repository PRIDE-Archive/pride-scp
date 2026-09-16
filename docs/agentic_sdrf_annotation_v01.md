# Agentic SDRF annotation v0.1

This iteration introduces a bounded evidence-seeking design-assessment stage before SDRF metadata proposal generation.

## Goal

The model must reason about experimental design and evidence sufficiency before proposing dataset-level SDRF values. It should identify heterogeneous biology/design, distinguish project-global fields from row/group-local fields, and request additional evidence rather than forcing a dataset-wide value.

## Flow

1. Build the existing provenance-first evidence packet.
2. Ask Ollama for a structured `DatasetDesignAssessment`.
3. Execute at most six requested evidence actions against the trusted evidence inventory.
4. Ask Ollama to reassess the design with those retrieval results.
5. Feed the final assessment into the existing SDRF proposal prompt.
6. Fail closed: fields classified as row/group-local are not broadcast from a dataset-level proposal unless the assessment also marks them project-global.
7. Continue through the existing provenance repair, row construction, and validation path.

## Initial bounded evidence actions

- `SEARCH_PUBLICATION`
- `SEARCH_SUPPLEMENT`
- `SEARCH_STRUCTURED_DESIGN`
- `SEARCH_REPOSITORY_METADATA`
- `SEARCH_EXACT_RAW_NAME`
- `EXPAND_EVIDENCE_CONTEXT`
- `LOOKUP_KG_TERM`
- `COMPARE_CONFLICTING_EVIDENCE`
- `ABSTAIN`

The v0.1 retriever operates only over evidence already loaded into `DatasetEvidence`. It does not perform unrestricted network retrieval. This keeps the first experiment deterministic and allows us to test the planning contract before wiring additional external/source-specific retrieval tools.

## Safety contract

Filename text is non-generative. It can be used as a search query but cannot by itself establish biological identity, cell count, disease, organism, or isolation method.

If the model marks a field as row/group-local and no proven row/group linkage is available, the dataset-level proposal is downgraded to `not available` (or `uncertain` for relation mode) and an audit warning is emitted.

## New artifacts

For LLM-driven accessions the annotation run writes:

- `proposals/{PXD}.design_assessment.json`
- `proposals/{PXD}.evidence_actions.json`
- the existing raw/final Ollama proposal files

The audit records the design-agent version and paths.

## Version

- generator: `pride-scp-sdrf-v0.4.9`
- design agent: `pride-scp-design-agent-v0.1`
- auditor remains `pride-scp-sdrf-auditor-v0.3`
