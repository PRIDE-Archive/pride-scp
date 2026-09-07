#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-$PWD}"
V042C="${V042C:-$ROOT/data/sdrf_mapping_role_recheck_gt105_pride_v042c}"
COHORT_TSV="${COHORT_TSV:-$V042C/audit/sdrf_mapping_evidence_audit.tsv}"
BASE_MANIFEST="${BASE_MANIFEST:-$ROOT/data/sdrf_mapping_relation_recheck_gt105_pride_v041/publication_manifest_with_text.tsv}"
FULL_EVIDENCE="${FULL_EVIDENCE:-$ROOT/data/sdrf_annotation_gt105_pride_v031_full/evidence}"
V036_EVIDENCE="${V036_EVIDENCE:-$ROOT/data/sdrf_required_metadata_isolation_context_rescue_gt105_pride_v036/evidence}"
OUT="${OUT:-$ROOT/data/sdrf_multiplex_publication_accession_recovery_gt105_pride_v048}"
WORKERS="${WORKERS:-4}"
TIMEOUT="${TIMEOUT:-45}"

for f in "$COHORT_TSV" "$BASE_MANIFEST"; do
  if [[ ! -f "$f" ]]; then
    echo "ERROR: required input not found: $f" >&2
    exit 2
  fi
done
mkdir -p "$OUT"

# Derive the residual multiplex cohort from the accepted source-grounded v0.4.2c audit.  PXD028040
# is deliberately retired because v0.4.5 completed its repository/sample/reporter mapping.
python - "$COHORT_TSV" "$OUT/accessions.txt" <<'PY'
import csv, sys
src, dst = sys.argv[1:]
with open(src) as fh:
    rows = list(csv.DictReader(fh, delimiter='\t'))
acc = sorted({
    r['accession'].strip().upper()
    for r in rows
    if r.get('relation_recheck') == 'multiplex_supported'
    and r.get('accession','').strip().upper() != 'PXD028040'
})
if len(acc) != 8:
    print(f"WARNING: expected 8 residual multiplex accessions; observed {len(acc)}", file=sys.stderr)
with open(dst, 'w') as out:
    out.write('\n'.join(acc) + '\n')
print('remaining true-multiplex cohort:', ','.join(acc))
PY

cat > "$OUT/run_manifest.json" <<JSON
{
  "phase": "GT105_PRIDE_SDRF_v048_publication_accession_reverse_recovery",
  "recovery_version": "pride-scp-sdrf-publication-accession-recovery-v0.1",
  "mapping_auditor_version": "pride-scp-sdrf-mapping-auditor-v0.4",
  "cohort_source": "$COHORT_TSV",
  "architectural_changes": [
    "accept v0.4.7 as evidence that no high-value repository design/support assets remain for the eight residual multiplex studies",
    "refresh current PRIDE publication metadata for the residual multiplex cohort",
    "query Europe PMC by exact PXD accession to recover publications that are missing or stale in PRIDE metadata",
    "require the exact accession in PMC full text plus project-title compatibility or a matching current PRIDE DOI/PMID before accepting a recovered publication",
    "quarantine current PRIDE publication associations that are directly contradicted by an exact-accession full-text candidate with strong project-title mismatch",
    "materialize accepted publication full text through the existing Stage-03 contract and rerun the non-generative reporter-role auditor",
    "remain non-generative; recovered publications are evidence sources only and do not authorize reporter rows by themselves"
  ],
  "gt_use": "accession cohort seed/evaluation only",
  "runtime_sdrf_gt_metadata_used": false,
  "runtime_sdrf_gt_labels_used": false,
  "non_generative": true
}
JSON
cat "$OUT/run_manifest.json"

# Refresh current PRIDE metadata first.  This is repository evidence, not GT truth.
python "$ROOT/python/stages/01_fetch_pride_publications.py" \
  --accessions-file "$OUT/accessions.txt" \
  --output "$OUT/pride_publications_refreshed.tsv" \
  --cache-dir "$OUT/pride_publication_cache" \
  --workers "$WORKERS"

