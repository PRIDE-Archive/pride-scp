#!/usr/bin/env bash
set -euo pipefail

MODEL="${1:-${PRIDE_SCP_OLLAMA_MODEL:-qwen2.5:3b}}"
MODEL_DIR="${2:-$PWD/models/ollama}"
OLLAMA_IMAGE="${PRIDE_SCP_OLLAMA_IMAGE:-ollama/ollama:0.33.3}"
mkdir -p "$MODEL_DIR"
MODEL_DIR="$(cd "$MODEL_DIR" && pwd)"

command -v docker >/dev/null 2>&1 || { echo "ERROR: docker is required" >&2; exit 2; }

CID="$(docker run -d --rm \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp \
  -e OLLAMA_MODELS=/models \
  -v "$MODEL_DIR:/models" \
  "$OLLAMA_IMAGE" serve)"
cleanup() { docker rm -f "$CID" >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM

for _ in $(seq 1 60); do
  docker exec "$CID" ollama list >/dev/null 2>&1 && break
  sleep 1
done

echo "Prefetching model '$MODEL' into $MODEL_DIR"
docker exec "$CID" ollama pull "$MODEL"
docker exec "$CID" ollama show "$MODEL" > "$MODEL_DIR/model-${MODEL//[:\/]/_}.info.txt"
(cd "$MODEL_DIR" && find . -type f ! -name 'model-store.sha256' -print0 | LC_ALL=C sort -z | xargs -0 sha256sum > model-store.sha256)
echo "Model cache ready: $MODEL_DIR"
