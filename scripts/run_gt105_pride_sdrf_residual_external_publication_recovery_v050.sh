#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-$PWD}"
OUT="${OUT:-$ROOT/data/sdrf_residual_external_publication_recovery_gt105_pride_v050}"
V049="${V049:-$ROOT/data/sdrf_local_publication_corpus_reconcile_gt105_pride_v049}"
V042C="${V042C:-$ROOT/data/sdrf_mapping_role_recheck_gt105_pride_v042c}"
V041="${V041:-$ROOT/data/sdrf_mapping_relation_recheck_gt105_pride_v041}"
V042A="${V042A:-$ROOT/data/sdrf_relation_scope_recheck_gt105_pride_v042}"
V036="${V036:-$ROOT/data/sdrf_required_metadata_isolation_context_rescue_gt105_pride_v036}"
V031="${V031:-$ROOT/data/sdrf_annotation_gt105_pride_v031_full}"
SNAPSHOT="${SNAPSHOT:-$ROOT/data/snapshot}"
FULL_EVIDENCE="${FULL_EVIDENCE:-$V031/evidence}"
V036_EVIDENCE="${V036_EVIDENCE:-$V036/evidence}"
mkdir -p "$OUT"

[[ -f "$V049/external_recovery_needed.txt" ]] || { echo "ERROR: run v0.4.9 first; missing $V049/external_recovery_needed.txt" >&2; exit 2; }
[[ -f "$V049/local_publication_manifest_selected.tsv" ]] || { echo "ERROR: missing v0.4.9 selected manifest" >&2; exit 2; }

# Build the source-sensitive residual union from accepted source-grounded outputs, then intersect it
# with v0.4.9's external-recovery queue.  This avoids web-searching already-valid accessions merely
# because their local publication corpus is incomplete.
python - "$V042C/audit/sdrf_mapping_evidence_audit.tsv" \
         "$V041/audit/sdrf_mapping_evidence_audit.tsv" \
         "$V042A/sdrf_annotation_results.tsv" \
         "$V036/sdrf_annotation_results.tsv" \
         "$V049/external_recovery_needed.txt" \
         "$OUT/source_sensitive_residual_union.txt" \
         "$OUT/active_external_recovery_queue.txt" \
         "$OUT/deferred_external_recovery.txt" <<'PY'
import csv,re,sys
from pathlib import Path
v042c,v041,v042a,v036,external,union_out,queue_out,deferred_out=map(Path,sys.argv[1:])
res=set()
def rows(p):
    if not p.is_file(): return []
    with p.open(errors='replace') as fh: return list(csv.DictReader(fh, delimiter='\t'))
for r in rows(v042c):
    a=(r.get('accession') or '').strip().upper()
    if r.get('relation_recheck')=='multiplex_supported' and a!='PXD028040': res.add(a)
for r in rows(v041):
    a=(r.get('accession') or '').strip().upper()
    if r.get('relation_recheck') in {'multiplex_unlinked_dataset_evidence','relation_source_recheck'}: res.add(a)
for p in (v042a,v036):
    for r in rows(p):
        a=(r.get('accession') or '').strip().upper()
        valid=(r.get('locally_valid') or r.get('valid') or '').strip().lower()
        status=(r.get('completeness_status') or r.get('status') or '').strip()
        if re.fullmatch(r'PXD\d{6,}',a) and valid in {'false','0','no'} and ('required_metadata' in status or 'incomplete' in status):
            res.add(a)
ext={x.strip().upper() for x in external.read_text().splitlines() if re.fullmatch(r'PXD\d{6,}',x.strip(),re.I)}
queue=sorted(res & ext)
deferred=sorted(ext - set(queue))
union_out.write_text('\n'.join(sorted(res))+'\n')
queue_out.write_text('\n'.join(queue)+('\n' if queue else ''))
deferred_out.write_text('\n'.join(deferred)+('\n' if deferred else ''))
print(f'source-sensitive residual union: {len(res)}')
print(f'active external recovery queue: {len(queue)} -> {",".join(queue) if queue else "-"}')
print(f'deferred external recovery (not current blocker): {len(deferred)}')
PY

QUEUE="$OUT/active_external_recovery_queue.txt"
[[ -s "$QUEUE" ]] || { echo "No active source-sensitive residuals require external publication recovery."; exit 0; }

