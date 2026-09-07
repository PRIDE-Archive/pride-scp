#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-$PWD}"
V041="${V041:-$ROOT/data/sdrf_mapping_relation_recheck_gt105_pride_v041}"
PUB_MANIFEST="${PUB_MANIFEST:-$V041/publication_manifest_with_text.tsv}"
FULL_EVIDENCE="${FULL_EVIDENCE:-$ROOT/data/sdrf_annotation_gt105_pride_v031_full/evidence}"
V036_EVIDENCE="${V036_EVIDENCE:-$ROOT/data/sdrf_required_metadata_isolation_context_rescue_gt105_pride_v036/evidence}"
OUT="${OUT:-$ROOT/data/sdrf_mapping_role_recheck_gt105_pride_v042}"

if [[ ! -f "$PUB_MANIFEST" ]]; then
  echo "ERROR: v0.4.1 publication manifest not found: $PUB_MANIFEST" >&2
  exit 2
fi

mkdir -p "$OUT"
cat > "$OUT/multiplex_supported_accessions.txt" <<'ACCESSIONS'
PXD028040
PXD029320
PXD034370
PXD041328
PXD041399
PXD045500
PXD048347
PXD069039
PXD073405
ACCESSIONS

cat > "$OUT/run_manifest.json" <<JSON
{
  "phase": "GT105_PRIDE_SDRF_v042_real_corpus_reporter_role_recheck",
  "auditor_version": "pride-scp-sdrf-mapping-auditor-v0.3",
  "publication_manifest": "$PUB_MANIFEST",
  "architectural_changes": [
    "recognize analyte as an analytical/single-cell reporter role",
    "bind reporter roles locally to each reporter token instead of assigning broad-context fallback roles",
    "prefer following reporter-role phrases before preceding phrases so one reporter cannot inherit another reporter's role",
    "retain PDF line-break and reporter-token normalization",
    "remain non-generative until explicit reporter roles satisfy the narrow evidence contract"
  ],
  "gt_use": "accession cohort seed/evaluation only",
  "runtime_sdrf_gt_metadata_used": false,
  "runtime_sdrf_gt_labels_used": false
}
JSON
cat "$OUT/run_manifest.json"

python "$ROOT/scripts/sdrf_mapping_evidence_audit.py" \
  --accessions-file "$OUT/multiplex_supported_accessions.txt" \
  --evidence-dir "$V036_EVIDENCE" \
  --evidence-dir "$FULL_EVIDENCE" \
  --publication-manifest "$PUB_MANIFEST" \
  --output "$OUT/audit"

printf '\nv0.4.2 reporter-role recheck summary\n'
cat "$OUT/audit/sdrf_mapping_evidence_audit_summary.json"

printf '\nCompact reporter-role inventory\n'
python - "$OUT/audit/sdrf_mapping_evidence_audit.tsv" <<'PY'
import csv, sys
with open(sys.argv[1]) as fh:
    rows = list(csv.DictReader(fh, delimiter="\t"))
for r in rows:
    print(
        f"{r['accession']} class={r['mapping_class']} confidence={r['confidence']} "
        f"relation={r['relation_recheck']}/{r['relation_confidence']} chemistry={r['chemistry'] or '-'} "
        f"carrier={r['carrier_channels'] or '-'} reference={r['reference_channels'] or '-'} "
        f"single={r['single_cell_channels'] or '-'} ambiguous={r['ambiguous_channels'] or '-'} "
        f"pub_text={r['publication_text_rows']}"
    )
print("\nNarrow explicit-role contract candidates")
for r in rows:
    if r["mapping_class"] == "single_analytical_channel_per_run":
        print(
            f"{r['accession']} analytical={r['single_cell_channels']} "
            f"carrier={r['carrier_channels']} reference={r['reference_channels'] or '-'}"
        )
PY

printf '\nv0.4.2 reporter-role recheck complete (non-generative)\n'
printf '  summary:  %s\n' "$OUT/audit/sdrf_mapping_evidence_audit_summary.json"
printf '  inventory: %s\n' "$OUT/audit/sdrf_mapping_evidence_audit.tsv"
printf '  contexts:  %s\n' "$OUT/audit/contexts/"
