#!/usr/bin/env bash
set -euo pipefail
ROOT="${ROOT:-$(pwd)}"
ACCEPTED_ROOT="${ACCEPTED_ROOT:-$ROOT/data/gt196_massive_native_m2_identity_recall_repair}"
BRIDGE_ROOT="${BRIDGE_ROOT:-$ROOT/data/gt196_massive_native_m2c_source_bridge}"
OUT_ROOT="${OUT_ROOT:-$ROOT/data/gt196_massive_native_m2c1_gt65_curation_benchmark}"
ANNOTATIONS_DIR="${ANNOTATIONS_DIR:-$ROOT/work/python/pride_scp_annotations/annotations}"
MODEL="${MODEL:-qwen2.5:3b}"
CPU_THREADS="${CPU_THREADS:-4}"
FORCE="${FORCE:-1}"
mkdir -p "$OUT_ROOT"

for f in \
  "$ACCEPTED_ROOT/m2_identity_recall_repair_summary.json" \
  "$ACCEPTED_ROOT/discovery/candidates.jsonl" \
  "$ACCEPTED_ROOT/recall_massive/massive_gt65_recall_audit.tsv" \
  "$BRIDGE_ROOT/m2c_source_bridge_summary.json" \
  "$BRIDGE_ROOT/bridge/massive_native_curation_candidates.jsonl"; do
  [[ -f "$f" ]] || { echo "ERROR: missing M2-C1 input: $f" >&2; exit 1; }
done

python - "$ACCEPTED_ROOT" "$BRIDGE_ROOT" <<'PY'
import json,sys
from pathlib import Path
a=Path(sys.argv[1]); b=Path(sys.argv[2])
s=json.loads((a/'m2_identity_recall_repair_summary.json').read_text())
c=json.loads((b/'m2c_source_bridge_summary.json').read_text())
if s.get('acceptance_passed') is not True or s.get('massive_gt_recall')!='65/65': raise SystemExit('M2-C1 guard failed: accepted 65/65 identity baseline missing')
if s.get('gt_used_for_identity') is not False: raise SystemExit('M2-C1 guard failed: GT entered runtime identity')
if c.get('acceptance_passed') is not True or c.get('gt_used_for_bridge') is not False: raise SystemExit('M2-C1 guard failed: M2-C0 bridge not accepted/GT-blind')
if c.get('bridge_candidates') != 118 or c.get('packet_errors') != 0: raise SystemExit('M2-C1 guard failed: M2-C0 packet bridge incomplete')
print('M2-C1 guard: identity 65/65; M2-C0 118/118 packetized; runtime bridge GT-blind')
PY

python "$ROOT/python/curation/build_massive_gt65_curation_benchmark.py" \
  --accepted-candidates-jsonl "$ACCEPTED_ROOT/discovery/candidates.jsonl" \
  --native-bridge-candidates-jsonl "$BRIDGE_ROOT/bridge/massive_native_curation_candidates.jsonl" \
  --recall-audit-tsv "$ACCEPTED_ROOT/recall_massive/massive_gt65_recall_audit.tsv" \
  --output-dir "$OUT_ROOT/benchmark" \
  --expect-gt-rows 65 \
  | tee "$OUT_ROOT/benchmark_build.stdout.json"

CURATION_ARGS=(
  "$OUT_ROOT/benchmark/massive_gt65_benchmark_candidates.jsonl"
  --annotations-dir "$ANNOTATIONS_DIR"
  --output-dir "$OUT_ROOT/curation"
  --model "$MODEL"
  --cpu-threads "$CPU_THREADS"
)
if [[ "$FORCE" == "1" ]]; then CURATION_ARGS+=(--force); fi
python "$ROOT/python/curation/pride_scp_curation_v19.py" "${CURATION_ARGS[@]}" \
  | tee "$OUT_ROOT/curation.stdout.json"

python "$ROOT/python/evaluation/evaluate_massive_gt65_curation_benchmark.py" \
  --manifest "$OUT_ROOT/benchmark/massive_gt65_benchmark_manifest.tsv" \
  --curation-summary "$OUT_ROOT/curation/curation_v19_summary.tsv" \
  --output-dir "$OUT_ROOT/evaluation" \
  --expect-gt-rows 65 \
  | tee "$OUT_ROOT/evaluation.stdout.json"

python - "$OUT_ROOT" <<'PY'
import json,sys
from pathlib import Path
r=Path(sys.argv[1])
b=json.loads((r/'benchmark/massive_gt65_benchmark_build_summary.json').read_text())
c=json.loads((r/'curation/curation_v19_metrics.json').read_text())
e=json.loads((r/'evaluation/massive_gt65_curation_benchmark_summary.json').read_text())
execution_passed=(b.get('gt_rows')==65 and b.get('gt_labels_written_to_curation_candidates') is False and c.get('errors')==0 and e.get('gt_rows_evaluated')==65 and not e.get('missing_curation_representatives'))
s={
 'execution_passed':execution_passed,
 'phase':'M2-C1_frozen_v19_massive_gt65_curation_benchmark',
 'frozen_curation_version':c.get('curation_pipeline_version'),
 'gt_rows':e.get('gt_rows_evaluated'),
 'unique_curation_candidates':b.get('unique_curation_candidates'),
 'gt_labels_written_to_curation_candidates':b.get('gt_labels_written_to_curation_candidates'),
 'representation_class_counts':b.get('representation_class_counts'),
 'recovery_route_counts':b.get('recovery_route_counts'),
 'decision_counts':e.get('overall',{}).get('decision_counts'),
 'include':e.get('overall',{}).get('includes'),
 'review':e.get('overall',{}).get('reviews'),
 'exclude':e.get('overall',{}).get('hard_false_negatives'),
 'curation_errors':e.get('overall',{}).get('errors'),
 'with_publication_annotations':e.get('overall',{}).get('with_publication_annotations'),
 'with_nonempty_packet':e.get('overall',{}).get('with_nonempty_packet'),
 'by_recovery_route':e.get('by_recovery_route'),
 'by_representation_class':e.get('by_representation_class'),
 'note':'Diagnostic benchmark. Automated excludes are hard false negatives to investigate; this stage does not tune v19 or modify GT196.'
}
(r/'m2c1_gt65_curation_benchmark_summary.json').write_text(json.dumps(s,indent=2,sort_keys=True)+'\n')
print(json.dumps(s,indent=2,sort_keys=True))
if not execution_passed: raise SystemExit('M2-C1 execution gate failed')
PY

echo "MassIVE M2-C1 frozen v19 GT65 curation benchmark complete"
echo "  summary: $OUT_ROOT/m2c1_gt65_curation_benchmark_summary.json"
echo "  audit:   $OUT_ROOT/evaluation/massive_gt65_curation_decision_audit.tsv"
echo "  reviews: $OUT_ROOT/evaluation/massive_gt65_reviews.tsv"
echo "  hard FN: $OUT_ROOT/evaluation/massive_gt65_hard_false_negatives.tsv"
