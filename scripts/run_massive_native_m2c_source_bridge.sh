#!/usr/bin/env bash
set -euo pipefail
ROOT="${ROOT:-$(pwd)}"
SNAPSHOT_DIR="${SNAPSHOT_DIR:-$ROOT/data/snapshot}"
ACCEPTED_ROOT="${ACCEPTED_ROOT:-$ROOT/data/gt196_massive_native_m2_identity_recall_repair}"
OUT_ROOT="${OUT_ROOT:-$ROOT/data/gt196_massive_native_m2c_source_bridge}"
ANNOTATIONS_DIR="${ANNOTATIONS_DIR:-$ROOT/work/python/pride_scp_annotations/annotations}"
MODEL="${MODEL:-qwen2.5:3b}"
mkdir -p "$OUT_ROOT"
for f in "$ACCEPTED_ROOT/m2_identity_recall_repair_summary.json" "$ACCEPTED_ROOT/discovery/candidates.jsonl" "$ACCEPTED_ROOT/candidate_audit/candidate_diagnostics.tsv" "$ACCEPTED_ROOT/delta_after_identity/massive_native_candidate_delta.tsv" "$ACCEPTED_ROOT/recall_massive/massive_gt65_recall_summary.json"; do
  [[ -f "$f" ]] || { echo "ERROR: missing accepted M2-B.1 input: $f" >&2; exit 1; }
done
python - "$ACCEPTED_ROOT" <<'PY'
import json,sys
from pathlib import Path
r=Path(sys.argv[1]); s=json.loads((r/'m2_identity_recall_repair_summary.json').read_text()); q=json.loads((r/'recall_massive/massive_gt65_recall_summary.json').read_text())
if s.get('acceptance_passed') is not True: raise SystemExit('M2-C guard failed: M2-B.1 is not accepted')
if s.get('gt_used_for_identity') is not False: raise SystemExit('M2-C guard failed: GT entered runtime identity')
if s.get('new_candidate_count_after_repair') != 118: raise SystemExit(f"M2-C guard failed: expected 118 new candidates, saw {s.get('new_candidate_count_after_repair')}")
if q.get('recovered_gt_positives') != 65 or q.get('expected_gt_positives') != 65: raise SystemExit('M2-C guard failed: MassIVE GT65 recall is not 65/65')
print('M2-C guard: accepted identity baseline 537 candidates / 118 new native / GT65 65/65')
PY
python "$ROOT/python/curation/build_massive_native_curation_bridge.py" \
  --candidates-jsonl "$ACCEPTED_ROOT/discovery/candidates.jsonl" \
  --delta-tsv "$ACCEPTED_ROOT/delta_after_identity/massive_native_candidate_delta.tsv" \
  --candidate-diagnostics "$ACCEPTED_ROOT/candidate_audit/candidate_diagnostics.tsv" \
  --snapshot "$SNAPSHOT_DIR" \
  --output-dir "$OUT_ROOT/bridge" \
  --expect-candidates 118 | tee "$OUT_ROOT/bridge.stdout.json"
python "$ROOT/python/curation/pride_scp_curation_v19.py" \
  "$OUT_ROOT/bridge/massive_native_curation_candidates.jsonl" \
  --annotations-dir "$ANNOTATIONS_DIR" \
  --output-dir "$OUT_ROOT/packets" \
  --model "$MODEL" \
  --packets-only | tee "$OUT_ROOT/packets.stdout.json"
python "$ROOT/python/evaluation/evaluate_massive_native_m2c_source_audit.py" \
  --source-audit "$OUT_ROOT/bridge/massive_native_source_evidence_audit.tsv" \
  --delta-audit "$ACCEPTED_ROOT/delta_after_identity/massive_native_candidate_delta.tsv" \
  --output-dir "$OUT_ROOT/evaluation" | tee "$OUT_ROOT/evaluation.stdout.json"
python - "$OUT_ROOT" <<'PY'
import json,sys
from pathlib import Path
r=Path(sys.argv[1]); b=json.loads((r/'bridge/massive_native_curation_bridge_summary.json').read_text()); p=json.loads((r/'packets/curation_v19_metrics.json').read_text()); e=json.loads((r/'evaluation/massive_native_m2c_source_audit_evaluation_summary.json').read_text())
passed=(b.get('gt_used_for_bridge') is False and b.get('candidate_count')==118 and p.get('candidates_selected')==118 and p.get('errors')==0 and e.get('runtime_bridge_gt_used') is False)
s={'acceptance_passed':passed,'phase':'M2-C0_native_source_bridge_packets','frozen_curation_version':p.get('curation_pipeline_version'),'gt_used_for_bridge':b.get('gt_used_for_bridge'),'bridge_candidates':b.get('candidate_count'),'trusted_pxd_alias_candidate_count':b.get('trusted_pxd_alias_candidate_count'),'native_without_trusted_pxd_alias_count':b.get('native_without_trusted_pxd_alias_count'),'publication_identifier_coverage':b.get('publication_identifier_coverage'),'file_inventory_coverage':b.get('file_inventory_coverage'),'metadata_file_hint_coverage':b.get('metadata_file_hint_coverage'),'sample_or_method_evidence_coverage':b.get('sample_or_method_evidence_coverage'),'packet_status_counts':b.get('packet_status_counts'),'packets_selected':p.get('candidates_selected'),'packet_errors':p.get('errors'),'evaluation_only_gt_positive_rows':e.get('frozen_gt_positive',{}).get('rows'),'evaluation_only_non_gt_rows':e.get('absent_from_frozen_gt',{}).get('rows'),'note':'Packets-only bridge. v19 decision policy/model prompt are unchanged; GT is joined only in the separate evaluation output.'}
(r/'m2c_source_bridge_summary.json').write_text(json.dumps(s,indent=2,sort_keys=True)+'\n'); print(json.dumps(s,indent=2,sort_keys=True))
if not passed: raise SystemExit('M2-C0 source bridge acceptance gate failed')
PY
echo "MassIVE M2-C0 source evidence + packet bridge complete"
echo "  summary:    $OUT_ROOT/m2c_source_bridge_summary.json"
echo "  source:     $OUT_ROOT/bridge/massive_native_source_evidence_audit.tsv"
echo "  review:     $OUT_ROOT/evaluation/massive_native_m2c_non_gt_source_review_queue.tsv"
echo "  packets:    $OUT_ROOT/packets"
