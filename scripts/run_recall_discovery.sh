#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BIN="${PRIDE_SCP_BIN:-$ROOT/target/release/pride-scp}"
SNAPSHOT_DIR="${SNAPSHOT_DIR:-$ROOT/data/snapshot}"
DISCOVERY_DIR="${DISCOVERY_DIR:-$ROOT/data/discovery}"
BRIDGE_DIR="${BRIDGE_DIR:-$ROOT/data/python_bridge}"
CONCURRENCY="${PRIDE_CONCURRENCY:-8}"
TIMEOUT="${PRIDE_TIMEOUT:-120}"
RETRIES="${PRIDE_RETRIES:-4}"
PROJECT_PAGE_SIZE="${PRIDE_PROJECT_PAGE_SIZE:-100}"

if [[ ! -x "$BIN" ]]; then
  echo "ERROR: build the Rust CLI first: cargo build --release --locked" >&2
  exit 1
fi

"$BIN" snapshot \
  --output "$SNAPSHOT_DIR" \
  --concurrency "$CONCURRENCY" \
  --timeout "$TIMEOUT" \
  --retries "$RETRIES" \
  --project-page-size "$PROJECT_PAGE_SIZE"

"$BIN" discover \
  --snapshot "$SNAPSHOT_DIR" \
  --config "$ROOT/config/discovery_terms.json" \
  --output "$DISCOVERY_DIR" \
  --min-score 1

"$BIN" candidate-audit \
  --candidates "$DISCOVERY_DIR/candidates.jsonl" \
  --config "$ROOT/config/discovery_terms.json" \
  --output "$ROOT/data/candidate_audit"

"$BIN" export-python \
  --candidates "$DISCOVERY_DIR/candidates.tsv" \
  --candidates-jsonl "$DISCOVERY_DIR/candidates.jsonl" \
  --config "$ROOT/config/discovery_terms.json" \
  --output "$BRIDGE_DIR" \
  --min-tier weak

KNOWN="${KNOWN_POSITIVES:-$ROOT/benchmarks/known_positives_2026-08-20.csv}"
if [[ -f "$KNOWN" ]]; then
  "$BIN" recall-audit \
    --candidates "$DISCOVERY_DIR/candidates.tsv" \
    --known-positives "$KNOWN" \
    --output "$ROOT/data/recall_audit"
else
  echo "NOTE: no known-positive benchmark at $KNOWN; recall audit skipped." >&2
fi
