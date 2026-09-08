#!/usr/bin/env bash
set -euo pipefail

MODEL="${MODEL:-${PRIDE_SCP_OLLAMA_MODEL:-qwen2.5:3b}}"
OLLAMA_HOST="${OLLAMA_HOST:-127.0.0.1:11434}"
export OLLAMA_HOST
export OLLAMA_NO_CLOUD="${OLLAMA_NO_CLOUD:-1}"

if [[ -z "${OLLAMA_MODELS:-}" ]]; then
  if [[ -d /models/ollama ]]; then
    export OLLAMA_MODELS=/models/ollama
  elif [[ -d /opt/pride-scp/baked-ollama-models ]]; then
    export OLLAMA_MODELS=/opt/pride-scp/baked-ollama-models
  else
    echo "ERROR: no Ollama model store configured. Bind a model directory and set OLLAMA_MODELS." >&2
    exit 2
  fi
fi

mkdir -p "${PRIDE_SCP_RUNTIME_TMP:-/tmp/pride-scp-runtime}"
OLLAMA_LOG="${OLLAMA_LOG:-${PRIDE_SCP_RUNTIME_TMP:-/tmp/pride-scp-runtime}/ollama.log}"

ollama serve >"$OLLAMA_LOG" 2>&1 &
OLLAMA_PID=$!
cleanup() {
  local rc=$?
  kill "$OLLAMA_PID" >/dev/null 2>&1 || true
  wait "$OLLAMA_PID" >/dev/null 2>&1 || true
  exit "$rc"
}
trap cleanup EXIT INT TERM

ready=0
for _ in $(seq 1 60); do
  if ollama list >/tmp/pride_scp_ollama_list.$$ 2>/dev/null; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "$ready" != "1" ]]; then
  echo "ERROR: Ollama server did not become ready. Log: $OLLAMA_LOG" >&2
  tail -100 "$OLLAMA_LOG" >&2 || true
  exit 2
fi

if [[ "${OLLAMA_REQUIRE_MODEL:-1}" == "1" ]]; then
  if ! ollama list | awk 'NR>1 {print $1}' | grep -Fxq "$MODEL"; then
    echo "ERROR: required Ollama model '$MODEL' is not present in $OLLAMA_MODELS" >&2
    echo "Available models:" >&2
    ollama list >&2 || true
    echo "No network pull is attempted at runtime. Prefetch/bake the model before HPC deployment." >&2
    exit 2
  fi
fi

if [[ $# -eq 0 ]]; then
  exec bash
fi

"$@"
