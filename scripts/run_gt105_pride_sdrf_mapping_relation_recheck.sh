#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-$PWD}"
V040="${V040:-$ROOT/data/sdrf_mapping_evidence_audit_gt105_pride_v040}"
ACCESSIONS="${ACCESSIONS:-$V040/mapping_accessions.txt}"
BASE_MANIFEST="${BASE_MANIFEST:-$ROOT/work/python/pride_candidate_publications_with_content.tsv}"
FULL_EVIDENCE="${FULL_EVIDENCE:-$ROOT/data/sdrf_annotation_gt105_pride_v031_full/evidence}"
V036_EVIDENCE="${V036_EVIDENCE:-$ROOT/data/sdrf_required_metadata_isolation_context_rescue_gt105_pride_v036/evidence}"
OUT="${OUT:-$ROOT/data/sdrf_mapping_relation_recheck_gt105_pride_v041}"
WORKERS="${WORKERS:-4}"

for f in "$ACCESSIONS" "$BASE_MANIFEST"; do
  if [[ ! -f "$f" ]]; then
    echo "ERROR: required input not found: $f" >&2
    exit 2
  fi
done

mkdir -p "$OUT"
cp "$ACCESSIONS" "$OUT/mapping_accessions.txt"

cat > "$OUT/run_manifest.json" <<JSON
{
  "phase": "GT105_PRIDE_SDRF_v041_mapping_relation_recheck",
  "auditor_version": "pride-scp-sdrf-mapping-auditor-v0.2",
  "mapping_lane_input": "$ACCESSIONS",
  "architectural_changes": [
    "refresh publication metadata directly from current PRIDE project records for the mapping cohort",
    "preserve existing local PDF-backed publication rows while merging refreshed publication metadata",
    "resolve explicit two-channel role assignments joined by respectively",
    "scope multiplex relation support to isobaric evidence locally linked to the single-cell branch",
    "separate non-isobaric single-cell branches from dataset-level TMT/iTRAQ evidence in other sub-studies"
  ],
  "gt_use": "accession cohort seed/evaluation only",
  "runtime_sdrf_gt_metadata_used": false,
  "runtime_sdrf_gt_labels_used": false
}
JSON
cat "$OUT/run_manifest.json"

# Refresh publication associations from current PRIDE instead of relying only on the older
# manuscript manifest.  This is evidence acquisition, not GT metadata lookup.
python "$ROOT/python/stages/01_fetch_pride_publications.py" \
  --accessions-file "$OUT/mapping_accessions.txt" \
  --output "$OUT/pride_publications_refreshed.tsv" \
  --cache-dir "$OUT/pride_publication_cache" \
  --workers "$WORKERS"

# Merge refreshed publication metadata with older rows that may carry locally validated PDFs.
# For the same accession/publication identity, prefer the older row only when it contains an
# actually usable local PDF path; otherwise prefer the refreshed PRIDE row.
python - "$BASE_MANIFEST" "$OUT/pride_publications_refreshed.tsv" "$OUT/mapping_accessions.txt" "$OUT/publication_manifest_merged.tsv" <<'PY'
import csv, os, sys
from pathlib import Path
base_path, fresh_path, accessions_path, out_path = map(Path, sys.argv[1:])
wanted = {x.strip().upper() for x in accessions_path.read_text().splitlines() if x.strip()}

def read(path):
    with path.open(errors="replace") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))

def norm(v):
    return (v or "").strip()

def pub_key(row):
    acc = norm(row.get("accession")).upper()
    doi = norm(row.get("publication_doi") or row.get("resolved_doi")).lower()
    pmid = norm(row.get("publication_pmid") or row.get("resolved_pmid")).lower()
    title = " ".join(norm(row.get("publication_title")).lower().split())
    if doi: return (acc, "doi", doi)
    if pmid: return (acc, "pmid", pmid)
    if title: return (acc, "title", title)
    return (acc, "row", norm(row.get("publication_status")) or "none")

def usable_pdf(row):
    status = norm(row.get("pdf_status")).lower()
    p = norm(row.get("pdf_path"))
    return status in {"downloaded", "already_exists"} and bool(p) and Path(p).is_file()

