#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-$PWD}"
SNAPSHOT="${SNAPSHOT:-$ROOT/data/snapshot}"
V042C="${V042C:-$ROOT/data/sdrf_mapping_role_recheck_gt105_pride_v042c}"
REPORTER_AUDIT="${REPORTER_AUDIT:-$V042C/audit/sdrf_mapping_evidence_audit.tsv}"
V046="${V046:-$ROOT/data/sdrf_multiplex_support_asset_audit_gt105_pride_v046}"
REUSE_SUPPORT_ROOT="${REUSE_SUPPORT_ROOT:-$V046/audit/support_files}"
OUT="${OUT:-$ROOT/data/sdrf_multiplex_support_asset_recheck_gt105_pride_v047}"

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
    raise SystemExit(
        f"ERROR: expected 8 residual multiplex-supported accessions after PXD028040 closure, "
        f"found {len(accessions)}: {accessions}"
    )
with open(out, "w") as fh:
    for accession in accessions:
        fh.write(accession + "\n")
print("remaining true-multiplex cohort:", ",".join(accessions))
PY

cat > "$OUT/run_manifest.json" <<JSON
{
  "phase": "GT105_PRIDE_SDRF_v047_high_specificity_multiplex_support_asset_recheck",
  "auditor_version": "pride-scp-sdrf-multiplex-support-asset-auditor-v0.2",
  "cohort_source": "$REPORTER_AUDIT",
  "v046_cache": "$REUSE_SUPPORT_ROOT",
  "architectural_changes": [
    "treat v0.4.6 as an accepted discovery checkpoint but reject result-table semantic noise as mapping evidence",
    "exclude search/result/protein/peptide/quantification assets unless an explicit design/metadata filename overrides that classification",
    "require TMT or reporter/channel/label context before 126-135-like integers are recognized as reporter channels",
    "reject carrier/run/control homonyms from protein-result vocabulary such as solute carrier, RUN-domain proteins, and cell-cycle control proteins",
    "require explicit RAW/file/run/sample/replicate syntax rather than the bare word run for run-linkage evidence",
    "separate source-triage chemistry from credible reporter-role/run/sample mapping evidence",
    "reuse bounded v0.4.6 downloaded support assets where possible and remain non-generative"
  ],
  "PXD028040_status": "mapping architecture complete; template-isolation-vocabulary exception",
  "gt_use": "accession cohort seed/evaluation only",
  "runtime_sdrf_gt_metadata_used": false,
  "runtime_sdrf_gt_labels_used": false,
  "non_generative": true
}
JSON
cat "$OUT/run_manifest.json"

ARGS=(
  --accessions-file "$OUT/remaining_multiplex_accessions.txt"
  --snapshot "$SNAPSHOT"
  --reporter-audit-tsv "$REPORTER_AUDIT"
  --output "$OUT/audit"
)
if [[ -d "$REUSE_SUPPORT_ROOT" ]]; then
  ARGS+=(--reuse-support-root "$REUSE_SUPPORT_ROOT")
fi
python "$ROOT/scripts/sdrf_multiplex_support_asset_audit.py" "${ARGS[@]}"

printf '\nv0.4.7 high-specificity support-asset recheck summary\n'
cat "$OUT/audit/sdrf_multiplex_support_asset_audit_summary.json"

printf '\nCompact high-specificity inventory\n'
python - "$OUT/audit/sdrf_multiplex_support_asset_audit.tsv" <<'PY'
import csv, sys
with open(sys.argv[1]) as fh:
    rows = list(csv.DictReader(fh, delimiter="\t"))
for r in rows:
    print(
        f"{r['accession']} class={r['support_class']} raw={r['raw_files']} "
        f"selected={r['support_candidates']} high={r['high_priority_support_candidates']} "
        f"medium={r['medium_priority_support_candidates']} low={r['low_priority_support_candidates']} "
        f"excluded_results={r['result_like_assets_excluded']} pub_text={r['publication_text_rows']} "
        f"credible={r['credible_evidence_hits']} role_hits={r['explicit_reporter_role_hits']} "
        f"role_run={r['reporter_role_and_run_hits']} run_hits={r['run_linkage_hits']}"
    )

