#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

IMAGE_REPOSITORY="${PRIDE_SCP_IMAGE_REPOSITORY:-pride-scp}"
IMAGE_TAG="${PRIDE_SCP_IMAGE_TAG:-dev}"
TARGET="${PRIDE_SCP_DOCKER_TARGET:-runtime}"
PLATFORM="${PRIDE_SCP_DOCKER_PLATFORM:-linux/amd64}"
REQUIRE_CLEAN="${PRIDE_SCP_REQUIRE_CLEAN:-0}"
RUST_BASE_IMAGE="${PRIDE_SCP_RUST_BASE_IMAGE:-rust:1.80.1-bookworm}"
RUNTIME_BASE_IMAGE="${PRIDE_SCP_RUNTIME_BASE_IMAGE:-ubuntu:24.04}"
OLLAMA_IMAGE="${PRIDE_SCP_OLLAMA_IMAGE:-ollama/ollama:0.33.3}"
UV_IMAGE="${PRIDE_SCP_UV_IMAGE:-ghcr.io/astral-sh/uv:0.12.10}"
PYTHON_VERSION="${PRIDE_SCP_PYTHON_VERSION:-3.12.12}"
BAKE_MODEL="${PRIDE_SCP_BAKE_OLLAMA_MODEL:-0}"
OLLAMA_MODEL="${PRIDE_SCP_OLLAMA_MODEL:-qwen2.5:3b}"
IMAGE="${IMAGE_REPOSITORY}:${IMAGE_TAG}"

command -v docker >/dev/null 2>&1 || { echo "ERROR: docker is required" >&2; exit 2; }

PROJECT_VERSION="$(python3 - <<'PY'
import tomllib
print(tomllib.load(open('Cargo.toml','rb'))['workspace']['package']['version'])
PY
)"

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  VCS_REF="$(git rev-parse HEAD)"
  if [[ -n "$(git status --porcelain)" ]]; then SOURCE_STATE=dirty; else SOURCE_STATE=clean; fi
  SOURCE_FINGERPRINT="$(git ls-files -z | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | awk '{print $1}')"
else
  VCS_REF=unknown
  SOURCE_STATE=unknown
  SOURCE_FINGERPRINT="$(find . -type f ! -path './target/*' ! -path './data/*' ! -path './work/*' -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | awk '{print $1}')"
fi

if [[ "$REQUIRE_CLEAN" == "1" && "$SOURCE_STATE" != "clean" ]]; then
  echo "ERROR: PRIDE_SCP_REQUIRE_CLEAN=1 but source tree state is '$SOURCE_STATE'" >&2
  exit 2
fi
if [[ "$SOURCE_STATE" != "clean" ]]; then
  echo "WARNING: building from source_state=$SOURCE_STATE. Definitive scientific images should use a clean Git tree." >&2
fi

BUILD_TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
IMAGE_BUILD_ID="${PROJECT_VERSION}-${VCS_REF:0:12}-${SOURCE_FINGERPRINT:0:12}"

printf 'Building %s\n' "$IMAGE"
printf '  platform=%s target=%s\n' "$PLATFORM" "$TARGET"
printf '  version=%s vcs=%s source=%s\n' "$PROJECT_VERSION" "$VCS_REF" "$SOURCE_STATE"
printf '  fingerprint=%s\n' "$SOURCE_FINGERPRINT"
printf '  ollama_image=%s model=%s baked=%s\n' "$OLLAMA_IMAGE" "$OLLAMA_MODEL" "$BAKE_MODEL"

DOCKER_BUILDKIT=1 docker build \
  --platform "$PLATFORM" \
  --target "$TARGET" \
  --file containers/Dockerfile \
  --tag "$IMAGE" \
  --build-arg "RUST_BASE_IMAGE=$RUST_BASE_IMAGE" \
  --build-arg "RUNTIME_BASE_IMAGE=$RUNTIME_BASE_IMAGE" \
  --build-arg "OLLAMA_IMAGE=$OLLAMA_IMAGE" \
  --build-arg "UV_IMAGE=$UV_IMAGE" \
  --build-arg "PYTHON_VERSION=$PYTHON_VERSION" \
  --build-arg "PROJECT_VERSION=$PROJECT_VERSION" \
  --build-arg "VCS_REF=$VCS_REF" \
  --build-arg "SOURCE_STATE=$SOURCE_STATE" \
  --build-arg "SOURCE_FINGERPRINT=$SOURCE_FINGERPRINT" \
  --build-arg "IMAGE_BUILD_ID=$IMAGE_BUILD_ID" \
  --build-arg "BUILD_TIMESTAMP=$BUILD_TIMESTAMP" \
  --build-arg "PRIDE_SCP_BAKE_OLLAMA_MODEL=$BAKE_MODEL" \
  --build-arg "PRIDE_SCP_OLLAMA_MODEL=$OLLAMA_MODEL" \
  .

printf '\nImage provenance:\n'
docker image inspect "$IMAGE" --format '  id={{.Id}} size={{.Size}} created={{.Created}}'
docker run --rm "$IMAGE" cat /opt/pride-scp/build-info.txt
