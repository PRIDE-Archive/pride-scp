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
