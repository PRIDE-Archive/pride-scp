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
