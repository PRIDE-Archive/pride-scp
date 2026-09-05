#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-$(pwd)}"
SNAPSHOT_DIR="${SNAPSHOT_DIR:-$ROOT/data/snapshot}"
ACCEPTED_ROOT="${ACCEPTED_ROOT:-$ROOT/data/gt196_massive_native_m2_identity_recall_repair}"
M2C0_ROOT="${M2C0_ROOT:-$ROOT/data/gt196_massive_native_m2c_source_bridge}"
M2C1_ROOT="${M2C1_ROOT:-$ROOT/data/gt196_massive_native_m2c1_gt65_curation_benchmark}"
OUT_ROOT="${OUT_ROOT:-$ROOT/data/gt196_massive_native_m2c2_canonical_evidence_fusion}"
ANNOTATIONS_DIR="${ANNOTATIONS_DIR:-$ROOT/work/python/pride_scp_annotations/annotations}"
MODEL="${MODEL:-qwen2.5:3b}"
CPU_THREADS="${CPU_THREADS:-4}"
FORCE="${FORCE:-1}"
mkdir -p "$OUT_ROOT"

for f in \
  "$ACCEPTED_ROOT/m2_identity_recall_repair_summary.json" \
  "$SNAPSHOT_DIR/native/massive/massive_accessions.tsv" \
  "$M2C0_ROOT/m2c_source_bridge_summary.json" \
  "$M2C1_ROOT/m2c1_gt65_curation_benchmark_summary.json" \
  "$M2C1_ROOT/benchmark/massive_gt65_benchmark_candidates.jsonl" \
  "$M2C1_ROOT/benchmark/massive_gt65_benchmark_manifest.tsv" \
  "$M2C1_ROOT/curation/curation_v19_summary.tsv" \
  "$M2C1_ROOT/evaluation/massive_gt65_curation_decision_audit.tsv"; do
  [[ -f "$f" ]] || { echo "ERROR: missing M2-C2 input: $f" >&2; exit 1; }
done

python - "$ACCEPTED_ROOT" "$M2C0_ROOT" "$M2C1_ROOT" <<'PY'
import json,sys
from pathlib import Path
accepted=Path(sys.argv[1]); c0=Path(sys.argv[2]); c1=Path(sys.argv[3])
a=json.loads((accepted/'m2_identity_recall_repair_summary.json').read_text())
b=json.loads((c0/'m2c_source_bridge_summary.json').read_text())
c=json.loads((c1/'m2c1_gt65_curation_benchmark_summary.json').read_text())
if a.get('acceptance_passed') is not True or a.get('massive_gt_recall')!='65/65':
    raise SystemExit('M2-C2 guard failed: accepted identity/discovery baseline is not 65/65')
if a.get('gt_used_for_identity') is not False:
    raise SystemExit('M2-C2 guard failed: GT entered runtime identity')
if b.get('acceptance_passed') is not True or b.get('gt_used_for_bridge') is not False:
    raise SystemExit('M2-C2 guard failed: accepted GT-blind M2-C0 bridge missing')
if c.get('execution_passed') is not True or c.get('gt_rows')!=65:
    raise SystemExit('M2-C2 guard failed: accepted M2-C1 GT65 benchmark missing')
if c.get('exclude') != 0 or c.get('curation_errors') != 0:
    raise SystemExit('M2-C2 guard failed: M2-C1 safety baseline was not 0 exclude / 0 errors')
if c.get('frozen_curation_version') != 'v19-shadow-2.7.1':
    raise SystemExit('M2-C2 guard failed: unexpected curation version')
print('M2-C2 guard: identity 65/65; M2-C1 26 include / 39 review / 0 exclude; frozen v19-shadow-2.7.1')
PY

python "$ROOT/python/curation/build_massive_gt65_canonical_evidence_fusion.py" \
  --m2c1-candidates-jsonl "$M2C1_ROOT/benchmark/massive_gt65_benchmark_candidates.jsonl" \
  --m2c1-manifest "$M2C1_ROOT/benchmark/massive_gt65_benchmark_manifest.tsv" \
  --snapshot "$SNAPSHOT_DIR" \
  --output-dir "$OUT_ROOT/benchmark" \
  --expect-gt-rows 65 \
  --expect-pxd-gt-rows 26 \
  --expect-unique-pxd-representatives 25 \
  | tee "$OUT_ROOT/fusion_build.stdout.json"

CURATION_ARGS=(
  "$OUT_ROOT/benchmark/massive_gt65_canonical_fusion_candidates.jsonl"
  --annotations-dir "$ANNOTATIONS_DIR"
  --output-dir "$OUT_ROOT/curation_affected"
  --model "$MODEL"
  --cpu-threads "$CPU_THREADS"
  --accession-file "$OUT_ROOT/benchmark/affected_pxd_representatives.txt"
)
if [[ "$FORCE" == "1" ]]; then CURATION_ARGS+=(--force); fi
python "$ROOT/python/curation/pride_scp_curation_v19.py" "${CURATION_ARGS[@]}" \
  | tee "$OUT_ROOT/curation_affected.stdout.json"

python "$ROOT/python/curation/merge_massive_gt65_c2_curation.py" \
  --baseline-summary "$M2C1_ROOT/curation/curation_v19_summary.tsv" \
  --affected-summary "$OUT_ROOT/curation_affected/curation_v19_summary.tsv" \
  --affected-accessions "$OUT_ROOT/benchmark/affected_pxd_representatives.txt" \
  --output "$OUT_ROOT/curation_merged/curation_v19_summary.tsv" \
  --expect-total 63 \
  --expect-affected 25

