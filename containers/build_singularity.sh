#!/usr/bin/env bash
set -euo pipefail

[[ $# -eq 2 ]] || { echo "usage: $0 <docker-image> <output.sif>" >&2; exit 2; }
IMAGE="$1"
SIF="$2"
mkdir -p "$(dirname "$SIF")"
SIF="$(cd "$(dirname "$SIF")" && pwd)/$(basename "$SIF")"

if command -v apptainer >/dev/null 2>&1; then
  CONTAINER_BIN=apptainer
elif command -v singularity >/dev/null 2>&1; then
  CONTAINER_BIN=singularity
else
  echo "ERROR: neither apptainer nor singularity is available" >&2
  exit 2
fi
command -v docker >/dev/null 2>&1 || { echo "ERROR: docker is required to convert the local canonical image" >&2; exit 2; }
docker image inspect "$IMAGE" >/dev/null

TMP_ARCHIVE=""
cleanup() { [[ -n "$TMP_ARCHIVE" ]] && rm -f "$TMP_ARCHIVE"; }
trap cleanup EXIT INT TERM

echo "Building SIF with $CONTAINER_BIN from local Docker image $IMAGE"
if ! "$CONTAINER_BIN" build --force "$SIF" "docker-daemon://$IMAGE"; then
  echo "docker-daemon transport failed; falling back to docker-archive" >&2
  TMP_ARCHIVE="$(mktemp --suffix=.tar)"
  docker save "$IMAGE" -o "$TMP_ARCHIVE"
  "$CONTAINER_BIN" build --force "$SIF" "docker-archive://$TMP_ARCHIVE"
fi

SIF_DIR="$(dirname "$SIF")"
SIF_BASE="$(basename "$SIF")"
(
  cd "$SIF_DIR"
  sha256sum "$SIF_BASE" > "$SIF_BASE.sha256"
)

DOCKER_ID="$(docker image inspect "$IMAGE" --format '{{.Id}}')"
DOCKER_DIGEST="$(docker image inspect "$IMAGE" --format '{{join .RepoDigests ","}}')"
SIF_SHA="$(sha256sum "$SIF" | awk '{print $1}')"
META="$SIF.meta.txt"
{
  echo "docker_image=$IMAGE"
  echo "docker_image_id=$DOCKER_ID"
  echo "docker_repo_digests=$DOCKER_DIGEST"
  echo "sif=$SIF"
  echo "sif_sha256=$SIF_SHA"
  echo "conversion_runtime=$($CONTAINER_BIN --version 2>&1 | head -1)"
  echo "conversion_timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo
  echo "--- embedded build info ---"
  "$CONTAINER_BIN" exec "$SIF" cat /opt/pride-scp/build-info.txt
} > "$META"

echo "Running SIF smoke tests"
"$CONTAINER_BIN" exec "$SIF" pride-scp --version
"$CONTAINER_BIN" exec "$SIF" python -c 'import requests, pymupdf, pypdf; print("python imports: OK")'
"$CONTAINER_BIN" exec "$SIF" python /opt/pride-scp/scripts/scp_kg_extract_semantic_claims.py --self-test
"$CONTAINER_BIN" exec "$SIF" ollama --version
(
  cd "$SIF_DIR"
  sha256sum -c "$SIF_BASE.sha256"
)

echo "SIF ready: $SIF"
echo "  sha256: $SIF.sha256"
echo "  metadata: $META"
