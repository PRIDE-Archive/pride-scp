#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-$PWD}"
OUT="${OUT:-$ROOT/data/sdrf_local_publication_corpus_reconcile_gt105_pride_v049}"
MANUAL_PDF_DIR="${MANUAL_PDF_DIR:-$ROOT/manual_pdfs}"
SNAPSHOT="${SNAPSHOT:-$ROOT/data/snapshot}"
COHORT_FILE="${COHORT_FILE:-}"
V031="${V031:-$ROOT/data/sdrf_annotation_gt105_pride_v031_full}"
V042C="${V042C:-$ROOT/data/sdrf_mapping_role_recheck_gt105_pride_v042c}"
V048="${V048:-$ROOT/data/sdrf_multiplex_publication_accession_recovery_gt105_pride_v048}"
FULL_EVIDENCE="${FULL_EVIDENCE:-$V031/evidence}"
V036_EVIDENCE="${V036_EVIDENCE:-$ROOT/data/sdrf_required_metadata_isolation_context_rescue_gt105_pride_v036/evidence}"

mkdir -p "$OUT"

# Derive the already accepted 105-primary-PRIDE cohort from source-grounded SDRF checkpoint outputs.
# The wrapper does not reopen GT196/GT179 and never reads GT metadata/labels for publication selection.
if [[ -n "$COHORT_FILE" ]]; then
  [[ -f "$COHORT_FILE" ]] || { echo "ERROR: COHORT_FILE not found: $COHORT_FILE" >&2; exit 2; }
  cp "$COHORT_FILE" "$OUT/accessions.txt"
elif [[ -f "$V031/sdrf_annotation_results.tsv" ]]; then
  python - "$V031/sdrf_annotation_results.tsv" "$OUT/accessions.txt" <<'PY'
import csv,re,sys
src,dst=sys.argv[1:]
with open(src, errors='replace') as fh:
    rows=list(csv.DictReader(fh, delimiter='\t'))
acc=sorted({(r.get('accession') or '').strip().upper() for r in rows if re.fullmatch(r'PXD\d{6,}', (r.get('accession') or '').strip(), re.I)})
if len(acc) != 105:
    raise SystemExit(f'expected 105 accessions from v0.3.1 full results, observed {len(acc)}')
open(dst,'w').write('\n'.join(acc)+'\n')
print(f'cohort derived from v0.3.1 full results: {len(acc)} accessions')
PY
elif [[ -d "$V031/evidence" ]]; then
  python - "$V031/evidence" "$OUT/accessions.txt" <<'PY'
import re,sys
from pathlib import Path
root,dst=Path(sys.argv[1]),Path(sys.argv[2])
acc=set()
for p in root.rglob('*'):
    for m in re.findall(r'PXD\d{6,}', p.name, re.I): acc.add(m.upper())
acc=sorted(acc)
if len(acc) != 105:
    raise SystemExit(f'expected 105 accessions from v0.3.1 evidence filenames, observed {len(acc)}')
dst.write_text('\n'.join(acc)+'\n')
print(f'cohort derived from v0.3.1 evidence: {len(acc)} accessions')
PY
else
  echo "ERROR: cannot derive accepted 105-accession cohort. Set COHORT_FILE explicitly." >&2
  exit 2
fi

# Build explicit local source lists.  Missing paths are simply skipped and reported by the reconciler.
manifest_args=()
for f in \
  "$ROOT/work/python/pride_candidate_publications.tsv" \
  "$ROOT/work/python/pride_candidate_publications_with_pdfs.tsv" \
  "$ROOT/work/python/pride_candidate_publications_with_content.tsv" \
  "$ROOT/data/sdrf_mapping_relation_recheck_gt105_pride_v041/publication_manifest_with_text.tsv" \
  "$V048/publication_manifest_with_text.tsv"; do
  [[ -f "$f" ]] && manifest_args+=(--manifest "$f")
done

quarantine_args=()
for f in \
  "$V048/publication_manifest_quarantine.tsv" \
  "$ROOT/data/sdrf_local_publication_corpus_reconcile_gt105_pride_v049/local_publication_quarantine_manual.tsv"; do
  [[ -f "$f" ]] && quarantine_args+=(--quarantine-manifest "$f")
done

