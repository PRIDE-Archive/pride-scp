#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

UNIFIED="${SEMANTIC_UNIFICATION_DIR:-$ROOT/work/python/semantic_unification}"
OUT="${STAGE05_BRIDGE_DIR:-$ROOT/work/python/stage05_bridge}"
DECISIONS="${REVIEW_DECISIONS:-$UNIFIED/review_decisions.tsv}"

if [[ ! -f "$DECISIONS" ]]; then
  if [[ -f "$UNIFIED/review_decisions.template.tsv" ]]; then
    cp "$UNIFIED/review_decisions.template.tsv" "$DECISIONS"
    echo "Created review decision file: $DECISIONS" >&2
  fi
  cat >&2 <<EOF
ERROR: secondary-review candidates have not been adjudicated yet.
Fill the 'final_decision' column with include/exclude in:
  $DECISIONS

For a structural smoke test only, call the Python helper directly with
--allow-provisional. Do not use that mode for the final catalogue.
EOF
  exit 2
fi

python "$ROOT/python/recall/build_stage05_bridge.py" \
  --unified-manifest "$UNIFIED/unified_semantic_manifest.jsonl" \
  --publication-candidates "$ROOT/work/python/semantic_partition/publication_backed_candidates.jsonl" \
  --repository-candidates "$ROOT/work/python/semantic_partition/repository_only_candidates.jsonl" \
  --annotations-dir "$ROOT/work/python/pride_scp_annotations/annotations" \
  --review-decisions "$DECISIONS" \
  --output-dir "$OUT"
