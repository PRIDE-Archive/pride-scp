#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-$PWD}"
V041="${V041:-$ROOT/data/sdrf_mapping_relation_recheck_gt105_pride_v041}"
PUB_MANIFEST="${PUB_MANIFEST:-$V041/publication_manifest_with_text.tsv}"
FULL_EVIDENCE="${FULL_EVIDENCE:-$ROOT/data/sdrf_annotation_gt105_pride_v031_full/evidence}"
V036_EVIDENCE="${V036_EVIDENCE:-$ROOT/data/sdrf_required_metadata_isolation_context_rescue_gt105_pride_v036/evidence}"
OUT="${OUT:-$ROOT/data/sdrf_mapping_role_recheck_gt105_pride_v042c}"

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
  "phase": "GT105_PRIDE_SDRF_v042c_pdf_layout_reporter_role_recheck",
  "auditor_version": "pride-scp-sdrf-mapping-auditor-v0.4",
  "publication_manifest": "$PUB_MANIFEST",
  "architectural_changes": [
    "treat PDF hard line breaks as soft whitespace for bounded reporter-role binding",
    "retain period/semicolon boundaries and neighboring reporter tokens as hard role-assignment limits",
    "recover role phrases separated from their reporter token by short two-column PDF text interleaving",
    "preserve the v0.4.2b rule that unbound reporters stay unresolved rather than inheriting broad-context roles",
    "remain non-generative until the real local corpus satisfies the narrow explicit reporter-role contract"
  ],
  "mandatory_regression": "PXD028040 must resolve analytical/single-cell=128 and carrier=131 with no ambiguity",
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

printf '\nv0.4.2c PDF-layout reporter-role recheck summary\n'
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
        f"single={r['single_cell_channels'] or '-'} blank={r['blank_channels'] or '-'} "
        f"ambiguous={r['ambiguous_channels'] or '-'} pub_text={r['publication_text_rows']}"
    )

print("\nMandatory PXD028040 regression")
row = next((r for r in rows if r["accession"] == "PXD028040"), None)
if row is None:
    print("PXD028040 MISSING")
else:
    ok = (
        row["single_cell_channels"] == "128"
        and row["carrier_channels"] == "131"
        and not row["ambiguous_channels"]
    )
    print(
        f"PXD028040 analytical={row['single_cell_channels'] or '-'} "
        f"carrier={row['carrier_channels'] or '-'} "
        f"ambiguous={row['ambiguous_channels'] or '-'} "
        f"class={row['mapping_class']} confidence={row['confidence']} "
        f"mandatory_regression={'PASS' if ok else 'FAIL'}"
    )

print("\nNarrow explicit-role contract candidates")
for r in rows:
    if r["mapping_class"] == "single_analytical_channel_per_run":
        print(
            f"{r['accession']} analytical={r['single_cell_channels']} "
            f"carrier={r['carrier_channels']} reference={r['reference_channels'] or '-'}"
        )
PY

printf '\nv0.4.2c reporter-role recheck complete (non-generative)\n'
printf '  summary:  %s\n' "$OUT/audit/sdrf_mapping_evidence_audit_summary.json"
printf '  inventory: %s\n' "$OUT/audit/sdrf_mapping_evidence_audit.tsv"
printf '  contexts:  %s\n' "$OUT/audit/contexts/"
