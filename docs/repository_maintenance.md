# Repository maintenance

The project spent substantial time in research/benchmark iteration. Production
cleanup should preserve that work without making the Git worktree a historical
artifact store.

## What belongs in Git

Track maintained source and stable documentation:

- Rust crates and Cargo manifests;
- active Python stages/curation code;
- reusable shell wrappers;
- small deterministic test fixtures;
- CI configuration;
- production architecture/data-contract documentation.

## What should stay local

Do not track:

- `data/` and `work/` outputs;
- frozen GT/reference bundles used only for evaluation;
- manuscript PDFs;
- handoff documents;
- one-off run reports;
- tar/zip source bundles and split archive parts;
- patch/overlay manifests;
- historical benchmark wrappers that are no longer production entry points.

The root `.gitignore` encodes these boundaries.

## Safe cleanup helper

`scripts/repository_cleanup.sh` only moves untracked files. Before moving a
path, it asks Git whether the path is tracked; tracked files are always skipped.
Nothing is deleted.

Dry run:

```bash
./scripts/repository_cleanup.sh
```

Move obvious top-level historical/build-delivery artifacts to a timestamped
sibling archive:

```bash
./scripts/repository_cleanup.sh --apply
```

Include untracked GT/evaluation experiment helpers and historical docs:

```bash
./scripts/repository_cleanup.sh --apply --include-experiments
```

The default archive root is next to the repository, for example:

```text
/home/user/Documents/github/pride-scp.local-archive/
```

Override it with:

```bash
./scripts/repository_cleanup.sh --apply \
  --archive-root /path/to/archive
```

## Large duplicate reference bundles

Moving a file out of the Git worktree does not free disk space. If a full
`*.tar.gz` and split `*.partNN` copies both exist, verify the archive before
deleting duplicates manually.

For a split archive:

```bash
cat pride_scp_gpt_reference_bundle.tar.gz.part* | sha256sum
sha256sum pride_scp_gpt_reference_bundle.tar.gz
```

Only remove a duplicate copy after you have independently verified that the
remaining archive is complete and backed up where needed.

## Build artifacts

`target/` is ignored. `cargo clean` can reclaim it, but do not clean while a
long-running binary from that build is still active unless you intentionally
want to remove the on-disk build products.

## Production transition checklist

1. Finish any currently-running data job.
2. Run the cleanup helper in dry-run mode.
3. Archive top-level historical artifacts.
4. Decide whether to archive experiment-only scripts with
   `--include-experiments`.
5. Review `git status --short --ignored`.
6. Stage only maintained source/docs/tests.
7. Run the full validation matrix.
8. Commit the production-cleanup change separately from scientific behavior
   changes.
