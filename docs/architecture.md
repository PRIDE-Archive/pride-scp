# Architecture

PRIDE-SCP is a source-grounded catalogue and annotation system for public
mass-spectrometry single-cell proteomics datasets. The implementation separates
high-volume deterministic repository work from bounded semantic interpretation.

## Design invariants

1. **Discovery is recall-first.** A weak or missing publication, sparse
   repository metadata, or an uncertain model decision must not silently remove
   a repository candidate.
2. **Ground truth is never a production lookup.** Frozen reference/GT datasets
   are only for post-hoc evaluation.
3. **Source identity and biological interpretation are separate.** Repository
   aliases, mirrors, and secondary accessions are normalized before biological
   curation.
4. **LLMs propose facts; deterministic code owns acceptance and serialization.**
   Unsupported values remain unresolved.
5. **Existing curated SDRFs are preserved.** Reconstruction is only used when a
   usable source SDRF does not exist.

## Repository acquisition

### PRIDE primary snapshot

`pride-scp snapshot` materializes the primary PRIDE project universe into a
resumable local cache containing project JSON, file manifests, repository SDRF
responses, and retrieval errors.

### ProteomeCentral / PROXI identity registry

`pride-scp registry-snapshot` supplements repository identity with PXD aliases,
native accessions, and hosting-repository provenance. This layer is identity
plumbing; it does not decide SCP inclusion.

### Native MassIVE catalogue

`pride-scp massive-native-snapshot` enumerates native MassIVE `MSV...` projects
so datasets without PXD aliases can still enter discovery.

## Discovery

`pride-scp discover` scans normalized repository/project evidence using
configuration in `config/discovery_terms.json`. Candidate scoring is
Deterministic and keeps source evidence for later review.

Source scopes keep production and research tasks explicit:

- `pride-primary` — primary PRIDE production catalogue lane;
- `registry-supplement` — PXD aliases not represented by primary PRIDE;
- `native-massive` — native MassIVE records;
- `all` — union for cross-repository research/audit workflows.

`candidate-audit` groups retained evidence into review profiles without deleting
candidates. `export-python` writes stable TSV/JSONL contracts for downstream
Python stages.

## Publication and semantic layer

Python stages resolve publication content and build bounded source packets.
Qwen-based extractors operate on those packets. Curation policy is deterministic
and uses evidence identifiers rather than accepting free-form model claims.

The semantic layer may route a candidate to include/review/exclude according to
its evidence contract, but it must not use frozen GT labels as runtime truth.

## SDRF architecture

The SDRF workflow is intentionally separate from dataset-discovery membership.
A known dataset can have a high-quality SDRF, a weak repository SDRF, a
header-only API response, or no usable SDRF at all.

### Source resolution

`pride-scp sdrf-resolve` selects the highest-trust usable source and stores a
provenance-labelled local copy. Current precedence is:

1. BigBio community-curated SDRF;
2. repository-submitted SDRF;
3. usable local repository/API SDRF.

A file must parse and contain mapped `comment[data file]` rows to be considered
usable. Header-only API responses are recorded but not selected.

### Deterministic audit

`pride-scp sdrf-audit` checks resolved SDRFs without invoking Ollama. It reports:

- structural/schema validity;
- single-cell relation mode;
- missing target metadata;
- repository-file linkage coverage;
- enrichment priority.

Repository linkage is diagnostic and is not allowed to masquerade as SDRF
schema validity.

### Reconstruction and enrichment

`pride-scp sdrf-annotate` follows two paths:

```text
usable resolved SDRF
    -> preserve rows
    -> fill only evidence-supported gaps
    -> deterministic validation

no usable SDRF
    -> repository/file classification
    -> deterministic study-design scaffold
    -> bounded manuscript/repository evidence
    -> Ollama structured proposal
    -> provenance/semantic repair
    -> deterministic SDRF construction
```

One-cell-per-file designs can use deterministic filename-derived cell IDs when
that relation is source-supported. Multiplexed designs remain incomplete until
channel-to-sample mappings are supported; the generator does not invent TMT
channel assignments. Generic archive containers remain explicit mapping
blockers until archive contents are resolved.

## Trust boundaries

### Deterministic authority

Rust owns:

- repository enumeration and caching;
- identity normalization;
- candidate membership/scoring;
- evidence IDs and provenance checks;
- source precedence;
- SDRF row preservation and serialization;
- structural validation;
- resumability.

### Model authority

LLMs may:

- interpret bounded source passages;
- propose normalized biological/method metadata;
- identify likely experimental roles when supported by cited evidence.

LLMs may not:

- add a production accession because it appears in GT;
- serialize final SDRF rows;
- invent sample/channel mappings;
- convert missing evidence into certainty;
- override deterministic provenance checks.

## Evaluation boundary

Evaluation consumes already-generated production outputs and compares them to
frozen reference data. Reference data should remain outside Git and outside the
production runtime data flow.