# Reverse-resolve publications from exact accession mentions in Europe PMC full text, with a
# project-compatibility gate that prevents an erroneous/corrected accession citation from becoming
# SDRF evidence solely because the string is present in an article.
python "$ROOT/scripts/sdrf_publication_accession_recovery.py" \
  --accessions-file "$OUT/accessions.txt" \
  --pride-publications "$OUT/pride_publications_refreshed.tsv" \
  --output-dir "$OUT/publication_recovery" \
  --timeout "$TIMEOUT"

# Merge current PRIDE, accepted reverse-recovered publications, and useful historical local-PDF
# rows.  A current PRIDE publication identifier is quarantined only when the reverse audit has
# verified that exact accession in that article and classified it as a strong project-title mismatch.
python - \
  "$BASE_MANIFEST" \
  "$OUT/pride_publications_refreshed.tsv" \
  "$OUT/publication_recovery/recovered_publications.tsv" \
  "$OUT/publication_recovery/publication_accession_recovery_candidates.tsv" \
  "$OUT/accessions.txt" \
  "$OUT/publication_manifest_merged.tsv" \
  "$OUT/publication_manifest_quarantine.tsv" <<'PY'
import csv, sys
from pathlib import Path
base_path, fresh_path, recovered_path, candidates_path, acc_path, out_path, quarantine_path = map(Path, sys.argv[1:])
wanted = {x.strip().upper() for x in acc_path.read_text().splitlines() if x.strip()}

def read(path):
    with path.open(errors='replace') as fh:
        return list(csv.DictReader(fh, delimiter='\t'))

def norm(v):
    return (v or '').strip()

def doi(v):
    v = norm(v).lower()
    v = v.removeprefix('https://doi.org/').removeprefix('http://doi.org/').removeprefix('doi:')
    return v.strip()

def key(row):
    acc = norm(row.get('accession')).upper()
    d = doi(row.get('publication_doi') or row.get('resolved_doi'))
    p = norm(row.get('publication_pmid') or row.get('resolved_pmid')).lower()
    t = ' '.join(norm(row.get('publication_title')).lower().split())
    if d: return (acc, 'doi', d)
    if p: return (acc, 'pmid', p)
    if t: return (acc, 'title', t)
    return (acc, 'row', norm(row.get('publication_status')) or 'none')

def usable_pdf(row):
    status = norm(row.get('pdf_status')).lower()
    p = norm(row.get('pdf_path'))
    return status in {'downloaded','already_exists'} and bool(p) and Path(p).is_file()

def usable_text(row):
    p = norm(row.get('publication_content_text_path'))
    return bool(p) and Path(p).is_file()

base = [r for r in read(base_path) if norm(r.get('accession')).upper() in wanted]
fresh = [r for r in read(fresh_path) if norm(r.get('accession')).upper() in wanted]
recovered = [r for r in read(recovered_path) if norm(r.get('accession')).upper() in wanted]
candidates = [r for r in read(candidates_path) if norm(r.get('accession')).upper() in wanted]

# Identifiers proven to be an exact-accession mention in a semantically mismatched article.
reject_ids = set()
for r in candidates:
    if norm(r.get('recovery_status')) != 'rejected':
        continue
    if norm(r.get('exact_accession_in_fulltext')).lower() != 'true':
        continue
    if 'project_title_mismatch' not in norm(r.get('recovery_reason')):
        continue
    acc = norm(r.get('accession')).upper()
    d = doi(r.get('candidate_doi'))
    p = norm(r.get('candidate_pmid')).lower()
    if d: reject_ids.add((acc, 'doi', d))
    if p: reject_ids.add((acc, 'pmid', p))

