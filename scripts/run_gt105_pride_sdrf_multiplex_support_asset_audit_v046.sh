#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-$PWD}"
SNAPSHOT="${SNAPSHOT:-$ROOT/data/snapshot}"
V042C="${V042C:-$ROOT/data/sdrf_mapping_role_recheck_gt105_pride_v042c}"
REPORTER_AUDIT="${REPORTER_AUDIT:-$V042C/audit/sdrf_mapping_evidence_audit.tsv}"
OUT="${OUT:-$ROOT/data/sdrf_multiplex_support_asset_audit_gt105_pride_v046}"

if [[ ! -f "$REPORTER_AUDIT" ]]; then
  echo "ERROR: accepted v0.4.2c reporter audit not found: $REPORTER_AUDIT" >&2
  exit 2
fi
if [[ ! -d "$SNAPSHOT/files" ]]; then
  echo "ERROR: PRIDE repository file snapshot directory not found: $SNAPSHOT/files" >&2
  exit 2
fi

mkdir -p "$OUT"

python - "$REPORTER_AUDIT" "$OUT/remaining_multiplex_accessions.txt" <<'PY'
import csv, sys
src, out = sys.argv[1:]
with open(src) as fh:
    rows = list(csv.DictReader(fh, delimiter="\t"))
accessions = [
    r["accession"] for r in rows
    if r.get("relation_recheck") == "multiplex_supported" and r.get("accession") != "PXD028040"
]
if len(accessions) != 8:
    raise SystemExit(f"ERROR: expected 8 residual multiplex-supported accessions after PXD028040 closure, found {len(accessions)}: {accessions}")
with open(out, "w") as fh:
    for accession in accessions:
        fh.write(accession + "\n")
print("remaining true-multiplex cohort:", ",".join(accessions))
PY

cat > "$OUT/run_manifest.json" <<JSON
{
  "phase": "GT105_PRIDE_SDRF_v046_remaining_multiplex_support_asset_audit",
  "auditor_version": "pride-scp-sdrf-multiplex-support-asset-auditor-v0.1",
  "cohort_source": "$REPORTER_AUDIT",
  "architectural_changes": [
    "retire PXD028040 from the reporter-mapping lane after v0.4.5 completed all 16 repository RAW mappings",
    "derive the remaining multiplex-supported cohort from the accepted v0.4.2c source-grounded reporter audit rather than from GT metadata",
    "inventory and download only bounded public non-RAW design/metadata/readme/SDRF/tabular support assets for the remaining eight multiplex studies",
    "parse support assets non-generatively for explicit reporter roles, single-cell layout, run/file linkage, sample identifiers, and replicate semantics",
    "treat chemistry-only and filename-only signals as diagnostics and never as row-generation authorization"
  ],
  "PXD028040_status": "source-complete repository/sample/reporter reconstruction; template-isolation-vocabulary exception",
  "gt_use": "accession cohort seed/evaluation only",
  "runtime_sdrf_gt_metadata_used": false,
  "runtime_sdrf_gt_labels_used": false,
  "non_generative": true
}
JSON
cat "$OUT/run_manifest.json"

python "$ROOT/scripts/sdrf_multiplex_support_asset_audit.py" \
  --accessions-file "$OUT/remaining_multiplex_accessions.txt" \
  --snapshot "$SNAPSHOT" \
  --reporter-audit-tsv "$REPORTER_AUDIT" \
  --output "$OUT/audit"

printf '\nv0.4.6 remaining-multiplex support-asset audit summary\n'
cat "$OUT/audit/sdrf_multiplex_support_asset_audit_summary.json"

printf '\nCompact support-asset inventory\n'
python - "$OUT/audit/sdrf_multiplex_support_asset_audit.tsv" <<'PY'
import csv, sys
with open(sys.argv[1]) as fh:
    rows = list(csv.DictReader(fh, delimiter="\t"))
for r in rows:
    print(
        f"{r['accession']} class={r['support_class']} raw={r['raw_files']} "
        f"support={r['support_candidates']} acquired={r['support_files_acquired']} "
        f"pub_text={r['publication_text_rows']} hits={r['evidence_hits']} "
        f"role_hits={r['explicit_reporter_role_hits']} run_hits={r['run_linkage_hits']}"
    )

print("\nHighest-priority source-grounded support hits")
for r in rows:
    if int(r['explicit_reporter_role_hits'] or 0) or int(r['run_linkage_hits'] or 0):
        print(
            f"{r['accession']} class={r['support_class']} "
            f"role_hits={r['explicit_reporter_role_hits']} run_hits={r['run_linkage_hits']}"
        )

print("\nSource-recovery priority (no publication text and no acquired support evidence)")
for r in rows:
    if int(r['publication_text_rows'] or 0) == 0 and int(r['evidence_hits'] or 0) == 0:
        print(f"{r['accession']} support={r['support_candidates']} class={r['support_class']}")
PY

printf '\nEvidence-hit preview\n'
python - "$OUT/audit/support_asset_evidence_hits.tsv" <<'PY'
import csv, sys
with open(sys.argv[1]) as fh:
    rows = list(csv.DictReader(fh, delimiter="\t"))
for r in rows[:80]:
    print(
        f"{r['accession']} {r['support_file']} {r['location']} "
        f"chem={r['chemistry']} single={r['single_cell']} roles={r['roles'] or '-'} "
        f"channels={r['reporter_channels'] or '-'} run={r['run_semantics']} raw={r['raw_tokens'] or '-'}"
    )
    print("  " + r['text'][:500])
PY

printf '\nv0.4.6 support-asset audit complete (non-generative)\n'
printf '  summary: %s\n' "$OUT/audit/sdrf_multiplex_support_asset_audit_summary.json"
printf '  inventory: %s\n' "$OUT/audit/sdrf_multiplex_support_asset_audit.tsv"
printf '  assets: %s\n' "$OUT/audit/support_asset_status.tsv"
printf '  hits: %s\n' "$OUT/audit/support_asset_evidence_hits.tsv"
