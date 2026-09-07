#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-$PWD}"
BIN="${BIN:-$ROOT/target/release/pride-scp}"
SNAPSHOT="${SNAPSHOT:-$ROOT/data/snapshot}"
ANNOTATIONS="${ANNOTATIONS:-$ROOT/work/python/pride_scp_annotations/annotations}"
RESOLVED_SDRF_DIR="${RESOLVED_SDRF_DIR:-$ROOT/data/sdrf_source_resolution_gt105_pride_v024/resolved}"
V041="${V041:-$ROOT/data/sdrf_mapping_relation_recheck_gt105_pride_v041}"
PUB_MANIFEST="${PUB_MANIFEST:-$V041/publication_manifest_with_text.tsv}"
V042C="${V042C:-$ROOT/data/sdrf_mapping_role_recheck_gt105_pride_v042c}"
REPORTER_AUDIT="${REPORTER_AUDIT:-$V042C/audit/sdrf_mapping_evidence_audit.tsv}"
V043A="${V043A:-$ROOT/data/sdrf_reporter_run_scope_audit_gt105_pride_v043a}"
SUPPORT_XLSX="${SUPPORT_XLSX:-$V043A/audit/support_files/Choi_2021_Patch_proteomics_experimental_design.xlsx}"
FILES_JSON="${FILES_JSON:-$SNAPSHOT/files/PXD028040.json}"
MANIFEST_OUT="${MANIFEST_OUT:-$ROOT/data/sdrf_full_repository_mapping_manifest_gt105_pride_v045/audit}"
OUT="${OUT:-$ROOT/data/sdrf_full_repository_mapping_gt105_pride_v045}"
MODEL="${MODEL:-qwen2.5:3b}"

for f in "$FILES_JSON" "$SUPPORT_XLSX" "$REPORTER_AUDIT" "$PUB_MANIFEST"; do
  if [[ ! -f "$f" ]]; then
    echo "ERROR: required v0.4.5 source input not found: $f" >&2
    exit 2
  fi
done
if [[ ! -x "$BIN" ]]; then
  echo "ERROR: pride-scp binary not executable: $BIN" >&2
  echo "Build the v0.4.5 overlay first with: cargo build --release --locked" >&2
  exit 2
fi

mkdir -p "$MANIFEST_OUT" "$OUT"

cat > "$OUT/run_manifest.json" <<JSON
{
  "phase": "GT105_PRIDE_SDRF_v045_full_repository_explicit_mapping",
  "generator_version": "pride-scp-sdrf-v0.4.5",
  "accession": "PXD028040",
  "publication_manifest": "$PUB_MANIFEST",
  "support_workbook": "$SUPPORT_XLSX",
  "reporter_audit": "$REPORTER_AUDIT",
  "architectural_changes": [
    "extend the accepted PXD028040 explicit mapping from the nine biological single-neuron acquisitions to all 16 repository RAWs using deposited workbook date+SC keys",
    "represent the seven pre-single-neuron whole-tissue method-development/reference acquisitions as non-single-cell study-sample rows rather than forcing neuron identities",
    "retain TMT128/TMT131 only on workbook rows that explicitly state those reporter assignments and leave reporter label unavailable on the three rows without explicit TMT assignments",
    "prevent single-cell cell-type/isolation/individual metadata from leaking into explicitly non-single-cell manifest rows",
    "preserve the nine DA-neuron rows and their 3x3 technical-replicate structure unchanged",
    "remove repository-scope incompleteness only when every snapshot RAW is uniquely source-mapped"
  ],
  "expected_remaining_blocker": "template isolation vocabulary gap for patch-clamp microaspiration on the nine true single-neuron rows",
  "gt_use": "accession cohort seed/evaluation only",
  "runtime_sdrf_gt_metadata_used": false,
  "runtime_sdrf_gt_labels_used": false
}
JSON
cat "$OUT/run_manifest.json"