print("\nHighest-priority reconstruction candidates")
for r in rows:
    if int(r['reporter_role_and_run_hits'] or 0) > 0:
        print(
            f"{r['accession']} class={r['support_class']} role_run={r['reporter_role_and_run_hits']} "
            f"role_hits={r['explicit_reporter_role_hits']} run_hits={r['run_linkage_hits']}"
        )

print("\nPartial support candidates requiring bounded source review")
for r in rows:
    if int(r['credible_evidence_hits'] or 0) > 0 and int(r['reporter_role_and_run_hits'] or 0) == 0:
        print(
            f"{r['accession']} class={r['support_class']} credible={r['credible_evidence_hits']} "
            f"role_hits={r['explicit_reporter_role_hits']} run_hits={r['run_linkage_hits']}"
        )

print("\nSource-recovery priority")
for r in rows:
    if int(r['credible_evidence_hits'] or 0) == 0:
        print(
            f"{r['accession']} class={r['support_class']} selected={r['support_candidates']} "
            f"pub_text={r['publication_text_rows']}"
        )
PY

printf '\nSelected support assets after result-table triage\n'
python - "$OUT/audit/support_asset_status.tsv" <<'PY'
import csv, collections, sys
with open(sys.argv[1]) as fh:
    rows = list(csv.DictReader(fh, delimiter="\t"))
by = collections.defaultdict(list)
for r in rows:
    if r['selected'].lower() == 'true':
        by[r['accession']].append(r)
for accession in sorted(by):
    print(accession)
    for r in sorted(by[accession], key=lambda x: ({'high':0,'medium':1,'low':2}.get(x['priority'],9), x['file_name'].lower()))[:20]:
        print(
            f"  priority={r['priority']} reason={r['triage_reason']} status={r['acquisition_status']} "
            f"credible={r['credible_evidence_hits']} file={r['file_name']}"
        )
    if len(by[accession]) > 20:
        print(f"  ... {len(by[accession]) - 20} additional selected assets")
PY

printf '\nCredible evidence preview (max 8/accession)\n'
python - "$OUT/audit/support_asset_credible_evidence_hits.tsv" <<'PY'
import csv, collections, sys
with open(sys.argv[1]) as fh:
    rows = list(csv.DictReader(fh, delimiter="\t"))
by = collections.defaultdict(list)
for r in rows:
    by[r['accession']].append(r)
for accession in sorted(by):
    print(accession)
    ordered = sorted(
        by[accession],
        key=lambda r: ({'reporter_role_and_run':0,'explicit_reporter_role':1,'single_cell_reporter_layout':2,'run_or_sample_linkage':3}.get(r['evidence_tier'],9), r['support_file'], r['location'])
    )
    for r in ordered[:8]:
        print(
            f"  {r['support_file']} {r['location']} tier={r['evidence_tier']} chem={r['chemistry']} "
            f"single={r['single_cell']} roles={r['roles'] or '-'} channels={r['reporter_channels'] or '-'} "
            f"run={r['run_semantics']} raw={r['raw_tokens'] or '-'}"
        )
        print("    " + r['text'][:600])
PY

printf '\nv0.4.7 high-specificity support-asset recheck complete (non-generative)\n'
printf '  summary:  %s\n' "$OUT/audit/sdrf_multiplex_support_asset_audit_summary.json"
printf '  inventory: %s\n' "$OUT/audit/sdrf_multiplex_support_asset_audit.tsv"
printf '  assets:    %s\n' "$OUT/audit/support_asset_status.tsv"
printf '  credible:  %s\n' "$OUT/audit/support_asset_credible_evidence_hits.tsv"
