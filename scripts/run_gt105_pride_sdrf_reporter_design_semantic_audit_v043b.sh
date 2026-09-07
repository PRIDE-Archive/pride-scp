#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-$PWD}"
ACCESSION="${ACCESSION:-PXD028040}"
FILES_JSON="${FILES_JSON:-$ROOT/data/snapshot/files/${ACCESSION}.json}"
V042C="${V042C:-$ROOT/data/sdrf_mapping_role_recheck_gt105_pride_v042c}"
REPORTER_AUDIT_TSV="${REPORTER_AUDIT_TSV:-$V042C/audit/sdrf_mapping_evidence_audit.tsv}"
V043A="${V043A:-$ROOT/data/sdrf_reporter_run_scope_audit_gt105_pride_v043a}"
SUPPORT_DIR="${SUPPORT_DIR:-$V043A/audit/support_files}"
OUT="${OUT:-$ROOT/data/sdrf_reporter_design_semantic_audit_gt105_pride_v043b}"

if [[ ! -f "$FILES_JSON" ]]; then
  echo "ERROR: required repository file snapshot not found: $FILES_JSON" >&2
  exit 2
fi
if [[ ! -f "$REPORTER_AUDIT_TSV" ]]; then
  echo "ERROR: required v0.4.2c reporter audit not found: $REPORTER_AUDIT_TSV" >&2
  exit 2
fi
if [[ ! -d "$SUPPORT_DIR" ]]; then
  echo "ERROR: v0.4.3a support-file cache not found: $SUPPORT_DIR" >&2
  echo "Run ./scripts/run_gt105_pride_sdrf_reporter_run_scope_audit_v043a.sh first." >&2
  exit 2
fi

if [[ -n "${SUPPORT_XLSX:-}" ]]; then
  SUPPORT_XLSX_PATH="$SUPPORT_XLSX"
else
  mapfile -t XLSX_FILES < <(find "$SUPPORT_DIR" -maxdepth 1 -type f -iname '*.xlsx' -print | sort)
  if [[ "${#XLSX_FILES[@]}" -ne 1 ]]; then
    echo "ERROR: expected exactly one cached XLSX support file in $SUPPORT_DIR; found ${#XLSX_FILES[@]}" >&2
    printf '  %s\n' "${XLSX_FILES[@]:-}" >&2
    echo "Set SUPPORT_XLSX=/absolute/path/to/design.xlsx to override." >&2
    exit 2
  fi
  SUPPORT_XLSX_PATH="${XLSX_FILES[0]}"
fi
if [[ ! -f "$SUPPORT_XLSX_PATH" ]]; then
  echo "ERROR: support workbook not found: $SUPPORT_XLSX_PATH" >&2
  exit 2
fi

mkdir -p "$OUT"
cat > "$OUT/run_manifest.json" <<JSON
{
  "phase": "GT105_PRIDE_SDRF_v043b_reporter_design_semantic_audit",
  "auditor_version": "pride-scp-sdrf-design-semantic-auditor-v0.1",
  "accession": "$ACCESSION",
  "files_json": "$FILES_JSON",
  "support_xlsx": "$SUPPORT_XLSX_PATH",
  "reporter_audit_tsv": "$REPORTER_AUDIT_TSV",
  "architectural_changes": [
    "preserve workbook sheet/column/cell structure instead of flattening the deposited design rows",
    "test full RAW names first and then a bounded explicit acquisition-date + SC-code run key",
    "never allow TMT/single-neuron filename words alone to create a workbook linkage",
    "retain multiple date+SC design rows as ambiguous rather than choosing the nearest row",
    "emit adjacent workbook row context and explicit sample/TMT/reporter-role semantics for manual source review",
    "remain non-generative until biological sample and replicate semantics are explicit for the relevant RAW acquisitions"
  ],
  "gt_use": "accession cohort seed/evaluation only",
  "runtime_sdrf_gt_metadata_used": false,
  "runtime_sdrf_gt_labels_used": false,
  "non_generative": true
}
JSON
cat "$OUT/run_manifest.json"

python "$ROOT/scripts/sdrf_reporter_design_semantic_audit.py" \
  --accession "$ACCESSION" \
  --files-json "$FILES_JSON" \
  --support-xlsx "$SUPPORT_XLSX_PATH" \
  --reporter-audit-tsv "$REPORTER_AUDIT_TSV" \
  --output "$OUT/audit"