quarantine_args=()
for f in \
  "$V049/local_publication_quarantine.tsv" \
  "$ROOT/data/sdrf_multiplex_publication_accession_recovery_gt105_pride_v048/publication_manifest_quarantine.tsv"; do
  [[ -f "$f" ]] && quarantine_args+=(--quarantine-manifest "$f")
done

cat > "$OUT/run_manifest.json" <<JSON
{
  "phase": "GT105_PRIDE_SDRF_v050_residual_external_publication_recovery",
  "recovery_version": "pride-scp-sdrf-residual-external-publication-recovery-v0.1",
  "local_first_source": "$V049/local_publication_manifest_selected.tsv",
  "architectural_changes": [
    "accept v0.4.9 as the authoritative local-first publication source inventory for all 105 primary PRIDE accessions",
    "intersect v0.4.9 external_recovery_needed with the currently source-sensitive residual SDRF lanes instead of searching all 38 publication-incomplete accessions",
    "recover by known DOI/PMID/title first, then exact PXD reverse lookup, with project-title search only as review fallback",
    "require verified accession/project identity or a non-quarantined known publication identifier before accepting external full text",
    "keep PXD069039 and any other quarantined publication identities blocked even when an erroneous paper contains the exact accession",
    "cache accepted Europe-PMC XML and normalized text immediately and inventory supplementary/external-data links for the next mapping-recovery iteration",
    "rerun only the residual multiplex and relation/source audit cohorts from the combined local-first plus accepted-external manifest",
    "remain non-generative"
  ],
  "network_scope": "only active source-sensitive residuals from the v0.4.9 external queue",
  "gt_use": "none; all cohorts are derived from accepted source-grounded SDRF outputs",
  "runtime_sdrf_gt_metadata_used": false,
  "runtime_sdrf_gt_labels_used": false,
  "non_generative": true
}
JSON
cat "$OUT/run_manifest.json"

python "$ROOT/scripts/sdrf_residual_external_publication_recovery.py" \
  --queue-file "$QUEUE" \
  --local-manifest "$V049/local_publication_manifest_selected.tsv" \
  --local-inventory "$V049/local_publication_source_inventory.tsv" \
  --snapshot-dir "$SNAPSHOT" \
  "${quarantine_args[@]}" \
  --output-dir "$OUT/publication_recovery"

printf '\nv0.5.0 residual external publication recovery summary\n'
cat "$OUT/publication_recovery/residual_external_publication_recovery_summary.json"

printf '\nRecovered publication content\n'
python - "$OUT/publication_recovery/recovered_publications.tsv" <<'PY'
import csv,sys
with open(sys.argv[1]) as fh: rows=list(csv.DictReader(fh, delimiter='\t'))
for r in rows:
    print(f"{r['accession']} doi={r['publication_doi'] or '-'} pmcid={r['publication_pmcid'] or '-'} "
          f"exact={r['external_recovery_exact_accession']} reason={r['external_recovery_reason']} title={r['publication_title']}")
if not rows: print('none')
PY

printf '\nStill unresolved after bounded external recovery\n'
cat "$OUT/publication_recovery/unresolved_after_external_recovery.txt" 2>/dev/null || true

# Residual multiplex audit: same accepted 8-member source-grounded cohort as v0.4.9.
if [[ -f "$V042C/audit/sdrf_mapping_evidence_audit.tsv" ]]; then
  python - "$V042C/audit/sdrf_mapping_evidence_audit.tsv" "$OUT/residual_multiplex_accessions.txt" <<'PY'
import csv,sys
with open(sys.argv[1]) as fh: rows=list(csv.DictReader(fh, delimiter='\t'))
acc=sorted({r['accession'].strip().upper() for r in rows if r.get('relation_recheck')=='multiplex_supported' and r.get('accession','').strip().upper()!='PXD028040'})
open(sys.argv[2],'w').write('\n'.join(acc)+'\n')
PY
  ea=(); [[ -d "$V036_EVIDENCE" ]] && ea+=(--evidence-dir "$V036_EVIDENCE"); [[ -d "$FULL_EVIDENCE" ]] && ea+=(--evidence-dir "$FULL_EVIDENCE")
  python "$ROOT/scripts/sdrf_mapping_evidence_audit.py" \
    --accessions-file "$OUT/residual_multiplex_accessions.txt" \
    "${ea[@]}" \
    --publication-manifest "$OUT/publication_recovery/combined_publication_manifest.tsv" \
    --output "$OUT/residual_multiplex_audit"
fi

