#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-$PWD}"
OUT="${OUT:-$ROOT/data/sdrf_multiplex_evidence_graph_gt105_pride_v051}"
V050="${V050:-$ROOT/data/sdrf_residual_external_publication_recovery_gt105_pride_v050}"
V042C="${V042C:-$ROOT/data/sdrf_mapping_role_recheck_gt105_pride_v042c}"
V046="${V046:-$ROOT/data/sdrf_multiplex_support_asset_audit_gt105_pride_v046}"
SNAPSHOT="${SNAPSHOT:-$ROOT/data/snapshot}"
FETCH_EXTERNAL_ANALYSIS="${FETCH_EXTERNAL_ANALYSIS:-1}"
MAX_ARTIFACT_MB="${MAX_ARTIFACT_MB:-100}"
MAX_ARTIFACTS_PER_ACCESSION="${MAX_ARTIFACTS_PER_ACCESSION:-12}"
MAX_EXTERNAL_FILES="${MAX_EXTERNAL_FILES:-12}"
MAX_EXTERNAL_MB="${MAX_EXTERNAL_MB:-25}"
mkdir -p "$OUT"

[[ -f "$V042C/audit/sdrf_mapping_evidence_audit.tsv" ]] || { echo "ERROR: missing accepted v0.4.2c reporter audit" >&2; exit 2; }
[[ -f "$V050/publication_recovery/combined_publication_manifest.tsv" ]] || { echo "ERROR: run v0.5.0 first; missing combined publication manifest" >&2; exit 2; }

# Derive the still-labelled multiplex cohort from accepted source-grounded relation evidence.
python - "$V042C/audit/sdrf_mapping_evidence_audit.tsv" "$OUT/evidence_graph_accessions.txt" <<'PY'
import csv,sys
with open(sys.argv[1], errors='replace') as fh: rows=list(csv.DictReader(fh, delimiter='\t'))
acc=sorted({(r.get('accession') or '').strip().upper() for r in rows
            if r.get('relation_recheck')=='multiplex_supported' and (r.get('accession') or '').strip().upper()!='PXD028040'})
open(sys.argv[2],'w').write('\n'.join(acc)+'\n')
print('v0.5.1 evidence-graph cohort:', ','.join(acc))
PY

cat > "$OUT/run_manifest.json" <<JSON
{
  "phase": "GT105_PRIDE_SDRF_v051_multiplex_evidence_graph_rearchitecture",
  "auditor_version": "pride-scp-sdrf-multiplex-evidence-graph-v0.1",
  "architectural_change": "replace token-local reporter inference with a multi-source evidence graph before any further SDRF generation",
  "evidence_layers": [
    "single-cell branch/modality contract",
    "global reporter-role contract using chemistry-aware channel set algebra",
    "structured repository analysis artifacts (.xlsx/.csv headers/.pdResult/.msf/.pdStudy/.sky)",
    "bounded quantitative reporter-channel summaries",
    "publication-linked analysis-code/data repositories",
    "cross-accession duplicate/supersession relation evidence"
  ],
  "mandatory_regressions": [
    "PXD041399: recover TMT6 single=126,127,128,129 blank=130 carrier=131 and its explicit TMTpro8 analytical set",
    "PXD041328/PXD048347: recover TMTpro18 carrier=126 blank=127C and all remaining 16 analytical channels",
    "PXD029320: recover carrier=126 and expected 14 analytical single cells even if exact analytical channel set remains open",
    "PXD045500: classify the single-cell branch as label-free/mixed-repository rather than reporter multiplexed",
    "PXD069039/PXD073405: surface same-study/duplicate-integrity relation for explicit review before generation"
  ],
  "result_artifact_policy": "result files are no longer searched as prose; they are parsed structurally by schema/header/SQLite/XML",
  "network_scope": "bounded acquisition of repository structured artifacts plus publication-linked GitHub analysis files when enabled",
  "fetch_external_analysis": $([[ "$FETCH_EXTERNAL_ANALYSIS" == "1" ]] && echo true || echo false),
  "gt_use": "none; cohort and all evidence are source-grounded",
  "runtime_sdrf_gt_metadata_used": false,
  "runtime_sdrf_gt_labels_used": false,
  "non_generative": true
}
JSON
cat "$OUT/run_manifest.json"