pdf_args=()
for d in \
  "$ROOT/work/python/publication_pdfs" \
  "$HOME/Documents/PRIDE_SCP/pride_scp_catalogue_pipeline/publication_pdfs" \
  "$HOME/Documents/PRIDE_SCP/pride_scp_catalogue_pipeline/pride_publication_pdfs"; do
  [[ -d "$d" ]] && pdf_args+=(--pdf-dir "$d")
done
if [[ -n "${LEGACY_PDF_DIRS:-}" ]]; then
  IFS=':' read -r -a legacy_dirs <<< "$LEGACY_PDF_DIRS"
  for d in "${legacy_dirs[@]}"; do [[ -d "$d" ]] && pdf_args+=(--pdf-dir "$d"); done
fi

content_args=()
for d in \
  "$ROOT/work/python/publication_content/text" \
  "$ROOT/data/sdrf_mapping_relation_recheck_gt105_pride_v041/publication_content/text" \
  "$V048/publication_content/text"; do
  [[ -d "$d" ]] && content_args+=(--content-text-dir "$d")
done
# Include other previously materialized SDRF publication-content directories without assuming one
# specific rescue iteration was the only useful local cache.
while IFS= read -r -d '' d; do
  already=0
  for existing in "${content_args[@]:-}"; do [[ "$existing" == "$d" ]] && already=1; done
  [[ $already -eq 0 ]] && content_args+=(--content-text-dir "$d")
done < <(find "$ROOT/data" -maxdepth 4 -type d -path '*/publication_content/text' -print0 2>/dev/null || true)

cat > "$OUT/run_manifest.json" <<JSON
{
  "phase": "GT105_PRIDE_SDRF_v049_local_first_publication_corpus_reconciliation",
  "reconciler_version": "pride-scp-sdrf-local-publication-corpus-reconciler-v0.1",
  "cohort_source": "$V031",
  "architectural_changes": [
    "make manual_pdfs and existing work/python publication caches explicit first-class SDRF evidence sources",
    "reconcile all 105 primary PRIDE accessions locally before any further PRIDE/Europe-PMC recovery",
    "apply accepted publication quarantine identifiers/content hashes before a manual or cached source can be selected",
    "prefer explicit manual mappings, accession-named manual PDFs, cached PDFs, cached normalized text, and only then metadata-only manifest rows",
    "extract text from selected local PDFs locally without invoking any network-capable publication resolver",
    "emit one auditable per-accession source decision plus all local publication candidates and an external-recovery-needed queue",
    "rerun the residual eight-accession reporter audit only from the reconciled local-first selected manifest"
  ],
  "source_precedence": [
    "manual_pdfs/manual_pdf_manifest.tsv",
    "manual_pdfs/PXDxxxxxx.pdf",
    "validated existing publication PDF caches",
    "validated existing normalized publication text",
    "trustworthy existing publication manifest metadata",
    "external recovery only after this phase and only for unresolved accessions"
  ],
  "network_used": false,
  "gt_use": "none in this iteration; cohort comes from the accepted v0.3.1 source-grounded 105-accession checkpoint",
  "runtime_sdrf_gt_metadata_used": false,
  "runtime_sdrf_gt_labels_used": false,
  "non_generative": true
}
JSON
cat "$OUT/run_manifest.json"

python "$ROOT/scripts/sdrf_local_publication_corpus_reconcile.py" \
  --accessions-file "$OUT/accessions.txt" \
  --snapshot-dir "$SNAPSHOT" \
  --manual-pdf-dir "$MANUAL_PDF_DIR" \
  "${manifest_args[@]}" \
  "${quarantine_args[@]}" \
  "${pdf_args[@]}" \
  "${content_args[@]}" \
  --output-dir "$OUT" \
  --expected-accessions 105

printf '\nv0.4.9 local-first publication corpus summary\n'
cat "$OUT/local_publication_corpus_summary.json"

printf '\nPer-accession local-first source decisions\n'
python - "$OUT/local_publication_source_inventory.tsv" <<'PY'
import csv,sys
with open(sys.argv[1]) as fh: rows=list(csv.DictReader(fh, delimiter='\t'))
for r in rows:
    print(
        f"{r['accession']} status={r['selected_status']} source={r['selected_source'] or '-'} "
        f"pdf={'yes' if r['selected_pdf_path'] else 'no'} text={'yes' if r['selected_text_path'] else 'no'} "
        f"quarantine={r['quarantined_candidates']} external={r['external_recovery_needed']} "
        f"doi={r['selected_publication_doi'] or '-'} title={r['selected_publication_title'] or '-'}"
    )
