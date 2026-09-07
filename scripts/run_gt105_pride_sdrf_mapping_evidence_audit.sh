#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-$PWD}"
TRIAGE="${TRIAGE:-$ROOT/data/sdrf_recovery_triage_gt105_pride_v0312/sdrf_recovery_triage.tsv}"
V036_RESULTS="${V036_RESULTS:-$ROOT/data/sdrf_required_metadata_isolation_context_rescue_gt105_pride_v036/sdrf_annotation_results.tsv}"
FULL_EVIDENCE="${FULL_EVIDENCE:-$ROOT/data/sdrf_annotation_gt105_pride_v031_full/evidence}"
V036_EVIDENCE="${V036_EVIDENCE:-$ROOT/data/sdrf_required_metadata_isolation_context_rescue_gt105_pride_v036/evidence}"
PUB_MANIFEST="${PUB_MANIFEST:-$ROOT/work/python/pride_candidate_publications_with_content.tsv}"
OUT="${OUT:-$ROOT/data/sdrf_mapping_evidence_audit_gt105_pride_v040}"
WORKERS="${WORKERS:-4}"

for f in "$TRIAGE" "$V036_RESULTS" "$PUB_MANIFEST"; do
  if [[ ! -f "$f" ]]; then
    echo "ERROR: required input not found: $f" >&2
    exit 2
  fi
done

mkdir -p "$OUT"

python - "$TRIAGE" "$V036_RESULTS" "$OUT/mapping_accessions.txt" <<'PY'
import csv, sys
triage, v036, dst = sys.argv[1:]
accessions = set()
with open(triage) as fh:
    for r in csv.DictReader(fh, delimiter="\t"):
        if r.get("lane") == "denovo_mapping":
            accessions.add(r["accession"])
with open(v036) as fh:
    for r in csv.DictReader(fh, delimiter="\t"):
        if r.get("completeness_status") == "incomplete_sample_to_file_or_channel_mapping":
            accessions.add(r["accession"])
accessions = sorted(accessions)
with open(dst, "w") as out:
    out.write("\n".join(accessions) + "\n")
print(f"mapping-evidence audit accessions={len(accessions)} -> {dst}")
for a in accessions:
    print("  ", a)
if len(accessions) != 17:
    print(f"WARNING: expected current mapping lane size 17, observed {len(accessions)}")
PY

cat > "$OUT/run_manifest.json" <<JSON
{
  "phase": "GT105_PRIDE_SDRF_v040_mapping_evidence_audit",
  "auditor_version": "pride-scp-sdrf-mapping-auditor-v0.1",
  "accepted_ready_before_mapping_audit": 68,
  "mapping_lane_expected": 17,
  "architectural_goal": "inventory explicit reporter-channel/sample-role evidence before deterministic multiplex SDRF row generation",
  "gt_use": "accession cohort seed/evaluation only",
  "runtime_sdrf_gt_metadata_used": false,
  "runtime_sdrf_gt_labels_used": false
}
JSON
cat "$OUT/run_manifest.json"

# Materialize text for the entire mapping lane using the Stage-03 PDF->text contract fixed
# in v0.3.5. This does not run Ollama and does not modify accepted SDRF outputs.
python "$ROOT/python/stages/03_resolve_publication_content.py" \
  "$PUB_MANIFEST" \
  --output "$OUT/publication_manifest_with_text.tsv" \
  --content-dir "$OUT/publication_content" \
  --accessions-file "$OUT/mapping_accessions.txt" \
  --workers "$WORKERS"

python "$ROOT/scripts/sdrf_mapping_evidence_audit.py" \
  --accessions-file "$OUT/mapping_accessions.txt" \
  --evidence-dir "$V036_EVIDENCE" \
  --evidence-dir "$FULL_EVIDENCE" \
  --publication-manifest "$OUT/publication_manifest_with_text.tsv" \
  --output "$OUT/audit"

printf '\nMapping-evidence audit summary\n'
cat "$OUT/audit/sdrf_mapping_evidence_audit_summary.json"

printf '\nCompact mapping inventory\n'
python - "$OUT/audit/sdrf_mapping_evidence_audit.tsv" <<'PY'
import csv, sys
with open(sys.argv[1]) as fh:
    rows = list(csv.DictReader(fh, delimiter="\t"))
for r in rows:
    print(
        f"{r['accession']} class={r['mapping_class']} confidence={r['confidence']} "
        f"chemistry={r['chemistry'] or '-'} carrier={r['carrier_channels'] or '-'} "
        f"reference={r['reference_channels'] or '-'} single={r['single_cell_channels'] or '-'} "
        f"blank={r['blank_channels'] or '-'} ambiguous={r['ambiguous_channels'] or '-'} "
        f"raw={r['raw_file_count']} pub_text={r['publication_text_rows']}"
    )
PY

printf '\nv0.4.0 mapping-evidence audit complete\n'
printf '  accessions: %s\n' "$OUT/mapping_accessions.txt"
printf '  publication manifest: %s\n' "$OUT/publication_manifest_with_text.tsv"
printf '  summary: %s\n' "$OUT/audit/sdrf_mapping_evidence_audit_summary.json"
printf '  inventory: %s\n' "$OUT/audit/sdrf_mapping_evidence_audit.tsv"
printf '  contexts: %s\n' "$OUT/audit/contexts/"