python "$ROOT/python/evaluation/evaluate_massive_gt65_curation_benchmark.py" \
  --manifest "$OUT_ROOT/benchmark/massive_gt65_canonical_fusion_manifest.tsv" \
  --curation-summary "$OUT_ROOT/curation_merged/curation_v19_summary.tsv" \
  --output-dir "$OUT_ROOT/evaluation" \
  --expect-gt-rows 65 \
  | tee "$OUT_ROOT/evaluation.stdout.json"

python "$ROOT/python/evaluation/compare_massive_gt65_canonical_fusion.py" \
  --baseline-audit "$M2C1_ROOT/evaluation/massive_gt65_curation_decision_audit.tsv" \
  --fusion-audit "$OUT_ROOT/evaluation/massive_gt65_curation_decision_audit.tsv" \
  --output-dir "$OUT_ROOT/comparison" \
  --expect-gt-rows 65 \
  | tee "$OUT_ROOT/comparison.stdout.json"

# Write the acceptance summary before applying the final gate so diagnostics
# survive even if the final bounded pass discovers a hard false negative.
python - "$ACCEPTED_ROOT" "$M2C1_ROOT" "$OUT_ROOT" <<'PY'
import json,sys
from pathlib import Path
accepted=Path(sys.argv[1]); c1=Path(sys.argv[2]); out=Path(sys.argv[3])
a=json.loads((accepted/'m2_identity_recall_repair_summary.json').read_text())
b=json.loads((out/'benchmark/massive_gt65_canonical_fusion_build_summary.json').read_text())
c=json.loads((out/'curation_affected/curation_v19_metrics.json').read_text())
e=json.loads((out/'evaluation/massive_gt65_curation_benchmark_summary.json').read_text())
q=json.loads((out/'comparison/massive_gt65_canonical_fusion_comparison_summary.json').read_text())
base=json.loads((c1/'m2c1_gt65_curation_benchmark_summary.json').read_text())
acceptance=(
    a.get('massive_gt_recall')=='65/65'
    and a.get('gt_used_for_identity') is False
    and b.get('fusion_coverage_complete') is True
    and b.get('gt_labels_written_to_curation_candidates') is False
    and b.get('gt_used_for_identity') is False
    and b.get('gt_used_for_fusion') is False
    and b.get('pxd_representatives_with_source_derived_native_aliases')==25
    and c.get('errors')==0
    and c.get('curation_pipeline_version')=='v19-shadow-2.7.1'
    and e.get('gt_rows_evaluated')==65
    and not e.get('missing_curation_representatives')
    and e.get('overall',{}).get('errors')==0
    and e.get('overall',{}).get('hard_false_negatives')==0
)
s={
    'acceptance_passed':acceptance,
    'phase':'M2-C2_final_bounded_canonical_evidence_fusion',
    'massive_gt_discovery_recall':a.get('massive_gt_recall'),
    'frozen_curation_version':c.get('curation_pipeline_version'),
    'gt_used_for_identity':b.get('gt_used_for_identity'),
    'gt_used_for_fusion':b.get('gt_used_for_fusion'),
    'gt_labels_written_to_curation_candidates':b.get('gt_labels_written_to_curation_candidates'),
    'affected_unique_pxd_representatives_rerun':b.get('unique_pxd_representatives'),
    'affected_gt_rows':b.get('pxd_gt_rows'),
    'linked_native_accession_count':b.get('linked_native_accession_count'),
    'fused_native_evidence_item_total':b.get('fused_native_evidence_item_total'),
    'affected_curation_errors':c.get('errors'),
    'baseline_decision_counts':base.get('decision_counts'),
    'fusion_decision_counts':e.get('overall',{}).get('decision_counts'),
    'decision_transition_counts':q.get('decision_transition_counts'),
    'affected_transition_counts':q.get('affected_transition_counts'),
    'hard_false_negatives':e.get('overall',{}).get('hard_false_negatives'),
    'curation_errors':e.get('overall',{}).get('errors'),
    'stopping_policy':'Freeze MassIVE after this pass if safety gates hold. Do not continue prompt/model/rule tuning to reduce residual reviews.',
    'note':'Final bounded architecture-completeness pass. Only 25 PXD representatives were rerun; 38 unaffected native representatives reuse accepted M2-C1 decisions.'
}
(out/'m2c2_canonical_evidence_fusion_summary.json').write_text(json.dumps(s,indent=2,sort_keys=True)+'\n')
print(json.dumps(s,indent=2,sort_keys=True))
if not acceptance:
    raise SystemExit('M2-C2 final safety gate failed; inspect outputs. Do not tune around the failure.')
PY

echo "MassIVE M2-C2 final bounded canonical-evidence-fusion pass complete"
echo "  summary:    $OUT_ROOT/m2c2_canonical_evidence_fusion_summary.json"
echo "  fusion:     $OUT_ROOT/benchmark/canonical_evidence_fusion_audit.tsv"
echo "  decisions:  $OUT_ROOT/evaluation/massive_gt65_curation_decision_audit.tsv"
echo "  comparison: $OUT_ROOT/comparison/massive_gt65_canonical_fusion_comparison_summary.json"
echo "  changed:    $OUT_ROOT/comparison/massive_gt65_canonical_fusion_changed_decisions.tsv"
