# GT-independent PRIDE production catalogue

This lane is the production use of PRIDE_SCP. Frozen GT196/GT179 is **not** a runtime input.
The GT reference exists only to evaluate whether the pipeline independently recovers known
positives and to diagnose genuine architectural shortcomings.

## Source scope

Production PRIDE discovery uses:

```text
--source-scope pride-primary
```

This scans only `data/snapshot/projects/*.json`, i.e. primary PRIDE project records.
ProteomeCentral registry-only supplements and native MassIVE records are excluded from the
PRIDE production candidate cohort. They may be used by separate repository lanes, but they
must not inflate the PRIDE-only catalogue.

For the frozen 31-Aug-2026 snapshot the accepted reproducibility expectation is:

```text
primary PRIDE projects scanned = 40,364
primary PRIDE candidates       = 334
```

These are snapshot reproducibility guards, not GT-derived target counts. They can be disabled
when intentionally running against a newer repository snapshot.

## Curation

Every production candidate is sent to frozen `v19-shadow-2.7.1` using repository,
file/SDRF, and available publication/Stage04 evidence. No candidate is selected because it
appears in GT and no GT label is supplied to the model or deterministic decision layer.

`review` is a valid production state. The runner never converts review to negative and never
optimizes toward a target catalogue size.

## Outputs

`data/pride_scp_production_catalogue_v1/catalogue/` contains:

- `pride_scp_catalogue_automated_includes.csv` — automated production catalogue;
- `pride_scp_review_queue.csv` — unresolved candidates requiring bounded source review;
- `pride_scp_excluded_candidates.csv` — evidence-supported exclusions;
- `pride_scp_all_curated_candidates.csv` — complete auditable candidate table;
- `pride_scp_production_catalogue_summary.json` — provenance and decision counts.

The top-level `pride_scp_production_run_summary.json` records the source scope and hard
GT-independence invariants.

## GT use after production

A GT comparison may be run **only after** the production outputs are frozen. It is an external
evaluation and may be used to identify missed known positives or architectural bugs. It must
not be used to silently add accessions to the production catalogue, change source identity,
or force include decisions.
