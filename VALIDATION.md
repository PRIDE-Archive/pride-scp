# Validation status for the v0.1.0 bootstrap archive

Checks completed in the artifact-building environment:

- all Cargo TOML files parse successfully;
- `config/discovery_terms.json` parses successfully;
- Python recall helper compiles with `py_compile`;
- all shell scripts pass `bash -n`;
- synthetic fixture/config logic was independently sanity-checked.

The artifact-building environment did **not** contain a Rust toolchain, so a
real `cargo check/test` could not be executed before packaging. This is why the
first local validation on the target workstation is intentionally:

```bash
cargo generate-lockfile
cargo fmt --all
cargo check --workspace
cargo test --workspace
cargo build --release
./scripts/smoke_fixture.sh
```

Do not start the full PRIDE crawl until these commands pass.


## v0.1.1 live-snapshot regression

The v0.1.1 hotfix adds unit coverage for PRIDE project-page extraction and
last-page detection. A live pilot should also confirm that `--limit 100` fetches
only enough paginated catalogue pages to obtain 100 accessions, then snapshots
those projects with no fatal catalogue-body timeout.

## v0.1.2 progress/logging validation

Run locally on the Rust-enabled development machine:

```bash
cargo fmt --all
cargo check --workspace --locked
cargo test --workspace --locked
cargo build --release --locked
./scripts/smoke_fixture.sh
```

Then verify both interactive and quiet modes:

```bash
target/release/pride-scp discover \
  --snapshot data/snapshot_known_positive \
  --config config/discovery_terms.json \
  --output data/discovery_known_positive_v012 \
  --min-score 1 \
  --expected-positive-count 20

target/release/pride-scp --no-progress discover \
  --snapshot data/snapshot_known_positive \
  --config config/discovery_terms.json \
  --output data/discovery_known_positive_v012_quiet \
  --min-score 1 \
  --expected-positive-count 20
```

Both runs should preserve 20/20 known-positive candidate recovery.


## v0.1.3 full-catalogue enumeration regression

After applying the patch, run:

```bash
cargo fmt --all
cargo check --workspace --locked
cargo test --workspace --locked
cargo build --release --locked
./scripts/smoke_fixture.sh
```

The unit suite now includes synthetic regressions for:

- a normal paginated response;
- a short final page;
- a monolithic response containing more than twice the requested page size.

For the interrupted live snapshot, rerun the same output directory:

```bash
target/release/pride-scp snapshot \
  --output data/snapshot \
  --concurrency 8 \
  --request-concurrency 16 \
  --timeout 120 \
  --retries 4 \
  --project-page-size 100
```

Expected enumeration behavior is that cached page 0 yields roughly the full
40k-accession universe and terminates with:

```text
enumeration_termination = monolithic_catalogue_response
```

or, if the server response shape changes, no more than the configured number
of duplicate-only stagnant pages are traversed.

To force a current catalogue snapshot without invalidating downstream caches:

```bash
target/release/pride-scp snapshot \
  --output data/snapshot \
  --refresh-catalogue \
  --concurrency 8 \
  --request-concurrency 16 \
  --timeout 120 \
  --retries 4
```

## v0.1.4 semantic bridge and snapshot compaction

Validate locally:

```bash
cargo fmt --all
cargo check --workspace --locked
cargo test --workspace --locked
cargo build --release --locked
./scripts/smoke_fixture.sh
python -m py_compile python/recall/*.py
bash -n scripts/*.sh
```

The Rust unit suite now additionally checks that broad-context labels are kept
separate from specific SCP/method labels and that snapshot compaction removes
redundant catalogue pages while preserving materialized project records.

On the completed full discovery, run:

```bash
target/release/pride-scp candidate-audit \
  --candidates data/discovery/candidates.jsonl \
  --config config/discovery_terms.json \
  --output data/candidate_audit

target/release/pride-scp export-python \
  --candidates data/discovery/candidates.tsv \
  --candidates-jsonl data/discovery/candidates.jsonl \
  --config config/discovery_terms.json \
  --output data/python_bridge \
  --min-tier weak
```

The bridge must retain all 321 candidates.

Before reclaiming storage, preview:

```bash
target/release/pride-scp compact-snapshot \
  --snapshot data/snapshot \
  --dry-run
```

The dry-run must report `validation_ok=true`, 40,364 indexed accessions, at
least 40,364 materialized project records, and a very large number of redundant
project-page bytes eligible for deletion. Then run the same command without
`--dry-run` and confirm that `projects/`, `files/`, and `sdrf/` remain present.

## v0.1.5 publication resolver

Offline regression:

```bash
python python/recall/pdf_resolver_regression_smoke.py
```

Expected:

```text
All v0.1.5 PDF resolver regression tests passed.
PMCID fallback no longer requires hasPDF=Y.
Manual/reused PDFs override unresolved cache paths.
Old no_open_access_pdf cache schema is invalidated.
Unresolved rows now retain structured diagnostics.
```

After applying the patch, rerun `scripts/run_python_publication_enrichment.sh`.
The old Stage-02 `.resolution_cache` is safe to retain: schema-v1 unresolved
records are ignored automatically.  Compare the resulting
`publication_backed` count with the previous broken 0/321 partition before
starting Ollama annotation.

Optional live network smoke against three known SCP publication DOIs:

```bash
python python/recall/pdf_resolver_live_smoke.py \
  --keep-output work/python/pdf_resolver_live_smoke
```

Expected final line: `Live known-OA smoke: 3/3 usable PDFs`. If a publisher or
PMC endpoint changes, inspect the retained `pdf_error` and
`pdf_resolution_trace` fields before running the full 201-publication batch.


## v0.1.6 NCBI PMC ID Converter fallback