base = [r for r in read(base_path) if norm(r.get("accession")).upper() in wanted]
fresh = [r for r in read(fresh_path) if norm(r.get("accession")).upper() in wanted]
all_fields=[]
for r in base+fresh:
    for k in r:
        if k not in all_fields: all_fields.append(k)
chosen={}
# Seed refreshed rows so current repository publication metadata wins by default.
for r in fresh:
    chosen[pub_key(r)] = dict(r)
# Preserve a local-PDF row for the same publication identity, or add historical rows that are
# not represented in refreshed metadata.  Historical no-publication rows never displace a
# refreshed publication_found row for the accession.
fresh_pub_acc={norm(r.get("accession")).upper() for r in fresh if norm(r.get("publication_status"))=="publication_found"}
for r in base:
    acc=norm(r.get("accession")).upper()
    key=pub_key(r)
    if usable_pdf(r):
        merged=dict(chosen.get(key, {})); merged.update(r); chosen[key]=merged
    elif key not in chosen and not (acc in fresh_pub_acc and norm(r.get("publication_status")) != "publication_found"):
        chosen[key]=dict(r)
rows=sorted(chosen.values(), key=lambda r:(norm(r.get("accession")), norm(r.get("publication_doi")), norm(r.get("publication_title"))))
with out_path.open("w", newline="") as fh:
    w=csv.DictWriter(fh, fieldnames=all_fields, delimiter="\t", extrasaction="ignore")
    w.writeheader()
    for r in rows: w.writerow({k:r.get(k,"") for k in all_fields})
print(f"merged publication rows={len(rows)} -> {out_path}")
for acc in sorted(wanted):
    rr=[r for r in rows if norm(r.get('accession')).upper()==acc]
    pubs=sum(norm(r.get('publication_status'))=='publication_found' for r in rr)
    pdfs=sum(usable_pdf(r) for r in rr)
    print(f"{acc} rows={len(rr)} publication_found={pubs} usable_local_pdf={pdfs}")
PY

# Materialize searchable text from local PDFs or current publication metadata/PMC links.
python "$ROOT/python/stages/03_resolve_publication_content.py" \
  "$OUT/publication_manifest_merged.tsv" \
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

printf '\nv0.4.1 mapping relation-recheck summary\n'
cat "$OUT/audit/sdrf_mapping_evidence_audit_summary.json"

printf '\nCompact relation/mapping inventory\n'
python - "$OUT/audit/sdrf_mapping_evidence_audit.tsv" <<'PY'
import csv, sys
with open(sys.argv[1]) as fh:
    rows=list(csv.DictReader(fh, delimiter='\t'))
for r in rows:
    print(
        f"{r['accession']} class={r['mapping_class']} confidence={r['confidence']} "
        f"relation={r['relation_recheck']}/{r['relation_confidence']} chemistry={r['chemistry'] or '-'} "
        f"carrier={r['carrier_channels'] or '-'} reference={r['reference_channels'] or '-'} "
        f"single={r['single_cell_channels'] or '-'} ambiguous={r['ambiguous_channels'] or '-'} "
        f"linked_iso={r['single_cell_isobaric_contexts']} linked_noniso={r['single_cell_nonisobaric_contexts']} "
        f"dataset_iso={r['dataset_only_isobaric_contexts']} pub_text={r['publication_text_rows']}"
    )
PY

printf '\nRelation false-positive candidates\n'
cat "$OUT/audit/relation_false_positive_candidate.txt" 2>/dev/null || true

printf '\nv0.4.1 complete\n'
printf '  refreshed publications: %s\n' "$OUT/pride_publications_refreshed.tsv"
printf '  merged publication manifest: %s\n' "$OUT/publication_manifest_merged.tsv"
printf '  publication text manifest: %s\n' "$OUT/publication_manifest_with_text.tsv"
printf '  summary: %s\n' "$OUT/audit/sdrf_mapping_evidence_audit_summary.json"
printf '  inventory: %s\n' "$OUT/audit/sdrf_mapping_evidence_audit.tsv"
printf '  contexts: %s\n' "$OUT/audit/contexts/"
