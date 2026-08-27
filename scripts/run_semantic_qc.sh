#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

INPUT="${SEMANTIC_QC_INPUT:-$ROOT/work/python/semantic_unification/qc_candidate_queue.jsonl}"
OUT="${SEMANTIC_QC_DIR:-$ROOT/work/python/semantic_qc}"
CRITIC_MODEL="${SEMANTIC_QC_CRITIC_MODEL:-phi4-mini:3.8b}"
JURY_MODEL="${SEMANTIC_QC_JURY_MODEL:-gemma3:4b}"
CPU_THREADS="${SEMANTIC_QC_CPU_THREADS:-4}"

python "$ROOT/python/recall/adjudicate_semantic_qc.py" \
  "$INPUT" \
  --output-dir "$OUT" \
  --critic-model "$CRITIC_MODEL" \
  --jury-model "$JURY_MODEL" \
  --cpu-threads "$CPU_THREADS"

mkdir -p "$ROOT/work/python/semantic_unification"
cp "$OUT/review_decisions.tsv" \
  "$ROOT/work/python/semantic_unification/review_decisions.tsv"

cat <<EOF

Independent semantic QC complete.

Summary:
  $OUT/semantic_qc_summary.json

Full decisions:
  $OUT/semantic_qc_results.tsv

Manual unresolved cases:
  $OUT/uncertain_for_manual_review.tsv

Bridge decision file updated:
  $ROOT/work/python/semantic_unification/review_decisions.tsv

Next:
  scripts/build_stage05_bridge.sh

The bridge will refuse to build if any secondary-review candidate remains
uncertain. Resolve only those residual rows manually before Stage 05.
EOF
