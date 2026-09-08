#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-$PWD}"
OUT="${OUT:-$ROOT/data/sdrf_generalized_evidence_graph_v053}"
SNAPSHOT="${SNAPSHOT:-$ROOT/data/snapshot}"
PUBLICATION_MANIFEST="${PUBLICATION_MANIFEST:-$ROOT/data/sdrf_residual_external_publication_recovery_gt105_pride_v050/publication_recovery/combined_publication_manifest.tsv}"
SUPPLEMENTARY_LINKS="${SUPPLEMENTARY_LINKS:-$ROOT/data/sdrf_residual_external_publication_recovery_gt105_pride_v050/publication_recovery/publication_supplementary_links.tsv}"
ACCESSIONS_FILE="${ACCESSIONS_FILE:-}"
V051_GRAPH="${V051_GRAPH:-$ROOT/data/sdrf_multiplex_evidence_graph_gt105_pride_v051/audit/multiplex_evidence_graph.tsv}"
REUSE_STRUCTURED_ROOT="${REUSE_STRUCTURED_ROOT:-$ROOT/data/sdrf_multiplex_support_asset_audit_gt105_pride_v046/audit/support_files}"
REUSE_EXTERNAL_ROOT="${REUSE_EXTERNAL_ROOT:-$ROOT/data/sdrf_multibranch_evidence_graph_gt105_pride_v052/audit/external_analysis}"
FETCH_EXTERNAL_ANALYSIS="${FETCH_EXTERNAL_ANALYSIS:-1}"
MAX_ARTIFACTS_PER_ACCESSION="${MAX_ARTIFACTS_PER_ACCESSION:-12}"
MAX_ARTIFACT_MB="${MAX_ARTIFACT_MB:-100}"
MAX_EXTERNAL_FILES="${MAX_EXTERNAL_FILES:-24}"
MAX_EXTERNAL_MB="${MAX_EXTERNAL_MB:-25}"
MAX_ARCHIVE_MB="${MAX_ARCHIVE_MB:-200}"
mkdir -p "$OUT"

[[ -d "$SNAPSHOT" ]] || { echo "ERROR: missing snapshot: $SNAPSHOT" >&2; exit 2; }
[[ -f "$PUBLICATION_MANIFEST" ]] || { echo "ERROR: missing publication manifest: $PUBLICATION_MANIFEST" >&2; exit 2; }

# The generalized runner accepts any accession file.  For continuity only, if none is supplied and a
# prior evidence-graph cohort exists, derive the benchmark accession list from its accession column.
# This changes only which records are exercised; it does not change scientific inference behavior.
if [[ -z "$ACCESSIONS_FILE" ]]; then
  [[ -f "$V051_GRAPH" ]] || { echo "ERROR: set ACCESSIONS_FILE to a newline-delimited accession list" >&2; exit 2; }
  ACCESSIONS_FILE="$OUT/accessions.txt"
  python - "$V051_GRAPH" "$ACCESSIONS_FILE" <<'PY'
import csv,sys
with open(sys.argv[1], errors='replace') as fh:
    vals=sorted({(r.get('accession') or '').strip().upper() for r in csv.DictReader(fh, delimiter='\t') if (r.get('accession') or '').strip()})
open(sys.argv[2],'w').write('\n'.join(vals)+'\n')
print(f'derived compatibility benchmark cohort from prior graph: {len(vals)} accessions')
PY
fi
[[ -f "$ACCESSIONS_FILE" ]] || { echo "ERROR: ACCESSIONS_FILE not found: $ACCESSIONS_FILE" >&2; exit 2; }

python "$ROOT/scripts/check_sdrf_generalization_guard.py"

cat > "$OUT/run_manifest.json" <<JSON
{
  "phase": "PRIDE_SDRF_v053_generalized_source_evidence_graph",
  "auditor_version": "pride-scp-sdrf-generalized-evidence-graph-v0.1",
  "architectural_changes": [
    "remove accession-specific scientific branch logic from the active multi-branch runtime",
    "discover one-or-more experimental branches from reusable source features and reporter contracts",
    "assign files by evidence-feature scoring rather than accession-specific filename rules",
    "resolve RAW/sample/channel joins by exact, normalized, or unique composite source keys without row-order assumptions",
    "scan publication-linked analysis repositories generically by high-value schema/path classes and bounded archives",
    "classify cross-accession predecessor/expanded-redeposit relationships from title identity, RAW containment and chronology",
    "enforce a static generalization guard that rejects literal PXD identifiers in production inference code"
  ],
  "accessions_file": "$ACCESSIONS_FILE",
  "fetch_external_analysis": $([[ "$FETCH_EXTERNAL_ANALYSIS" == "1" ]] && echo true || echo false),
  "gt_use": "none in runtime inference; benchmark cohort files only choose records to exercise",
  "runtime_sdrf_gt_metadata_used": false,
  "runtime_sdrf_gt_labels_used": false,
  "runtime_accession_specific_rules": false,
  "non_generative": true
}
JSON
cat "$OUT/run_manifest.json"

