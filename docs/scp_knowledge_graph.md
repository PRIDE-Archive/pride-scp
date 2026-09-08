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

## v0.5.5 canonicalization and resolution

The global graph deliberately stores more claims than accepted facts.  `scripts/scp_kg_resolve.py`
adds the first accession-agnostic canonicalization/resolution layer between source ingestion and any
future SDRF projection.

### Canonical identity

Reusable identities are normalized before evidence is compared.  The resolver currently
canonicalizes common technology/reporter aliases (for example `TMT pro 18-plex`, `TMTpro18` and
`TMTpro 18 plex`), reporter-channel notation, acquisition-method labels and modality spelling.  The
original source nodes are retained and `node_canonicalization` records the mapping to the canonical
node; source provenance is never discarded.

Unknown terms are preserved rather than force-mapped to a known vocabulary.  Canonicalization is a
normalization operation, not a semantic guess.

### Source lineage and independent corroboration

Raw claim count is not treated as evidence strength.  The resolver computes both a source lineage and
an evidence family for every claim.  Multiple repeated observations from one lineage are collapsed to
their strongest contribution, and multiple lineages from the same evidence family are capped when
computing corroboration support.

Consequently, ten `scp.slavovlab.net` pages repeating the same method/publication do not count as ten
independent sources.  Community resources remain valuable for discovery and corroboration but cannot
manufacture accession-specific SDRF truth by repetition.

### Risk-aware resolution

Canonical claim groups are classified by risk:

- `source_relation`: statements that a page/repository mentions or links a resource;
- `bibliographic`: repository title/date/RAW inventory and publication identity;
- `semantic`: modality, chemistry, acquisition and other method-level facts;
- `mapping`: branch/file/sample/channel joins used directly by SDRF rows;
- `relationship`: cross-accession predecessor/redeposit/related-study hypotheses.

The acceptance gate becomes stricter as risk increases.  High-risk RAW/sample/channel mapping claims
require primary or structured source closure.  Community evidence, publication prose or runtime
hypotheses cannot independently close such a mapping.  Cross-accession relationship inferences remain
review-gated even when strongly corroborated unless an explicit source closes the relationship.

Claim-resolution states are explicit:

- `accepted_existing` — already present as a canonical primary edge;
- `accepted_resolved` — promoted by resolver policy;
- `corroborated_not_accepted` — independently supported but below the risk-specific generation gate;
- `source_asserted` — present in a source but insufficiently corroborated;
- `hypothesis_only` — supported only by derived/runtime hypotheses;
- `conflicted` — mutually exclusive alternatives remain;
- `rejected_by_stronger_evidence` — a functional fact conflicts with a stronger already accepted
  primary fact.

### Branch-hypothesis resolution

The resolver also canonicalizes the noisy branch hypotheses produced by the generalized v0.5.3
extractor.  It does not promote those branches to graph truth.  Instead it:

1. normalizes modality/chemistry/acquisition/reporter-role features;
2. marks internally impossible combinations such as label-free modality plus TMT reporter chemistry;
3. marks incompatible specific reporter chemistries as contradictory;
4. conservatively clusters only compatible hypotheses with sufficiently similar evidence;
5. emits `canonical_branch_candidate`, `unresolved_hypothesis`, or `contradictory_hypothesis` review
   records.

A canonical branch candidate still requires source-grounded publication/repository/structured claims
before becoming an accepted experimental branch.

### Resolver tables and exports

The SQLite graph now additionally contains:

- `node_canonicalization`;
- `resolution_group`;
- `resolution_claim`;
- `branch_resolution`;
- `branch_resolution_member`.

The standard graph export includes corresponding TSVs.  The v0.5.5 runner also writes explicit
review queues under `resolution/`, including conflicts, corroborated-but-not-accepted facts,
hypothesis-only facts and branch-resolution diagnostics.

Run:

```bash
./scripts/run_scp_global_knowledge_graph_v055.sh
```

The stage remains non-generative.  Its purpose is to create a reliable canonical knowledge layer
before the small LLM is allowed to contribute manuscript-derived semantic claims at scale.

## v0.5.6 constrained small-LLM semantic claim extraction

`scripts/scp_kg_extract_semantic_claims.py` connects the existing small local Ollama model to the
knowledge graph as a **semantic source reader**, not an SDRF generator.

The extractor reads two source classes for each runtime-selected accession:

1. PRIDE project metadata from the local repository snapshot;
2. locally resolved accession-associated manuscript/full-text rows from the publication manifest.

Manuscript text is split into provenance-labelled, section-aware chunks. Explicit bibliography
sections are excluded from semantic extraction so methods cited from unrelated papers are not
mistaken for methods used by the current study. The full-manuscript mode processes all bounded
chunks; a relevant mode is also available for larger catalogue-scale runs and reports the selected
text-coverage fraction.

### Model contract

The model sees evidence IDs such as `P0001` or `M0001` and may emit only a fixed semantic ontology:

- experimental modality;
- reporter chemistry / reusable technology;
- MS acquisition mode;
- analytical/carrier/blank/reference reporter-role descriptions;
- isolation and sample-preparation method;
- organism, organism part, cell type, disease and sample type;
- instrument and cleavage agent;
- cells per well / multiplex size / relation-mode descriptions.

The model cannot choose graph node types or arbitrary predicates. RAW/file/sample/cell/run mapping
predicates are absent from the Ollama JSON schema and are rejected again by
`import_claim_jsonl()` if an externally supplied small-LLM claim attempts to use them.

Every accepted model claim must cite supplied passage IDs. The Python converter reconstructs exact
source URI, content hash, source lineage, graph node type and evidence locator. A reporter-channel
role is rejected unless the channel token itself occurs in the cited passage. Claims whose object is
not lexically/alias-grounded in the source receive a deterministic confidence cap.

### Source independence

Model interpretation is not treated as identical to primary source truth. The resolver introduces
lower-authority evidence families:

- `peer_reviewed_model_extraction`;
- `repository_model_extraction`.

One small-LLM interpretation cannot self-promote a semantic fact. Matching claims from independent
source lineages, such as manuscript text plus PRIDE project metadata, may satisfy the existing
independent-corroboration gate. Mapping facts remain governed by the stricter structured-source gate.

The model-derived source metadata stores a lineage URI so repeated chunks from the same manuscript
count as one evidence lineage rather than independent votes.

### Caching and reproducibility

Each Ollama packet is cached by prompt version, model name and exact provenance-labelled packet
content. Re-runs therefore reuse valid model responses unless `--force`/`LLM_FORCE=1` is requested.
The run writes the original packets, validated graph-claim JSONL, rejected-claim JSONL and per-packet
results for auditability.

Run the integrated non-generative stage with:

```bash
./scripts/run_scp_global_knowledge_graph_v056.sh
```

This stage imports the constrained model claims and reruns the v0.5.5 canonical resolver. It still
does not project or generate SDRFs.

Source-local `ExperimentalContext` nodes deliberately include source identity in their keys. Two
manuscripts/metadata records that happen to use similar context labels are therefore not merged by
the LLM stage itself. Dataset-wide accession claims can corroborate across independent source
lineages immediately; experimental-context claims require a later generic context/branch resolution
step to establish cross-source identity. This prevents model wording from silently merging distinct
sub-studies.

`publication_mode=full` means source-order manuscript coverage up to the configured
`max_publication_chunks`; setting that limit to `0` removes the cap. `publication_mode=relevant`
selects the highest-scoring broad SCP/method passages up to the same cap. The extraction summary
always reports the selected character-coverage fraction.

## v0.5.7 optimized semantic extraction

The v0.5.6 architecture is retained, but the CPU small-LLM stage no longer defaults to brute-force
full-manuscript packet processing.  v0.5.7 adds a high-recall retrieval layer that selects diverse
SCP/SDRF-relevant source chunks across single-cell scope, multiplexing, acquisition, preparation and
biology, then sends only bounded source excerpts to the model.  Retrieval is auditable: the summary
records source characters, selected chunk coverage, model-character fraction and anchor-feature
coverage for every accession.

The model budget is also bounded more aggressively by default (`num_ctx=8192`, `max_claims=20`,
`max_packet_chars=8000`).  These settings are performance controls only; the fixed semantic ontology,
provenance requirements and ban on model-generated RAW/sample/channel joins are unchanged.

Completed older semantic packets are reusable across prompt versions when accession, source URI and
source-content SHA256 still match.  This allows an interrupted v0.5.6 run to seed v0.5.7 without
throwing away expensive CPU inference already completed.  New packet/claim/rejection checkpoints are
written incrementally after every packet.

The integrated runner is:

```bash
./scripts/run_scp_global_knowledge_graph_v057.sh
```

Useful runtime controls include:

```text
RESUME=1                    reuse an existing v0.5.7 base graph and skip KG/community rebuild
BASE_GRAPH_FROM=/path/db    seed a new v0.5.7 run from an already-built compatible base graph
LLM_ACCESSIONS_FILE=...     run expensive LLM extraction only on a selected accession subset
PUBLICATION_MODE=semantic   high-recall semantic retrieval (default)
MAX_PUBLICATION_CHUNKS=6    maximum selected source chunks/publication
SEMANTIC_EXCERPT_CHARS=2800 maximum model-facing characters/selected chunk
LLM_MAX_PACKET_CHARS=8000   bounded evidence per Ollama request
LLM_MAX_CLAIMS=20           bounded structured output claims/request
LLM_MAX_PACKETS=0           0 means no packet cap; positive values support bounded performance pilots
```

`packet_plan.tsv` is produced before inference begins and the console prints per-packet start/end,
cache state, evidence size and wall time.  This makes catalogue-scale runtime measurable before a
large semantic-enrichment launch.