# Re-audit the four v0.4.1 unlinked/relation-source cases with newly recovered publication text.
if [[ -f "$V041/audit/sdrf_mapping_evidence_audit.tsv" ]]; then
  python - "$V041/audit/sdrf_mapping_evidence_audit.tsv" "$OUT/relation_source_accessions.txt" <<'PY'
import csv,sys
with open(sys.argv[1]) as fh: rows=list(csv.DictReader(fh, delimiter='\t'))
acc=sorted({r['accession'].strip().upper() for r in rows if r.get('relation_recheck') in {'multiplex_unlinked_dataset_evidence','relation_source_recheck'}})
open(sys.argv[2],'w').write('\n'.join(acc)+'\n')
PY
  ea=(); [[ -d "$V036_EVIDENCE" ]] && ea+=(--evidence-dir "$V036_EVIDENCE"); [[ -d "$FULL_EVIDENCE" ]] && ea+=(--evidence-dir "$FULL_EVIDENCE")
  python "$ROOT/scripts/sdrf_mapping_evidence_audit.py" \
    --accessions-file "$OUT/relation_source_accessions.txt" \
    "${ea[@]}" \
    --publication-manifest "$OUT/publication_recovery/combined_publication_manifest.tsv" \
    --output "$OUT/relation_source_audit"
fi

printf '\nResidual multiplex inventory after bounded external recovery\n'
if [[ -f "$OUT/residual_multiplex_audit/sdrf_mapping_evidence_audit.tsv" ]]; then
python - "$OUT/residual_multiplex_audit/sdrf_mapping_evidence_audit.tsv" <<'PY'
import csv,sys
with open(sys.argv[1]) as fh: rows=list(csv.DictReader(fh, delimiter='\t'))
for r in rows:
 print(f"{r['accession']} pub_text={r.get('publication_text_available','') or r.get('publication_text','') or '-'} "
       f"class={r['mapping_class']} relation={r['relation_recheck']}/{r['relation_confidence']} chemistry={r['chemistry'] or '-'} "
       f"carrier={r['carrier_channels'] or '-'} reference={r['reference_channels'] or '-'} single={r['single_cell_channels'] or '-'} "
       f"blank={r['blank_channels'] or '-'} ambiguous={r['ambiguous_channels'] or '-'}")
PY
fi

printf '\nRelation/source recheck inventory after bounded external recovery\n'
if [[ -f "$OUT/relation_source_audit/sdrf_mapping_evidence_audit.tsv" ]]; then
python - "$OUT/relation_source_audit/sdrf_mapping_evidence_audit.tsv" <<'PY'
import csv,sys
with open(sys.argv[1]) as fh: rows=list(csv.DictReader(fh, delimiter='\t'))
for r in rows:
 print(f"{r['accession']} class={r['mapping_class']} relation={r['relation_recheck']}/{r['relation_confidence']} "
       f"chemistry={r['chemistry'] or '-'} linked_iso={r.get('single_cell_isobaric_contexts','-')} "
       f"linked_noniso={r.get('single_cell_nonisobaric_contexts','-')} dataset_iso={r.get('dataset_only_isobaric_contexts','-')}")
PY
fi

printf '\nExternal supplementary/data links from recovered residual publications\n'
if [[ -s "$OUT/publication_recovery/publication_supplementary_links.tsv" ]]; then
python - "$OUT/publication_recovery/publication_supplementary_links.tsv" <<'PY'
import csv,sys
with open(sys.argv[1]) as fh:
 rows=list(csv.DictReader(fh, delimiter='\t'))
for r in rows[:80]: print(f"{r['accession']} {r['link_type']} {r['link']} {r['context'][:180]}")
print(f'total_links={len(rows)}')
PY
else
  echo none
fi

printf '\nv0.5.0 residual external publication recovery complete (bounded, non-generative)\n'
printf '  queue:      %s\n' "$QUEUE"
printf '  summary:    %s\n' "$OUT/publication_recovery/residual_external_publication_recovery_summary.json"
printf '  recovered:  %s\n' "$OUT/publication_recovery/recovered_publications.tsv"
printf '  combined:   %s\n' "$OUT/publication_recovery/combined_publication_manifest.tsv"
printf '  supplement: %s\n' "$OUT/publication_recovery/publication_supplementary_links.tsv"
printf '  multiplex:  %s\n' "$OUT/residual_multiplex_audit/sdrf_mapping_evidence_audit.tsv"
printf '  relation:   %s\n' "$OUT/relation_source_audit/sdrf_mapping_evidence_audit.tsv"
