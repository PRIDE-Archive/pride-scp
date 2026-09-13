# GitHub Container Registry publication

PRIDE-SCP publishes one canonical `linux/amd64` Docker runtime image and derives the HPC SIF from
that exact Docker image. The SIF workflow never rebuilds PRIDE-SCP from source independently.

## Packages

For repository `OWNER/pride-scp`:

- Docker/OCI runtime: `ghcr.io/OWNER/pride-scp`
- Singularity/Apptainer SIF as an OCI artifact: `oras://ghcr.io/OWNER/pride-scp-sif`

Every successful non-PR Docker build publishes an immutable tag:

```text
sha-<full 40-character Git commit SHA>
```

Pushes to `main` also update `main`. Version tags matching `v*` publish that exact tag. Version tags
matching `vMAJOR.MINOR.PATCH` or `vMAJOR.MINOR.PATCH.REVISION` also update `latest`.

The SIF workflow is triggered only after the Docker workflow succeeds, resolves the exact source SHA,
pulls `sha-<SHA>`, converts it through `containers/build_singularity.sh`, checks the generated
checksum, runs PRIDE-SCP smoke tests, uploads the SIF as an Actions artifact, and publishes the same
immutable SHA to the `-sif` GHCR package.

## Pull by immutable SHA

Docker:

```bash
SHA=<full-git-sha>
docker pull ghcr.io/OWNER/pride-scp:sha-${SHA}
```

Apptainer:

```bash
SHA=<full-git-sha>
apptainer pull pride-scp.sif \
  oras://ghcr.io/OWNER/pride-scp-sif:sha-${SHA}
```

On a cluster that provides SingularityCE instead of Apptainer, pull/stage the SIF once on a host with
registry access and run the resulting `.sif` normally with SingularityCE.

## Workflow behavior

`.github/workflows/container-docker.yml` builds on relevant pull requests but never pushes packages
from a pull request. Pushes to `main`, `v*` tags, and manual dispatches may publish using the built-in
`GITHUB_TOKEN` with `packages: write`.

`.github/workflows/container-sif.yml` listens for a successful `Container - Docker` workflow and skips
Docker runs originating from pull requests. It can also be dispatched manually for a specific full
40-character source SHA and optional alias.

If the GHCR package name existed before these workflows were introduced, ensure the package is linked
to the repository and grants GitHub Actions write access. No personal access token should be required
for the normal same-repository workflow.

## Provenance

The Docker workflow records the Git SHA, Cargo lock SHA-256, source fingerprint, workspace version,
platform, runtime target, and immutable GHCR tag as a GitHub Actions artifact.

The SIF workflow records:

- exact Git SHA;
- exact Docker tag used for conversion;
- resolved Docker repository digest;
- SIF SHA-256;
- the metadata file produced by `containers/build_singularity.sh`.

For reproducible cluster runs, prefer the immutable SHA tag or Docker digest over `main` / `latest`.
