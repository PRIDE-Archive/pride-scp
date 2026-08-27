#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

UNIFIED="${SEMANTIC_QC_UNIFIED:-$ROOT/work/python/semantic_unification/unified_semantic_manifest.jsonl}"
PACKETS="${SEMANTIC_QC_INPUT:-$ROOT/work/python/semantic_unification/qc_evidence_packets.jsonl}"
PACKET_SUMMARY="${SEMANTIC_QC_PACKET_SUMMARY:-$ROOT/work/python/semantic_unification/qc_evidence_packet_summary.json}"
OUT="${SEMANTIC_QC_DIR:-$ROOT/work/python/semantic_qc}"
CRITIC_MODEL="${SEMANTIC_QC_CRITIC_MODEL:-phi4-mini:3.8b}"
JURY_MODEL="${SEMANTIC_QC_JURY_MODEL:-gemma3:4b}"
CPU_THREADS="${SEMANTIC_QC_CPU_THREADS:-4}"

python "$ROOT/python/recall/build_semantic_qc_packets.py" \
  --unified-manifest "$UNIFIED" \
  --annotations-dir "$ROOT/work/python/pride_scp_annotations/annotations" \
  --output "$PACKETS" \
  --summary "$PACKET_SUMMARY"

python "$ROOT/python/recall/adjudicate_semantic_qc.py" \
  "$PACKETS" \
  --output-dir "$OUT" \
  --critic-model "$CRITIC_MODEL" \
  --jury-model "$JURY_MODEL" \
  --cpu-threads "$CPU_THREADS"

mkdir -p "$ROOT/work/python/semantic_unification"
cp "$OUT/review_decisions.tsv" \
  "$ROOT/work/python/semantic_unification/review_decisions.tsv"

cat <<EOF

Evidence-grounded semantic QC complete.

Evidence packet summary:
  $PACKET_SUMMARY

Summary:
  $OUT/semantic_qc_summary.json

Full decisions:
  $OUT/semantic_qc_results.tsv

Residual manual cases:
  $OUT/uncertain_for_manual_review.tsv

Bridge decision file updated:
  $ROOT/work/python/semantic_unification/review_decisions.tsv

The v0.1.8 critic/jury cache is version-invalidated automatically; no manual
cache deletion or --force is required. Final Stage-05 bridge generation should
still wait until the residual uncertain queue is inspected/resolved.
EOF
