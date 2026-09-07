#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-$PWD}"
ACCESSION="${ACCESSION:-PXD028040}"
FILES_JSON="${FILES_JSON:-$ROOT/data/snapshot/files/${ACCESSION}.json}"
V041="${V041:-$ROOT/data/sdrf_mapping_relation_recheck_gt105_pride_v041}"
PUB_MANIFEST="${PUB_MANIFEST:-$V041/publication_manifest_with_text.tsv}"
V042C="${V042C:-$ROOT/data/sdrf_mapping_role_recheck_gt105_pride_v042c}"
REPORTER_AUDIT_TSV="${REPORTER_AUDIT_TSV:-$V042C/audit/sdrf_mapping_evidence_audit.tsv}"
OUT="${OUT:-$ROOT/data/sdrf_reporter_run_scope_audit_gt105_pride_v043a}"

for required in "$FILES_JSON" "$PUB_MANIFEST" "$REPORTER_AUDIT_TSV"; do
  if [[ ! -f "$required" ]]; then
    echo "ERROR: required input not found: $required" >&2
    exit 2
  fi
done

mkdir -p "$OUT"
cat > "$OUT/run_manifest.json" <<JSON
{
  "phase": "GT105_PRIDE_SDRF_v043a_reporter_run_scope_audit",
  "auditor_version": "pride-scp-sdrf-run-scope-auditor-v0.1",
  "accession": "$ACCESSION",
  "files_json": "$FILES_JSON",
  "publication_manifest": "$PUB_MANIFEST",
  "reporter_audit_tsv": "$REPORTER_AUDIT_TSV",
  "architectural_changes": [
    "keep reporter-row generation disabled after v0.4.2c",
    "inspect repository experimental-design/metadata support files before assigning the 128 analytical + 131 carrier layout to deposited RAW files",
    "download only small explicitly linked public design/metadata files and parse XLSX with the Python standard library",
    "cross-reference exact RAW basenames/stems against deposited support-table rows",
    "require manual/source-grounded review of biological sample and replicate semantics even when exact file linkage is present"
  ],
  "mandatory_reporter_contract": "PXD028040 single_analytical_channel_per_run/high; analytical=128; carrier=131; ambiguous=none",
  "gt_use": "accession cohort seed/evaluation only",
  "runtime_sdrf_gt_metadata_used": false,
  "runtime_sdrf_gt_labels_used": false,
  "non_generative": true
}
JSON
cat "$OUT/run_manifest.json"

python "$ROOT/scripts/sdrf_reporter_run_scope_audit.py" \
  --accession "$ACCESSION" \
  --files-json "$FILES_JSON" \
  --publication-manifest "$PUB_MANIFEST" \
  --reporter-audit-tsv "$REPORTER_AUDIT_TSV" \
  --output "$OUT/audit"

printf '\nv0.4.3a reporter run/file-scope audit summary\n'
cat "$OUT/audit/sdrf_reporter_run_scope_audit_summary.json"

printf '\nDeposited support-file acquisition status\n'
python - "$OUT/audit/support_file_status.json" <<'PY'
import json, sys
rows = json.load(open(sys.argv[1]))
if not rows:
    print("none")
for r in rows:
    print(
        f"{r.get('file','-')} status={r.get('status','-')} "
        f"rows={r.get('rows_parsed','0')} uri={r.get('uri','-')}"
    )
PY

printf '\nRAW/support-table matches\n'
python - "$OUT/audit/raw_support_matches.tsv" <<'PY'
import csv, sys
rows = list(csv.DictReader(open(sys.argv[1]), delimiter="\t"))
for r in rows:
    tags=[]
    if r['filename_tmt'].lower() == 'true': tags.append('filename_tmt')
    if r['filename_single_branch'].lower() == 'true': tags.append('filename_single_branch')
    print(
        f"{r['raw_file']} matches={r['support_match_count']} "
        f"flags={','.join(tags) or '-'} refs={r['support_refs'] or '-'}"
    )
PY

printf '\nPublication scope contexts\n'
python - "$OUT/audit/publication_scope_contexts.json" <<'PY'
import json, sys
rows = json.load(open(sys.argv[1]))
print(f"count={len(rows)}")
for i, r in enumerate(rows, 1):
    text = ' '.join(str(r.get('text','')).split())
    if len(text) > 500:
        text = text[:497] + '...'
    print(f"[{i}] scope={','.join(r.get('scope_terms', [])) or '-'} source={r.get('source','-')}")
    print(f"    {text}")
PY

printf '\nCandidate support rows containing a TMT + single-branch RAW filename\n'
python - "$OUT/audit/raw_support_matches.tsv" <<'PY'
import csv, sys
rows = list(csv.DictReader(open(sys.argv[1]), delimiter="\t"))
found = 0
for r in rows:
    if (
        r['filename_tmt'].lower() == 'true'
        and r['filename_single_branch'].lower() == 'true'
        and int(r['support_match_count'] or 0) > 0
    ):
        found += 1
        print(f"{r['raw_file']} -> {r['support_refs']}")
        print(f"    {r['support_row_texts']}")
if not found:
    print("none")
PY

printf '\nv0.4.3a run/file-scope audit complete (non-generative)\n'
printf '  summary:        %s\n' "$OUT/audit/sdrf_reporter_run_scope_audit_summary.json"
printf '  support status: %s\n' "$OUT/audit/support_file_status.json"
printf '  support rows:   %s\n' "$OUT/audit/support_rows.tsv"
printf '  RAW matches:    %s\n' "$OUT/audit/raw_support_matches.tsv"
printf '  pub contexts:   %s\n' "$OUT/audit/publication_scope_contexts.json"
