#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-$PWD}"
OUT="${OUT:-$ROOT/data/sdrf_multibranch_evidence_graph_gt105_pride_v052}"
V051="${V051:-$ROOT/data/sdrf_multiplex_evidence_graph_gt105_pride_v051/audit}"
V050="${V050:-$ROOT/data/sdrf_residual_external_publication_recovery_gt105_pride_v050}"
SNAPSHOT="${SNAPSHOT:-$ROOT/data/snapshot}"
FETCH_EXTERNAL_ANALYSIS="${FETCH_EXTERNAL_ANALYSIS:-1}"
MAX_EXTERNAL_FILES="${MAX_EXTERNAL_FILES:-24}"
MAX_EXTERNAL_MB="${MAX_EXTERNAL_MB:-25}"
MAX_ARCHIVE_MB="${MAX_ARCHIVE_MB:-200}"
mkdir -p "$OUT"

[[ -f "$V051/multiplex_evidence_graph.tsv" ]] || { echo "ERROR: missing accepted v0.5.1 audit; run v0.5.1 first" >&2; exit 2; }
[[ -f "$V051/design_contracts.tsv" ]] || { echo "ERROR: missing v0.5.1 design contracts" >&2; exit 2; }
[[ -f "$V051/external_analysis_sources.tsv" ]] || { echo "ERROR: missing v0.5.1 external source inventory" >&2; exit 2; }
[[ -f "$V050/publication_recovery/combined_publication_manifest.tsv" ]] || { echo "ERROR: missing v0.5.0 combined publication manifest" >&2; exit 2; }

cat > "$OUT/run_manifest.json" <<JSON
{
  "phase": "GT105_PRIDE_SDRF_v052_multi_branch_substudy_evidence_graph",
  "auditor_version": "pride-scp-sdrf-multibranch-evidence-graph-v0.1",
  "architectural_change": "replace one-accession-one-modality with one-accession-to-one-or-more source-grounded sub-study branch contracts",
  "bounded_targets": [
    "PXD041399: split label-free DDA/DIA, TMT6 single-cell and TMTpro/TMT8 single-cell branches",
    "PXD041328/PXD048347: attach the closed TMTpro18 contract to accession-specific branches and mine the publication-linked CellenOne/input repository for RAW-cell-channel joins",
    "PXD069039/PXD073405: resolve same-study/predecessor-expanded-redeposit integrity and segment targeted H3 SureQuant versus comparator/method-development files"
  ],
  "generation_policy": "non-generative; branch contracts may become explicit-row mapping candidates only after branch-specific file/sample/channel closure",
  "fetch_external_analysis": $([[ "$FETCH_EXTERNAL_ANALYSIS" == "1" ]] && echo true || echo false),
  "gt_use": "none; inputs are accepted source-grounded publication/repository/evidence-graph outputs",
  "runtime_sdrf_gt_metadata_used": false,
  "runtime_sdrf_gt_labels_used": false
}
JSON
cat "$OUT/run_manifest.json"

args=(
  --snapshot "$SNAPSHOT"
  --v051-audit "$V051"
  --publication-manifest "$V050/publication_recovery/combined_publication_manifest.tsv"
  --output "$OUT/audit"
  --max-external-files "$MAX_EXTERNAL_FILES"
  --max-external-bytes "$((MAX_EXTERNAL_MB * 1024 * 1024))"
  --max-archive-bytes "$((MAX_ARCHIVE_MB * 1024 * 1024))"
)
[[ "$FETCH_EXTERNAL_ANALYSIS" == "1" ]] && args+=(--fetch-external-analysis)

python "$ROOT/scripts/sdrf_multibranch_evidence_graph.py" "${args[@]}"

printf '\nv0.5.2 multi-branch evidence graph summary\n'
cat "$OUT/audit/multibranch_evidence_graph_summary.json"

printf '\nAccession-level branch state\n'
python - "$OUT/audit/accession_branch_summary.tsv" <<'PY'
import csv,sys
with open(sys.argv[1], errors='replace') as fh: rows=list(csv.DictReader(fh, delimiter='\t'))
for r in rows:
    print(f"{r['accession']} branches={r['branches']} modalities={r['modalities']} raw={r['repository_raw_files']} assigned={r['assigned_raw_files']} unassigned={r['unassigned_raw_files']} status={r['accession_status']}")
PY

printf '\nBranch contracts\n'
python - "$OUT/audit/branch_summary.tsv" <<'PY'
import csv,sys
with open(sys.argv[1], errors='replace') as fh: rows=list(csv.DictReader(fh, delimiter='\t'))
for r in rows:
    print(f"{r['accession']} {r['branch_id']} modality={r['modality']} confidence={r['confidence']} eligibility={r['generation_eligibility']} member_raw={r['member_raw_files']} candidate_raw={r['candidate_raw_files']} analytical={r['analytical_channels'] or '-'} carrier={r['carrier_channels'] or '-'} blank={r['blank_channels'] or '-'}")
    if r.get('blocker'): print('  blocker:', r['blocker'])
PY

printf '\nPXD069039/PXD073405 accession-integrity result\n'
cat "$OUT/audit/accession_integrity.tsv"

printf '\nGastruloid external-analysis mapping status\n'
cat "$OUT/audit/external_mapping_status.tsv"

printf '\nDownloaded/reused external analysis files\n'
if [[ -s "$OUT/audit/external_analysis_fetch_inventory.tsv" ]]; then
  tail -n +2 "$OUT/audit/external_analysis_fetch_inventory.tsv" | head -80 || true
fi

printf '\nExternal mapping evidence preview\n'
if [[ -s "$OUT/audit/external_analysis_mapping_evidence.tsv" ]]; then
  python - "$OUT/audit/external_analysis_mapping_evidence.tsv" <<'PY'
import csv,sys
with open(sys.argv[1], errors='replace') as fh: rows=list(csv.DictReader(fh, delimiter='\t'))
for r in rows[:30]:
    print(f"{r['accession']} {r['repository_path']} member={r['archive_member'] or '-'} type={r['evidence_type']} raws={r['raw_tokens'] or '-'} channels={r['channels'] or '-'} cells={r['cell_types'] or '-'} rows={r['row_count']}")
PY
fi

printf '\nv0.5.2 multi-branch/sub-study audit complete (non-generative)\n'
printf '  summary:    %s\n' "$OUT/audit/multibranch_evidence_graph_summary.json"
printf '  branches:   %s\n' "$OUT/audit/branch_contracts.tsv"
printf '  membership: %s\n' "$OUT/audit/branch_file_membership.tsv"
printf '  external:   %s\n' "$OUT/audit/external_analysis_mapping_evidence.tsv"
printf '  integrity:  %s\n' "$OUT/audit/accession_integrity.tsv"