PY

printf '\nLocal publication coverage by selected source\n'
python - "$OUT/local_publication_source_inventory.tsv" <<'PY'
import csv,sys
from collections import Counter
with open(sys.argv[1]) as fh: rows=list(csv.DictReader(fh, delimiter='\t'))
print('status:', Counter(r['selected_status'] for r in rows))
print('source:', Counter((r['selected_source'] or 'none') for r in rows))
print('local_pdf:', sum(bool(r['selected_pdf_path']) for r in rows))
print('local_text:', sum(bool(r['selected_text_path']) for r in rows))
print('quarantined_accessions:', sum(int(r['quarantined_candidates'] or 0)>0 for r in rows))
print('external_recovery_needed:', sum(r['external_recovery_needed']=='true' for r in rows))
PY

# Re-audit only the remaining true-multiplex cohort from local-first selected publication evidence.
if [[ -f "$V042C/audit/sdrf_mapping_evidence_audit.tsv" ]]; then
  python - "$V042C/audit/sdrf_mapping_evidence_audit.tsv" "$OUT/residual_multiplex_accessions.txt" <<'PY'
import csv,sys
src,dst=sys.argv[1:]
with open(src) as fh: rows=list(csv.DictReader(fh, delimiter='\t'))
acc=sorted({r['accession'].strip().upper() for r in rows if r.get('relation_recheck')=='multiplex_supported' and r.get('accession','').strip().upper()!='PXD028040'})
if len(acc)!=8: print(f'WARNING: expected 8 residual multiplex accessions, observed {len(acc)}', file=sys.stderr)
open(dst,'w').write('\n'.join(acc)+'\n')
PY

  auditor_args=()
  [[ -d "$V036_EVIDENCE" ]] && auditor_args+=(--evidence-dir "$V036_EVIDENCE")
  [[ -d "$FULL_EVIDENCE" ]] && auditor_args+=(--evidence-dir "$FULL_EVIDENCE")
  python "$ROOT/scripts/sdrf_mapping_evidence_audit.py" \
    --accessions-file "$OUT/residual_multiplex_accessions.txt" \
    "${auditor_args[@]}" \
    --publication-manifest "$OUT/local_publication_manifest_selected.tsv" \
    --output "$OUT/residual_multiplex_audit"

  printf '\nResidual multiplex inventory from local-first corpus\n'
  python - "$OUT/residual_multiplex_audit/sdrf_mapping_evidence_audit.tsv" "$OUT/local_publication_source_inventory.tsv" <<'PY'
import csv,sys
with open(sys.argv[1]) as fh: audit=list(csv.DictReader(fh, delimiter='\t'))
with open(sys.argv[2]) as fh: inv={r['accession']:r for r in csv.DictReader(fh, delimiter='\t')}
for r in audit:
    i=inv.get(r['accession'],{})
    print(
        f"{r['accession']} local_source={i.get('selected_source') or '-'} text={'yes' if i.get('selected_text_path') else 'no'} "
        f"class={r['mapping_class']} confidence={r['confidence']} relation={r['relation_recheck']}/{r['relation_confidence']} "
        f"chemistry={r['chemistry'] or '-'} carrier={r['carrier_channels'] or '-'} reference={r['reference_channels'] or '-'} "
        f"single={r['single_cell_channels'] or '-'} blank={r['blank_channels'] or '-'} ambiguous={r['ambiguous_channels'] or '-'}"
    )
PY
fi

printf '\nv0.4.9 local-first publication reconciliation complete (network-free, non-generative)\n'
printf '  summary:    %s\n' "$OUT/local_publication_corpus_summary.json"
printf '  inventory:  %s\n' "$OUT/local_publication_source_inventory.tsv"
printf '  candidates: %s\n' "$OUT/local_publication_candidate_rows.tsv"
printf '  selected:   %s\n' "$OUT/local_publication_manifest_selected.tsv"
printf '  all local:  %s\n' "$OUT/local_publication_manifest_all.tsv"
printf '  quarantine: %s\n' "$OUT/local_publication_quarantine.tsv"
printf '  external:   %s\n' "$OUT/external_recovery_needed.txt"