args=(
  --accessions-file "$ACCESSIONS_FILE"
  --snapshot "$SNAPSHOT"
  --publication-manifest "$PUBLICATION_MANIFEST"
  --output "$OUT/audit"
  --max-artifacts-per-accession "$MAX_ARTIFACTS_PER_ACCESSION"
  --max-artifact-bytes "$((MAX_ARTIFACT_MB * 1024 * 1024))"
  --max-external-files "$MAX_EXTERNAL_FILES"
  --max-external-bytes "$((MAX_EXTERNAL_MB * 1024 * 1024))"
  --max-archive-bytes "$((MAX_ARCHIVE_MB * 1024 * 1024))"
)
[[ -f "$SUPPLEMENTARY_LINKS" ]] && args+=(--supplementary-links "$SUPPLEMENTARY_LINKS")
[[ -d "$REUSE_STRUCTURED_ROOT" ]] && args+=(--reuse-root "$REUSE_STRUCTURED_ROOT")
[[ -d "$REUSE_EXTERNAL_ROOT" ]] && args+=(--reuse-external-root "$REUSE_EXTERNAL_ROOT")
[[ "$FETCH_EXTERNAL_ANALYSIS" == "1" ]] && args+=(--fetch-external-analysis)

python "$ROOT/scripts/sdrf_generalized_evidence_graph.py" "${args[@]}"

printf '\nv0.5.3 generalized evidence graph summary\n'
cat "$OUT/audit/generalized_evidence_graph_summary.json"

printf '\nAccession state\n'
python - "$OUT/audit/accession_summary.tsv" <<'PY'
import csv,sys
with open(sys.argv[1], errors='replace') as fh: rows=list(csv.DictReader(fh, delimiter='\t'))
for r in rows:
    print(f"{r['accession']} branches={r['branches']} modalities={r['modalities'] or '-'} raw={r['repository_raw_files']} assigned={r['assigned_raw_files']} unassigned={r['unassigned_raw_files']} candidates={r['explicit_row_mapping_candidate_branches']} status={r['accession_status']}")
PY

printf '\nBranch resolution\n'
python - "$OUT/audit/branch_resolution.tsv" <<'PY'
import csv,sys
with open(sys.argv[1], errors='replace') as fh: rows=list(csv.DictReader(fh, delimiter='\t'))
for r in rows:
    print(f"{r['accession']} {r['branch_id']} modality={r['modality']} chemistry={r['chemistry'] or '-'} acquisition={r['acquisition'] or '-'} member_raw={r['member_raw_files']} candidate_raw={r['candidate_raw_files']} joined={r['joined_member_raw_files']} status={r['resolved_status']}")
    if r.get('resolved_blocker'): print('  blocker:', r['resolved_blocker'])
PY

printf '\nCross-accession relation assessments\n'
if [[ -s "$OUT/audit/relation_assessments.tsv" ]]; then
  tail -n +2 "$OUT/audit/relation_assessments.tsv" | head -80 || true
fi

printf '\nGeneralized join evidence preview\n'
if [[ -s "$OUT/audit/join_evidence.tsv" ]]; then
  python - "$OUT/audit/join_evidence.tsv" <<'PY'
import csv,sys
with open(sys.argv[1], errors='replace') as fh: rows=list(csv.DictReader(fh, delimiter='\t'))
for r in rows[:40]:
    print(f"{r['accession']} raw={r['repository_raw']} join={r['join_method']}/{r['join_confidence']} channels={r['channels'] or '-'} samples={r['sample_tokens'] or '-'} source={r['source_location']}")
PY
fi

printf '\nv0.5.3 generalized SDRF evidence graph complete (non-generative)\n'
printf '  summary:      %s\n' "$OUT/audit/generalized_evidence_graph_summary.json"
printf '  branches:     %s\n' "$OUT/audit/branch_contracts.tsv"
printf '  membership:   %s\n' "$OUT/audit/branch_file_membership.tsv"
printf '  joins:        %s\n' "$OUT/audit/join_evidence.tsv"
printf '  relationships:%s\n' "$OUT/audit/relation_assessments.tsv"
