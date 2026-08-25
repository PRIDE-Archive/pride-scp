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
