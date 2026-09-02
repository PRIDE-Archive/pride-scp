#!/usr/bin/env bash
set -euo pipefail

# Evaluation-only benchmark runner for the frozen GT196 reference.
# The frozen GT is consulted only after independent candidate generation.
# Iteration 2 first ensures a cached ProteomeCentral/PROXI registry supplement so
# cross-repository PXD aliases absent from the primary PRIDE snapshot can enter
# discovery without hard-coded accession knowledge.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BIN="${PRIDE_SCP_BIN:-$ROOT/target/release/pride-scp}"
SNAPSHOT_DIR="${SNAPSHOT_DIR:-$ROOT/data/snapshot}"
GT_MASTER="${GT_MASTER:-$ROOT/gpt/final_curation_20260831/PRIDE_SCP_GT_REFERENCE_MASTER_2026-08-31_FINAL_v196.csv}"
OUT_ROOT="${OUT_ROOT:-$ROOT/data/gt196_discovery_benchmark}"
DISCOVERY_DIR="$OUT_ROOT/discovery"
AUDIT_DIR="$OUT_ROOT/candidate_audit"
RECALL_DIR="$OUT_ROOT/recall_pride"
REGISTRY_API_BASE="${REGISTRY_API_BASE:-https://proteomecentral.proteomexchange.org/api/proxi/v0.1}"
REGISTRY_PAGE_SIZE="${REGISTRY_PAGE_SIZE:-100}"
SKIP_REGISTRY_SNAPSHOT="${SKIP_REGISTRY_SNAPSHOT:-0}"
REGISTRY_FORCE="${REGISTRY_FORCE:-0}"

if [[ ! -x "$BIN" ]]; then
  echo "ERROR: build the Rust CLI first: cargo build --release --locked" >&2
  exit 1
fi
if [[ ! -d "$SNAPSHOT_DIR" ]]; then
  echo "ERROR: snapshot directory not found: $SNAPSHOT_DIR" >&2
  exit 1
fi
if [[ ! -f "$GT_MASTER" ]]; then
  echo "ERROR: frozen GT master not found: $GT_MASTER" >&2
  exit 1
fi

mkdir -p "$OUT_ROOT"

if [[ "$SKIP_REGISTRY_SNAPSHOT" != "1" ]]; then
  registry_args=(
    registry-snapshot
    --snapshot "$SNAPSHOT_DIR"
    --api-base "$REGISTRY_API_BASE"
    --page-size "$REGISTRY_PAGE_SIZE"
  )
  if [[ "$REGISTRY_FORCE" == "1" ]]; then
    registry_args+=(--force)
  fi
  "$BIN" "${registry_args[@]}"
fi

"$BIN" discover \
  --snapshot "$SNAPSHOT_DIR" \
  --config "$ROOT/config/discovery_terms.json" \
  --output "$DISCOVERY_DIR" \
  --min-score 1

"$BIN" candidate-audit \
  --candidates "$DISCOVERY_DIR/candidates.jsonl" \
  --config "$ROOT/config/discovery_terms.json" \
  --output "$AUDIT_DIR"

"$BIN" recall-audit \
  --candidates "$DISCOVERY_DIR/candidates.tsv" \
  --known-positives "$GT_MASTER" \
  --repository-filter PRIDE \
  --output "$RECALL_DIR"

printf 'GT196 discovery benchmark complete\n'
printf '  registry: %s\n' "$SNAPSHOT_DIR/registry"
printf '  discovery: %s\n' "$DISCOVERY_DIR"
printf '  candidate audit: %s\n' "$AUDIT_DIR"
printf '  PRIDE recall: %s\n' "$RECALL_DIR"