quarantine=[]
fresh_kept=[]
for r in fresh:
    acc = norm(r.get('accession')).upper()
    d = doi(r.get('publication_doi'))
    p = norm(r.get('publication_pmid')).lower()
    rejected = (d and (acc,'doi',d) in reject_ids) or (p and (acc,'pmid',p) in reject_ids)
    if rejected:
        rr=dict(r); rr['publication_quarantine_reason']='exact_accession_but_project_title_mismatch'
        quarantine.append(rr)
    else:
        fresh_kept.append(r)
fresh=fresh_kept

all_fields=[]
for r in base+fresh+recovered+quarantine:
    for k in r:
        if k not in all_fields: all_fields.append(k)
if 'publication_quarantine_reason' not in all_fields:
    all_fields.append('publication_quarantine_reason')

chosen={}
# Current PRIDE is the repository baseline.
for r in fresh:
    chosen[key(r)] = dict(r)
# Accepted reverse-resolved publication metadata may fill missing/stale publication identities.
for r in recovered:
    k=key(r)
    merged=dict(chosen.get(k, {}))
    for field, value in r.items():
        if norm(value): merged[field]=value
    chosen[k]=merged

# Preserve historical local PDF/text evidence without allowing stale no-publication rows to displace
# a current/recovered publication for the same accession.
pub_acc={norm(r.get('accession')).upper() for r in list(chosen.values()) if norm(r.get('publication_status'))=='publication_found'}
for r in base:
    acc=norm(r.get('accession')).upper(); k=key(r)
    d=doi(r.get('publication_doi') or r.get('resolved_doi'))
    p=norm(r.get('publication_pmid') or r.get('resolved_pmid')).lower()
    rejected = (d and (acc,'doi',d) in reject_ids) or (p and (acc,'pmid',p) in reject_ids)
    if rejected:
        rr=dict(r); rr['publication_quarantine_reason']='historical_local_source_matches_quarantined_publication_identity'
        quarantine.append(rr)
        continue
    if k in chosen:
        merged=dict(chosen[k])
        for field, value in r.items():
            if (field.startswith('pdf_') or field.startswith('publication_content_')) and norm(value):
                merged[field]=value
        chosen[k]=merged
    elif usable_pdf(r) or usable_text(r):
        chosen[k]=dict(r)
    elif norm(r.get('publication_status'))=='publication_found':
        chosen[k]=dict(r)
    elif acc not in pub_acc:
        chosen[k]=dict(r)

rows=sorted(chosen.values(), key=lambda r:(norm(r.get('accession')), doi(r.get('publication_doi')), norm(r.get('publication_title'))))
with out_path.open('w', newline='') as fh:
    w=csv.DictWriter(fh, fieldnames=all_fields, delimiter='\t', extrasaction='ignore')
    w.writeheader()
    for r in rows: w.writerow({k:r.get(k,'') for k in all_fields})
with quarantine_path.open('w', newline='') as fh:
    w=csv.DictWriter(fh, fieldnames=all_fields, delimiter='\t', extrasaction='ignore')
    w.writeheader()
    for r in quarantine: w.writerow({k:r.get(k,'') for k in all_fields})
print(f'merged publication rows={len(rows)} quarantine={len(quarantine)}')
for acc in sorted(wanted):
    rr=[r for r in rows if norm(r.get('accession')).upper()==acc]
    print(f"{acc} rows={len(rr)} publications={sum(norm(r.get('publication_status'))=='publication_found' for r in rr)} local_pdf={sum(usable_pdf(r) for r in rr)}")
PY

# Materialize full text from validated PDFs or PMC/Europe-PMC using the recovered DOI/PMID/PMCID.
python "$ROOT/python/stages/03_resolve_publication_content.py" \
  "$OUT/publication_manifest_merged.tsv" \
  --output "$OUT/publication_manifest_with_text.tsv" \
  --content-dir "$OUT/publication_content" \
  --accessions-file "$OUT/accessions.txt" \
  --workers "$WORKERS" \
  --timeout "$TIMEOUT"

