# Submission-ready SDRF release and upstream PR workflow

This workflow freezes exact-hash PRIDE-SCP SDRFs and contributes them to
`bigbio/sdrf-annotated-datasets` as one pull request per accession.

The contribution behavior is aligned with `sdrf-annotated-datasets` at commit
`1b96296a1dbcf4cd4034440e5f83415a71eeadc5`:

- canonical path: `datasets/{ACCESSION}/{ACCESSION}.sdrf.tsv`;
- branch from `dev`, or `main` when `main` is the integration branch;
- validate locally with current `sdrf-pipelines` using
  `parse_sdrf validate-sdrf --sdrf_file ... --use_ols_cache_only`;
- keep PRs accession-scoped;
- cite public evidence in the PR description;
- disclose agent assistance and whether human review occurred.

## 1. Freeze a production release

Example on Codon after the review-manifest promotion run:

```bash
cd /nfs/research/juan/DIA/singj/pride-scp

OUT="$PWD/results/pride-scp-v0514_3-readiness-full105-review20"
INPUT="$PWD/data/sdrf_readiness_full105_v0514_1_stage65"
REVIEW_MANIFEST="$PWD/reviews/PRIDE_SCP_REVIEW23_APPROVED_MANIFEST_2026-09-11.tsv"
RELEASE="$PWD/releases/PRIDE_SCP_SUBMISSION_RELEASE20_2026-09-11"
ARCHIVE="$PWD/releases/PRIDE_SCP_SUBMISSION_RELEASE20_2026-09-11.tar.gz"

python scripts/freeze_sdrf_submission_release.py \
  --submission-root "$OUT/submission/datasets" \
  --approved-manifest "$REVIEW_MANIFEST" \
  --readiness-summary "$OUT/sdrf_readiness_summary.json" \
  --review-adjudication "$PWD/reviews/PRIDE_SCP_REVIEW23_FINAL_ADJUDICATION_2026-09-11.tsv" \
  --review-summary "$PWD/reviews/PRIDE_SCP_REVIEW23_FINAL_ADJUDICATION_2026-09-11.md" \
  --spec-contract "$PWD/resources/sdrf_spec_contract_v1_1_0.json" \
  --project-snapshot-root "$INPUT/snapshot/projects" \
  --output-dir "$RELEASE" \
  --archive "$ARCHIVE" \
  --expected-count 20
```

The script refuses to package any file whose bytes differ from the approved
SHA-256.

## 2. Prepare the local upstream-contribution environment

Local fork:

```text
/home/sing/Documents/github/sdrf-annotated-datasets
```

Install the validator exactly as requested by upstream:

```bash
python -m pip install --upgrade \
  "git+https://github.com/bigbio/sdrf-pipelines.git@main"
```

Check GitHub CLI authentication:

```bash
gh auth status
```

Ensure the fork has an upstream remote (the automation can add it when
`--add-upstream-remote` is supplied):

```bash
cd /home/sing/Documents/github/sdrf-annotated-datasets
git remote -v
```

## 3. Dry-run all accessions

The default mode does **not** push or create PRs. It:

- verifies the frozen release checksum manifest;
- fetches the fork and upstream;
- chooses `upstream/dev` when present, otherwise `upstream/main`;
- creates an isolated worktree/branch per accession;
- copies exactly one SDRF to `datasets/{ACCESSION}/`;
- verifies no unrelated file changed;
- runs the upstream-required validator command;
- creates a one-accession commit locally;
- writes the proposed PR body and result table;
- destroys the temporary worktree afterwards.

```bash
python /home/sing/Documents/github/pride-scp/scripts/open_sdrf_annotated_dataset_prs.py \
  --release-dir /path/to/PRIDE_SCP_SUBMISSION_RELEASE20_2026-09-11 \
  --repo /home/sing/Documents/github/sdrf-annotated-datasets \
  --add-upstream-remote \
  --results /home/sing/Documents/github/pride-scp/work/release20_pr_dry_run.tsv \
  --continue-on-error
```

Review the result table. `dry_run_validated` is ready to submit.
`no_change_upstream` means upstream already contains identical bytes and no PR
is required.

## 4. Submit one test PR

```bash
python /home/sing/Documents/github/pride-scp/scripts/open_sdrf_annotated_dataset_prs.py \
  --release-dir /path/to/PRIDE_SCP_SUBMISSION_RELEASE20_2026-09-11 \
  --repo /home/sing/Documents/github/sdrf-annotated-datasets \
  --add-upstream-remote \
  --accession PXD016921 \
  --submit \
  --results /home/sing/Documents/github/pride-scp/work/release20_pr_test.tsv
```

Inspect the resulting upstream PR and CI before submitting the remainder.

## 5. Submit the remaining cohort

```bash
python /home/sing/Documents/github/pride-scp/scripts/open_sdrf_annotated_dataset_prs.py \
  --release-dir /path/to/PRIDE_SCP_SUBMISSION_RELEASE20_2026-09-11 \
  --repo /home/sing/Documents/github/sdrf-annotated-datasets \
  --add-upstream-remote \
  --submit \
  --continue-on-error \
  --results /home/sing/Documents/github/pride-scp/work/release20_pr_submission.tsv
```

The automation is idempotent:

- identical upstream file -> `no_change_upstream`;
- already-open generated PR -> `existing_open_pr`;
- validation failure -> no push and no PR;
- hash mismatch -> no branch/PR;
- any changed path outside the accession folder -> abort that accession.

## Publication compatibility gate

PRIDE-SCP v0.5.14.4 preserves the source candidate but writes validator-compatible publication
artifacts before exact hashes are frozen. In particular, already-explicit DIA methods are serialized
as `Data-independent acquisition` while sdrf-pipelines 0.1.5/0.1.6 retain issue #345.

Before any branch is pushed, `open_sdrf_annotated_dataset_prs.py` now runs both the repository's base
validation command and separate `ms-proteomics` plus every declared leaf-template validation using
`--skip-ontology`. This mirrors the upstream review gate closely enough to catch DIA/template failures
locally rather than after PR creation.