python "$ROOT/scripts/sdrf_reporter_full_design_manifest.py" \
  --accession PXD028040 \
  --files-json "$FILES_JSON" \
  --support-xlsx "$SUPPORT_XLSX" \
  --reporter-audit-tsv "$REPORTER_AUDIT" \
  --output "$MANIFEST_OUT"

ROW_MANIFEST="$MANIFEST_OUT/explicit_row_mapping_manifest.tsv"
if [[ ! -f "$ROW_MANIFEST" ]]; then
  echo "ERROR: full explicit row-mapping manifest was not created: $ROW_MANIFEST" >&2
  exit 2
fi

python - "$ROW_MANIFEST" <<'PY'
import csv, sys
from collections import Counter, defaultdict
p = sys.argv[1]
with open(p) as fh:
    rows = list(csv.DictReader(fh, delimiter="\t"))
if len(rows) != 16:
    raise SystemExit(f"ERROR: expected 16 explicit PXD028040 mapping rows, found {len(rows)}")
if len({r['raw_file'].lower() for r in rows}) != 16:
    raise SystemExit("ERROR: full manifest contains duplicate RAW assignments")
counts = Counter(r['sample_type'] for r in rows)
if counts != Counter({'single cell': 9, 'study sample': 7}):
    raise SystemExit(f"ERROR: unexpected sample-type contract: {counts}")
single = [r for r in rows if r['sample_type'] == 'single cell']
support = [r for r in rows if r['sample_type'] == 'study sample']
if {r['label'] for r in single} != {'TMT128'} or {r['carrier_channel'] for r in single} != {'TMT131'}:
    raise SystemExit("ERROR: single-neuron reporter contract changed")
if not all(r['cell_identifier'] == 'not applicable' and r['cells_per_well'] == 'not applicable' for r in support):
    raise SystemExit("ERROR: non-single support rows acquired fabricated cell identities/cell counts")
if Counter((r['label'], r['carrier_channel']) for r in support) != Counter({('not available','not applicable'): 3, ('TMT128','TMT131'): 4}):
    raise SystemExit("ERROR: whole-tissue support reporter contract changed")
by_sample = defaultdict(list)
for row in single:
    by_sample[row['source_name']].append(int(row['technical_replicate']))
expected = {'DA_neuron_1': [1,2,3], 'DA_neuron_2': [1,2,3], 'DA_neuron_3': [1,2,3]}
actual = {k: sorted(v) for k, v in by_sample.items()}
if actual != expected:
    raise SystemExit(f"ERROR: biological/technical replicate contract changed: {actual}")
print("\nv0.4.5 full repository manifest acceptance")
print("rows=16 single_neuron=9 non_single_whole_tissue=7 repository_scope=complete")
for row in rows:
    print(
        f"{row['raw_file']} -> source={row['source_name']} type={row['sample_type']} "
        f"label={row['label']} carrier={row['carrier_channel']} ref={row['design_ref']}"
    )
PY

"$BIN" sdrf-annotate \
  --accession PXD028040 \
  --snapshot "$SNAPSHOT" \
  --resolved-sdrf-dir "$RESOLVED_SDRF_DIR" \
  --annotations-dir "$ANNOTATIONS" \
  --publication-manifest "$PUB_MANIFEST" \
  --explicit-row-mapping-manifest "$ROW_MANIFEST" \
  --output "$OUT" \
  --model "$MODEL" \
  --max-evidence-items 128 \
  --max-evidence-chars 60000 \
  --max-files-in-prompt 48 \
  --force

python - "$OUT" <<'PY'
import csv, json, sys
from collections import Counter, defaultdict
from pathlib import Path
root = Path(sys.argv[1])
result_path = root / 'sdrf_annotation_results.tsv'
with result_path.open() as fh:
    results = list(csv.DictReader(fh, delimiter='\t'))
if len(results) != 1:
    raise SystemExit(f"ERROR: expected one result row, found {len(results)}")
