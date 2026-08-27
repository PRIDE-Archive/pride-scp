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
CRITIC_NUM_PREDICT="${SEMANTIC_QC_CRITIC_NUM_PREDICT:-520}"
JURY_NUM_PREDICT="${SEMANTIC_QC_JURY_NUM_PREDICT:-800}"
STRUCTURED_RETRIES="${SEMANTIC_QC_STRUCTURED_RETRIES:-1}"

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
  --cpu-threads "$CPU_THREADS" \
  --critic-num-predict "$CRITIC_NUM_PREDICT" \
  --jury-num-predict "$JURY_NUM_PREDICT" \
  --structured-retries "$STRUCTURED_RETRIES"

mkdir -p "$ROOT/work/python/semantic_unification"
cp "$OUT/review_decisions.tsv" \
  "$ROOT/work/python/semantic_unification/review_decisions.tsv"

cat <<EOF

Positive-chain evidence-grounded semantic QC complete.

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

v0.1.11 evaluates the target MS sample unit explicitly, so many cells feeding one
proteomic replicate cannot masquerade as single-cell MS, while separate multi-cell
controls do not negate genuine one-cell target samples. Evidence-aware arbitration
and structured-output retries remain enabled; older QC caches are invalidated.
EOF
