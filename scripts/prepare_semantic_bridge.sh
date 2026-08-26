#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BIN="${PRIDE_SCP_BIN:-$ROOT/target/release/pride-scp}"
DISCOVERY_DIR="${DISCOVERY_DIR:-$ROOT/data/discovery}"
AUDIT_DIR="${CANDIDATE_AUDIT_DIR:-$ROOT/data/candidate_audit}"
BRIDGE_DIR="${BRIDGE_DIR:-$ROOT/data/python_bridge}"

if [[ ! -x "$BIN" ]]; then
  echo "ERROR: build the Rust CLI first: cargo build --release --locked" >&2
  exit 1
fi

"$BIN" candidate-audit \
  --candidates "$DISCOVERY_DIR/candidates.jsonl" \
  --config "$ROOT/config/discovery_terms.json" \
  --output "$AUDIT_DIR"

"$BIN" export-python \
  --candidates "$DISCOVERY_DIR/candidates.tsv" \
  --candidates-jsonl "$DISCOVERY_DIR/candidates.jsonl" \
  --config "$ROOT/config/discovery_terms.json" \
  --output "$BRIDGE_DIR" \
  --min-tier weak

cat <<MSG
Semantic bridge prepared.

Candidate diagnostics:
  $AUDIT_DIR/candidate_diagnostics.tsv
  $AUDIT_DIR/candidate_diagnostics.jsonl

Python bridge:
  $BRIDGE_DIR/candidate_accessions.txt
  $BRIDGE_DIR/candidate_manifest.tsv
  $BRIDGE_DIR/semantic_candidates.jsonl

Next:
  scripts/run_python_publication_enrichment.sh
MSG