r = results[0]
print("\nv0.4.5 PXD028040 full-repository reconstruction result")
print(json.dumps(r, indent=2))

sdrf_path = root / 'sdrf' / 'PXD028040.sdrf.tsv'
with sdrf_path.open() as fh:
    rows = list(csv.DictReader(fh, delimiter='\t'))
print(f"sdrf_rows={len(rows)}")
if len(rows) != 16:
    raise SystemExit(f"ERROR: expected 16 generated rows, found {len(rows)}")
if len({x['comment[data file]'].lower() for x in rows}) != 16:
    raise SystemExit('ERROR: generated SDRF does not have one unique row per repository RAW')
role_counts = Counter(x['characteristics[sample type]'] for x in rows)
print('sample_type_counts=', role_counts)
if role_counts != Counter({'single cell': 9, 'study sample': 7}):
    raise SystemExit(f"ERROR: generated row-role contract changed: {role_counts}")
for row in rows:
    print(
        f"{row['comment[data file]']} source={row['source name']} type={row['characteristics[sample type]']} "
        f"cell={row.get('characteristics[cell identifier]','-')} isolation={row.get('characteristics[single cell isolation protocol]','-')} "
        f"label={row['comment[label]']} carrier={row.get('comment[carrier channel]','-')} tech={row['comment[technical replicate]']}"
    )

review_path = root / 'review' / 'PXD028040.sdrf.review.tsv'
errors, warnings = [], []
if review_path.is_file():
    with review_path.open() as fh:
        for issue in csv.DictReader(fh, delimiter='\t'):
            (errors if issue['level'] == 'error' else warnings).append(issue)
error_codes = Counter(x['code'] for x in errors)
warning_codes = Counter(x['code'] for x in warnings)
print('error_codes=', error_codes)
print('warning_codes=', warning_codes)
for forbidden in [
    'sample_to_channel_mapping_unresolved',
    'data_file_not_in_pride_raw_inventory',
    'explicit_row_mapping_repository_scope_incomplete',
]:
    if error_codes.get(forbidden):
        raise SystemExit(f"ERROR: v0.4.5 retained forbidden blocker {forbidden}: {error_codes[forbidden]}")
other_errors = {k: v for k, v in error_codes.items() if k != 'single_cell_isolation_unresolved'}
if other_errors:
    raise SystemExit(f"ERROR: unexpected validation errors remain after full repository mapping: {other_errors}")
if error_codes.get('single_cell_isolation_unresolved') != 9:
    raise SystemExit(
        f"ERROR: expected exactly nine source-supported template-isolation-gap errors, observed {error_codes.get('single_cell_isolation_unresolved', 0)}"
    )

audit_path = root / 'audit' / 'PXD028040.sdrf.audit.json'
if audit_path.is_file():
    audit = json.loads(audit_path.read_text())
    print(
        'audit_explicit_mapping_rows=', audit.get('explicit_row_mapping_rows'),
        'generation_mode=', audit.get('generation_mode'),
        'completeness=', audit.get('completeness_status'),
        'locally_valid=', audit.get('locally_valid')
    )
    if audit.get('explicit_row_mapping_rows') != 16:
        raise SystemExit('ERROR: audit did not record all 16 explicit mapping rows')
    if audit.get('completeness_status') != 'incomplete_template_isolation_method_gap':
        raise SystemExit(
            f"ERROR: expected isolation-template-gap completeness after repository closure, got {audit.get('completeness_status')}"
        )
PY

printf '\nv0.4.5 full repository explicit mapping complete\n'
printf '  manifest: %s\n' "$ROW_MANIFEST"
printf '  results:  %s\n' "$OUT/sdrf_annotation_results.tsv"
printf '  draft:    %s\n' "$OUT/sdrf/PXD028040.sdrf.tsv"
printf '  review:   %s\n' "$OUT/review/PXD028040.sdrf.review.tsv"
printf '  audit:    %s\n' "$OUT/audit/PXD028040.sdrf.audit.json"