args=(
  --accessions-file "$OUT/evidence_graph_accessions.txt"
  --snapshot "$SNAPSHOT"
  --publication-manifest "$V050/publication_recovery/combined_publication_manifest.tsv"
  --output "$OUT/audit"
  --max-artifacts-per-accession "$MAX_ARTIFACTS_PER_ACCESSION"
  --max-artifact-bytes "$((MAX_ARTIFACT_MB * 1024 * 1024))"
  --max-external-files "$MAX_EXTERNAL_FILES"
  --max-external-bytes "$((MAX_EXTERNAL_MB * 1024 * 1024))"
)
[[ -f "$V050/publication_recovery/publication_recovery_candidates.tsv" ]] && args+=(--publication-candidates "$V050/publication_recovery/publication_recovery_candidates.tsv")
[[ -f "$V050/publication_recovery/publication_supplementary_links.tsv" ]] && args+=(--supplementary-links "$V050/publication_recovery/publication_supplementary_links.tsv")
[[ -d "$V046/audit/support_files" ]] && args+=(--reuse-root "$V046/audit/support_files")
[[ "$FETCH_EXTERNAL_ANALYSIS" == "1" ]] && args+=(--fetch-external-analysis)

python "$ROOT/scripts/sdrf_multiplex_evidence_graph.py" "${args[@]}"

printf '\nv0.5.1 evidence graph summary\n'
cat "$OUT/audit/multiplex_evidence_graph_summary.json"

printf '\nPer-accession graph state\n'
python - "$OUT/audit/multiplex_evidence_graph.tsv" <<'PY'
import csv,sys
with open(sys.argv[1], errors='replace') as fh: rows=list(csv.DictReader(fh, delimiter='\t'))
for r in rows:
    print(f"{r['accession']} branch={r['branch_modality']}/{r['branch_confidence']} "
          f"contracts={r['complete_design_contracts']}/{r['design_contracts']} "
          f"structured={r['structured_evidence_rows']} runmap={r['structured_run_mapping_rows']} "
          f"external={r['external_analysis_sources']} status={r['graph_status']}")
    print(f"  next: {r['next_blocker']}")
PY

printf '\nClosed / high-confidence reporter design contracts\n'
python - "$OUT/audit/design_contracts.tsv" <<'PY'
import csv,sys
with open(sys.argv[1], errors='replace') as fh: rows=list(csv.DictReader(fh, delimiter='\t'))
for r in rows:
    if r.get('confidence')=='high' or r.get('complete_global_layout')=='true':
        print(f"{r['accession']} {r['chemistry']} complete={r['complete_global_layout']} analytical={r['analytical_channels'] or '-'} carrier={r['carrier_channels'] or '-'} blank={r['blank_channels'] or '-'} reference={r['reference_channels'] or '-'} expected={r['expected_analytical_count'] or '-'} source={r['source_ref']}")
PY

printf '\nStructured artifact acquisitions with evidence\n'
python - "$OUT/audit/artifact_inventory.tsv" <<'PY'
import csv,sys
with open(sys.argv[1], errors='replace') as fh: rows=list(csv.DictReader(fh, delimiter='\t'))
for r in rows:
    if int(r.get('structural_hits') or 0)>0 or r.get('priority')=='5':
        print(f"{r['accession']} priority={r['priority']} file={r['file_name']} acquired={r['acquired']} status={r['acquire_status']} parser={r['parser'] or '-'} hits={r['structural_hits']}")
PY

printf '\nPublication-linked external analysis sources\n'
if [[ -s "$OUT/audit/external_analysis_sources.tsv" ]]; then
  tail -n +2 "$OUT/audit/external_analysis_sources.tsv" | head -80 || true
fi

printf '\nCross-accession relation candidates\n'
if [[ -s "$OUT/audit/relation_candidates.tsv" ]]; then
  tail -n +2 "$OUT/audit/relation_candidates.tsv" || true
fi

printf '\nv0.5.1 multiplex evidence-graph reconstruction audit complete (non-generative)\n'
printf '  graph:       %s\n' "$OUT/audit/multiplex_evidence_graph.tsv"
printf '  contracts:   %s\n' "$OUT/audit/design_contracts.tsv"
printf '  artifacts:   %s\n' "$OUT/audit/artifact_inventory.tsv"
printf '  structured:  %s\n' "$OUT/audit/structured_artifact_evidence.tsv"
printf '  external:    %s\n' "$OUT/audit/external_analysis_sources.tsv"
printf '  relations:   %s\n' "$OUT/audit/relation_candidates.tsv"
