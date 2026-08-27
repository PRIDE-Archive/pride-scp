#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

OUT="${SEMANTIC_UNIFICATION_DIR:-$ROOT/work/python/semantic_unification}"
KNOWN="${KNOWN_POSITIVES:-$ROOT/benchmarks/known_positives_2026-08-20.csv}"

args=(
  python "$ROOT/python/recall/unify_semantic_results.py"
  --candidate-diagnostics "$ROOT/data/candidate_audit/candidate_diagnostics.jsonl"
  --publication-candidates "$ROOT/work/python/semantic_partition/publication_backed_candidates.jsonl"
  --repository-candidates "$ROOT/work/python/semantic_partition/repository_only_candidates.jsonl"
  --annotations-dir "$ROOT/work/python/pride_scp_annotations/annotations"
  --repository-triage "$ROOT/work/python/repository_triage/repository_semantic_triage.tsv"
  --output-dir "$OUT"
)

if [[ -f "$KNOWN" ]]; then
  args+=(--known-positives "$KNOWN")
fi

"${args[@]}"

cat <<EOF

Semantic unification complete.

Summary:
  $OUT/semantic_unification_summary.json

All 321 candidates:
  $OUT/unified_semantic_manifest.tsv

Secondary-review queue:
  $OUT/secondary_review_queue.tsv

Decision template:
  $OUT/review_decisions.template.tsv

IMPORTANT:
  - include_candidate is provisional and still requires downstream QC.
  - likely_non_scp rows are retained; they are not deleted from the recall universe.
  - do not build the final Stage-05 catalogue until review candidates are adjudicated.
EOF
