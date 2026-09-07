# Model roles

PRIDE-SCP uses small local language models as bounded source interpreters. They
are not discovery engines and they are not ground truth.

## Active model use

### Publication/repository extraction

Default model: `qwen2.5:3b` through local Ollama.

The model receives a bounded evidence packet and returns structured factual
proposals. Every accepted non-reserved assertion must be supported by valid
source evidence or deterministic derivation.

### SDRF reconstruction/enrichment

The SDRF lane uses the same principle:

1. Rust classifies repository files and infers a study-design scaffold.
2. Rust extracts field-relevant evidence windows.
3. Ollama proposes only unresolved metadata.
4. Rust repairs provenance/semantic errors.
5. Rust constructs or enriches SDRF rows.

The model never writes the final TSV.

## Deterministic safeguards

Examples include:

- repository/file transport values cannot become biological metadata;
- analysis software cannot become a mass-spectrometer or isolation method;
- acquisition-method vocabulary is separated from separation/sample-prep
  language;
- existing resolved SDRF values are locked unless intentionally missing;
- unsupported multiplex channel mappings remain unresolved;
- blank or `uncertain` study-design hints are non-assertive;
- exact-value provenance recovery is limited to field-relevant evidence.

## Historical model lanes

Earlier Phi/Gemma/MiniCheck experiments remain useful for historical error
analysis but are not part of the maintained production decision path unless
explicitly reintroduced and revalidated. Their run artifacts belong in local
archives or development documentation rather than the repository front page.

## Evaluation rule

Frozen GT/reference annotations may score model outputs after a run. They may
not be placed in prompts, used as accession allow-lists, or supplied as SDRF
field evidence.