# Re-run the existing non-generative mapping/reporter auditor with the refreshed publication corpus.
python "$ROOT/scripts/sdrf_mapping_evidence_audit.py" \
  --accessions-file "$OUT/accessions.txt" \
  --evidence-dir "$V036_EVIDENCE" \
  --evidence-dir "$FULL_EVIDENCE" \
  --publication-manifest "$OUT/publication_manifest_with_text.tsv" \
  --output "$OUT/audit"

printf '\nv0.4.8 publication-accession recovery summary\n'
cat "$OUT/publication_recovery/publication_accession_recovery_summary.json"

printf '\nPublication-text coverage after recovery\n'
python - "$OUT/publication_manifest_with_text.tsv" "$OUT/accessions.txt" <<'PY'
import csv, sys
from collections import defaultdict
manifest, acc_file = sys.argv[1:]
wanted=[x.strip().upper() for x in open(acc_file) if x.strip()]
with open(manifest) as fh:
    rows=list(csv.DictReader(fh, delimiter='\t'))
by=defaultdict(list)
for r in rows: by[(r.get('accession') or '').strip().upper()].append(r)
for acc in wanted:
    rr=by[acc]
    text=[r for r in rr if (r.get('publication_content_text_path') or '').strip()]
    sources=sorted({(r.get('publication_source') or '').strip() for r in text if (r.get('publication_source') or '').strip()})
    titles=[(r.get('publication_title') or '').strip() for r in text]
    print(f"{acc} publication_rows={len(rr)} text_rows={len(text)} sources={','.join(sources) or '-'}")
    for title in titles[:2]: print('  title:', title)
PY

printf '\nQuarantined current-PRIDE publication associations\n'
python - "$OUT/publication_manifest_quarantine.tsv" <<'PY'
import csv, sys
with open(sys.argv[1]) as fh:
    rows=list(csv.DictReader(fh, delimiter='\t'))
if not rows:
    print('none')
for r in rows:
    print(f"{r.get('accession')} doi={r.get('publication_doi') or '-'} pmid={r.get('publication_pmid') or '-'} title={r.get('publication_title') or '-'} reason={r.get('publication_quarantine_reason')}")
PY

printf '\nReporter/mapping inventory after recovered publication text\n'
python - "$OUT/audit/sdrf_mapping_evidence_audit.tsv" <<'PY'
import csv, sys
with open(sys.argv[1]) as fh:
    rows=list(csv.DictReader(fh, delimiter='\t'))
for r in rows:
    print(
        f"{r['accession']} class={r['mapping_class']} confidence={r['confidence']} "
        f"relation={r['relation_recheck']}/{r['relation_confidence']} chemistry={r['chemistry'] or '-'} "
        f"carrier={r['carrier_channels'] or '-'} reference={r['reference_channels'] or '-'} "
        f"single={r['single_cell_channels'] or '-'} blank={r['blank_channels'] or '-'} "
        f"ambiguous={r['ambiguous_channels'] or '-'} pub_text={r['publication_text_rows']}"
    )
PY

printf '\nHigh-confidence reporter-role candidates\n'
cat "$OUT/audit/single_analytical_channel_per_run.txt" 2>/dev/null || true

printf '\nv0.4.8 publication accession recovery complete (non-generative)\n'
printf '  recovery:   %s\n' "$OUT/publication_recovery/publication_accession_recovery_summary.json"
printf '  candidates: %s\n' "$OUT/publication_recovery/publication_accession_recovery_candidates.tsv"
printf '  recovered:  %s\n' "$OUT/publication_recovery/recovered_publications.tsv"
printf '  quarantine: %s\n' "$OUT/publication_manifest_quarantine.tsv"
printf '  text:       %s\n' "$OUT/publication_manifest_with_text.tsv"
printf '  audit:      %s\n' "$OUT/audit/sdrf_mapping_evidence_audit.tsv"