printf '\nv0.4.3b reporter design semantic audit summary\n'
cat "$OUT/audit/sdrf_reporter_design_semantic_audit_summary.json"

printf '\nWorkbook header candidates\n'
python - "$OUT/audit/header_candidates.json" <<'PY'
import json, sys
rows=json.load(open(sys.argv[1]))
for r in rows:
    print(f"sheet={r.get('sheet','-')} header_row={r.get('row_number')} score={r.get('score',0)}")
    print(f"  cells={json.dumps(r.get('cells',{}), ensure_ascii=False)}")
PY

printf '\nWorkbook semantic row inventory\n'
python - "$OUT/audit/design_rows_structured.tsv" <<'PY'
import csv, sys
rows=list(csv.DictReader(open(sys.argv[1]), delimiter='\t'))
for r in rows:
    semantic=(
        r['dates'] or r['sc_codes']
        or r['has_tmt'].lower()=='true'
        or r['has_single_branch'].lower()=='true'
        or r['has_sample_semantics'].lower()=='true'
        or r['has_128'].lower()=='true'
        or r['has_131'].lower()=='true'
    )
    if not semantic:
        continue
    flags=[]
    if r['has_tmt'].lower()=='true': flags.append('tmt')
    if r['has_single_branch'].lower()=='true': flags.append('single')
    if r['has_sample_semantics'].lower()=='true': flags.append('sample')
    if r['has_128'].lower()=='true': flags.append('128')
    if r['has_131'].lower()=='true': flags.append('131')
    if r['has_analyte_role'].lower()=='true': flags.append('analyte')
    if r['has_carrier_role'].lower()=='true': flags.append('carrier')
    print(
        f"{r['sheet']}:row{r['row_number']} dates={r['dates'] or '-'} sc={r['sc_codes'] or '-'} "
        f"flags={','.join(flags) or '-'} cells={r['cells_json']}"
    )
    print(f"  {r['row_text']}")
PY

printf '\nTMT + single-branch RAW structured design candidates\n'
python - "$OUT/audit/raw_design_candidates.tsv" <<'PY'
import csv, sys
rows=list(csv.DictReader(open(sys.argv[1]), delimiter='\t'))
selected=[r for r in rows if r['filename_tmt'].lower()=='true' and r['filename_single_branch'].lower()=='true']
for r in selected:
    semantics=[]
    for field,label in [
        ('explicit_row_tmt','row_tmt'),
        ('explicit_row_single_branch','row_single'),
        ('explicit_row_sample_semantics','row_sample'),
        ('explicit_row_128','row_128'),
        ('explicit_row_131','row_131'),
        ('explicit_row_analyte_role','row_analyte'),
        ('explicit_row_carrier_role','row_carrier'),
    ]:
        if r[field].lower()=='true': semantics.append(label)
    print(
        f"{r['raw_file']} date={r['raw_date'] or '-'} sc={r['raw_sc_code'] or '-'} "
        f"match={r['match_type']} confidence={r['confidence']} unique={r['unique']} "
        f"candidates={r['candidate_count']} refs={r['support_refs'] or '-'} "
        f"semantics={','.join(semantics) or '-'}"
    )
    if r['support_rows']:
        print(f"  rows={r['support_rows']}")
PY

printf '\nCandidate workbook row contexts\n'
python - "$OUT/audit/candidate_row_contexts.json" <<'PY'
import json, sys
obj=json.load(open(sys.argv[1]))
if not obj:
    print('none')
for raw, candidates in obj.items():
    if 'tmt' not in raw.lower() or 'single' not in raw.lower():
        continue
    print(raw)
    for item in candidates:
        c=item['candidate']
        print(f"  candidate {c['sheet']}:row{c['row_number']} cells={json.dumps(c['cells'], ensure_ascii=False)}")
        for ctx in item['context']:
            mark='*' if ctx['row_number']==c['row_number'] else ' '
            print(f"   {mark} {ctx['sheet']}:row{ctx['row_number']} {ctx['text']}")
PY

printf '\nv0.4.3b reporter design semantic audit complete (non-generative)\n'
printf '  summary:    %s\n' "$OUT/audit/sdrf_reporter_design_semantic_audit_summary.json"
printf '  rows:       %s\n' "$OUT/audit/design_rows_structured.tsv"
printf '  candidates: %s\n' "$OUT/audit/raw_design_candidates.tsv"
printf '  headers:    %s\n' "$OUT/audit/header_candidates.json"
printf '  contexts:   %s\n' "$OUT/audit/candidate_row_contexts.json"