Offline regression coverage now verifies normalization of a PMC ID Converter
record, cache-v2 invalidation, PMCID URL generation, manual/reuse override, and
structured unresolved traces. The authoritative live test remains:

```bash
python python/recall/pdf_resolver_live_smoke.py \
  --keep-output work/python/pdf_resolver_live_smoke_v016
```

Expected result for the three known-PMC SCP publications is `3/3 usable PDFs`.


## v0.1.7 publication-content validation

Offline regression:

```bash
python python/recall/publication_content_regression_smoke.py
```

Expected:

```text
All v0.1.7 publication-content regression tests passed.
JATS XML normalizes into deterministic text blocks.
Stage 04 accepts normalized full text as publication evidence.
Manual PDF manifests can link by PMCID/PXD.
Missing-PDF and priority manual-manuscript queues are distinct.
```

Live author-manuscript smoke (PXD037527 / PMC10529037):

```bash
python python/recall/publication_content_live_smoke.py \
  --keep-output work/python/publication_content_live_smoke_v017
```

Expected: `publication_content_status=available` using either
`fulltext_xml` or `fulltext_html`.

## v0.1.8 semantic-unification validation

Offline regression:

```bash
python python/recall/semantic_unification_regression_smoke.py
```

Expected:

```text
All v0.1.8 semantic-unification regression tests passed.
Stage-04 and repository labels are normalized before routing.
Repository triage overcalls are detected from its own structured evidence.
Review candidates cannot enter a final Stage-05 bridge without explicit decisions.
Repository-only candidates can be synthesized into Stage-05-compatible annotations after adjudication.
```

On the completed 321-candidate semantic run, execute:

```bash
scripts/unify_semantic_results.sh
```

For the v0.1.7 results used to design this release, the expected audit shape is:

```text
321 candidates, zero loss
219 publication-backed
102 repository-only

Stage 04: 42 yes / 177 no
Repository triage: 96 possible_true_scp / 6 uncertain
Repository overcalls vs its own structured evidence: 41

include_candidate: 56
review_high:       77
review_medium:     66
review_low:        20
likely_non_scp:   102
secondary review: 163
```

These counts are routing diagnostics, not an expected final catalogue size.

The final Stage-05 bridge must refuse to build until every review row has an
explicit `include` or `exclude` decision. A structural-only smoke is possible
with `--allow-provisional`, which encodes unresolved reviews as excluded and
therefore must never be used as the final catalogue.

Independent QC logic regression:

```bash
python python/recall/semantic_qc_regression_smoke.py
```

Expected:

```text
All v0.1.8 semantic-QC logic regression tests passed.
Selective jury triggers protect both recall and precision conflicts.
Critic/jury disagreement remains unresolved rather than being forced.
Evidence packets preserve source excerpts and prior semantic provenance.
```

The live Ollama run is intentionally separate because it is CPU-heavy:

```bash
scripts/run_semantic_qc.sh
```

Do not run the critic and jury simultaneously. The helper processes all critic
calls first, unloads that model, then loads the jury only for selected rows.



## v0.1.9 evidence-grounded QC

```bash
python -m py_compile python/recall/*.py
bash -n scripts/*.sh
python python/recall/evidence_grounded_qc_regression_smoke.py
```

The regression verifies direct source-passage rehydration, factual-axis decision normalization, automatic v0.1.8 cache invalidation, and selective-jury behavior. A live Ollama rerun is authoritative for the final uncertainty count.


Before the full live rerun, use `scripts/run_semantic_qc_smoke.sh`. The four-accession smoke intentionally contrasts individual egg SCP, a many-cell FACS population, a cell-line population study, and a modern genuine SCP dataset. Do not launch the full 219-candidate QC if that qualitative smoke is wrong.


## v0.1.10 positive-chain + structured-output validation

Offline regression:

```bash
python -m py_compile python/recall/*.py
bash -n scripts/*.sh
python python/recall/evidence_grounded_qc_regression_smoke.py
```

Expected:

```text
All v0.1.10 positive-chain QC regression tests passed.
Complete individual-cell-to-MS chains normalize to include despite cautious raw labels.
Separate multi-cell controls and identity-preserving multiplexing do not negate SCP samples.
Population/destructive-pooling evidence remains a deterministic exclusion.
Older QC caches are invalidated automatically.
Malformed/truncated structured output receives a corrective JSON retry.
```

Then rerun the four-accession live smoke:

```bash
scripts/run_semantic_qc_smoke.sh \
  2>&1 | tee work/python/semantic_qc_smoke_v0110.log
```

Required qualitative result before the full 219-candidate QC:

```text
PXD000902  include
PXD028991  exclude
PXD000441  exclude
PXD049412  include
errors      0
```

Do not run the full semantic QC if either positive control remains uncertain or either negative control becomes included.


## v0.1.11 target-MS-sample-unit QC

Offline regression:

```bash
python python/recall/evidence_grounded_qc_regression_smoke.py
```

Expected:

```text
All v0.1.11 MS-sample-unit QC regression tests passed.
Many-cell target MS samples override optimistic single-cell labels.
Complete one-cell target-MS chains override inconsistent benchmark-only flags.
Evidence-aware critic/jury arbitration preserves hard sample-unit exclusions.
Separate multi-cell controls remain compatible with genuine single-cell target samples.
Malformed structured output still receives a corrective JSON retry.
```

Authoritative live four-accession smoke:

```bash
scripts/run_semantic_qc_smoke.sh \
  2>&1 | tee work/python/semantic_qc_smoke_v0111.log
```

Acceptance criterion before the 219-candidate full run:

```text
PXD000902  include
PXD028991  exclude
PXD000441  exclude
PXD049412  include
errors      0
uncertain   0
```
