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
