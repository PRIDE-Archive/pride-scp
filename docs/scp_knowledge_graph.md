# PRIDE-SCP global knowledge/evidence graph

## Purpose

PRIDE-SCP uses one provenance-aware global graph for single-cell proteomics (SCP).  Each repository
accession is a graph entity and can be queried as an accession-scoped subgraph/view.  The production
runtime does **not** maintain accession-specific scientific rules and does not create a disconnected
knowledge store per accession.

The graph separates:

1. **Sources** — PRIDE/repository metadata, publications, repository files, analysis artifacts,
   community resources, and model-readable evidence.
2. **Nodes** — accessions, publications, experimental branch hypotheses, RAW files, reporter
   channels, technologies, methods, community-resource pages, protocols and analysis repositories.
3. **Claims** — provenance-bearing assertions emitted by source adapters, deterministic parsers, or
   the constrained small-LLM semantic extractor.
4. **Edges** — canonical accepted facts promoted from claims by explicit resolution rules.

This separation is deliberate.  A parser or model seeing `TMTpro18` creates a claim; it does not make
that statement graph truth by itself.

## SQLite schema

The initial implementation uses SQLite tables:

- `source`
- `node`
- `claim`
- `edge`
- `edge_claim`
- `graph_run`

The representation is intentionally portable and can later be exported to RDF, GraphML or Neo4j.

## Source authority

Repository metadata and repository RAW inventories are primary evidence and can be promoted directly
to accepted graph edges.  Accepted publication associations are also imported as canonical links.

Derived branch calls from the v0.5.3 generalized evidence graph are retained only as `hypothesis`
claims.  This is important because v0.5.3 deliberately generalized inference but still over-segmented
some experimental branches.

Repeated spectrum/PSM rows are collapsed before graph import so the same RAW appearing thousands of
times in a result database does not create thousands of independent mapping claims.

## Slavov Lab SCP source

`https://scp.slavovlab.net/` is ingested as a community knowledge/auditing source.  Its pages may add
claims about:

- SCP technologies and sample-preparation approaches;
- publications;
- PXD and MassIVE accessions;
- analysis-code repositories;
- protocols and training resources;
- data/metadata resources such as Zenodo, Figshare and Google Drive.

These claims are valuable for discovery and corroboration but are **not** automatically promoted to
accession-specific SDRF facts.  In particular, a community resource cannot by itself assign a RAW
file to a cell or reporter channel.

If the main SCP website cannot be crawled, the ingestion command can inventory SCP-relevant public
repositories from the `SlavovLab` GitHub organization as a distinct fallback provenance source.

## Small-LLM integration

The graph exposes a JSONL claim-import contract so the existing small local LLM can become a semantic
claim extractor rather than an unconstrained SDRF generator.  Each model claim must contain a source
URI plus evidence text or an evidence locator.

Example shape:

```json
{
  "scope_accession": "PXDxxxxxx",
  "subject": {"type": "ExperimentalBranch", "key": "source-defined-branch-key"},
  "predicate": "USES_CHEMISTRY",
  "object": {"type": "Technology", "key": "TMTpro18", "label": "TMTpro18"},
  "source_uri": "doi:10.xxxx/example",
  "source_type": "publication",
  "evidence_locator": "Methods / Multiplexing",
  "evidence_text": "...source-grounded sentence...",
  "extractor": "small_llm:qwen2.5:3b",
  "confidence": 0.96,
  "status": "asserted"
}
```

No claim without provenance is accepted by the importer.

## SDRF projection

The future SDRF generator should consume only **source-closed accepted facts** from an accession view.
Branch hypotheses and unresolved/conflicting claims remain review evidence and must not be serialized
as invented SDRF values.

## Generalization rule

Changing only an accession identifier must not change scientific inference.  Accessions are allowed
as runtime data and query scopes, not as scientific configuration.
