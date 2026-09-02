# Data contracts

## Primary PRIDE snapshot

```text
data/snapshot/
  accessions.txt
  project_pages/page_XXXXXX.json
  projects/PXD....json
  files/PXD....json
  sdrf/PXD....sdrf.tsv
  errors/{project,files,sdrf}/PXD....json
  snapshot_summary.json
```

Raw PRIDE API responses are cached and are not rewritten by discovery.

## ProteomeCentral registry supplement

Iteration 2 adds a separate supplemental registry cache under the same snapshot
root:

```text
data/snapshot/registry/
  pages/page_XXXXXX.json
  projects/PXD....json
  accessions.txt
  registry_accessions.tsv
  registry_summary.json
```

`registry-snapshot` enumerates the ProteomeCentral PROXI `/datasets` endpoint
and records every dataset that exposes a PXD identifier in the alias crosswalk.
A normalized project JSON is materialized only when that PXD is absent from the
primary PRIDE snapshot, avoiding a second 40k-record metadata copy. Supplemental
normalized records retain the original PROXI dataset object plus:

- `accession` / `projectAccession`: normalized PXD alias;
- `registrySource`: `ProteomeCentral PROXI`;
- `registryHostingRepository`: inferred repository label when available;
- `registryNativeAccessions`: native repository accessions such as `MSV...`;
- normalized `title` and `description` fields used by discovery.

`registry_accessions.tsv` is the explicit alias/provenance crosswalk:

```text
pxd_accession
hosting_repository
native_accessions
present_in_primary_snapshot
registry_json_path
dataset_title
```

The registry is **supplemental, not a second scoring copy of PRIDE**. Discovery
uses `registry/projects/PXD....json` only when the same PXD is absent from the
primary `projects/` directory. This preserves accepted PRIDE scores/tiers while
recovering cross-repository PXD aliases.

A successful registry enumeration reconciles `registry/projects/` against the
current supplemental alias set and removes stale generated JSON records. If an
explicit `--max-pages` cap is reached before the API signals the end of the
listing, `registry-snapshot` fails rather than emitting a silently incomplete
registry index. `registry_summary.json` reports both normalized records written
and stale normalized records removed.

## Discovery

`project_discovery_audit.tsv` contains every scanned project-like record,
including score=0 records, so missed positives can be traced.
`candidates.tsv` is the compact positive-signal bridge. `candidates.jsonl`
additionally contains all evidence hits and excerpts.

`discovery_summary.json` now distinguishes:

- `projects_scanned`: primary + registry supplements;
- `primary_projects_scanned`: materialized PRIDE projects;
- `registry_supplements_scanned`: PXD aliases absent from the primary PRIDE
  snapshot and therefore admitted from ProteomeCentral;
- positive/candidate/tier counts.

Key candidate fields:

- `accession`
- `score`
- `tier`: `strong`, `possible`, `weak`
- `positive_lanes`
- `positive_labels`
- `negative_context_labels`
- source paths
- JSONL `hits[]` with lane, label, term, weight, excerpt

For registry-only aliases, `project_json_path` points into
`snapshot/registry/projects/`, where the native accession and hosting-repository
provenance are preserved.

The default `--min-score 1` is deliberately recall-oriented.

## Recall benchmark

A benchmark CSV needs `pxd_accession` (or `accession`). If it contains
`contains_true_single_cell_ms` or `expected_positive`, only truthy rows are
counted as positives. Frozen GT-style tables may instead use
`reference_decision=include`, and `recall-audit` can optionally filter by
`hosting_repository`.

GT data is evaluation-only. It is not an input to `snapshot`,
`registry-snapshot`, or `discover`.

## Python bridge

`export-python` writes:

```text
data/python_bridge/candidate_accessions.txt
data/python_bridge/candidate_manifest.tsv
data/python_bridge/semantic_candidates.jsonl
```

The current Python Stage 01 accepts the accession list, and Stage 04 should be
run with `--all-valid-pdfs` so Stage 03 does not become a hard gate again.
