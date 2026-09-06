#!/usr/bin/env bash
set -euo pipefail

# Evaluation/reference cohort only: GT is used solely to select the 106 known PRIDE SCP
# accessions for this SDRF reconstruction batch. The SDRF generator never receives GT
# labels, GT metadata annotations, canonical-family annotations, or GT identity edges.

ROOT="${ROOT:-$(pwd)}"
GT_MASTER="${GT_MASTER:-$ROOT/gpt/final_curation_20260831/PRIDE_SCP_GT_REFERENCE_MASTER_2026-08-31_FINAL_v196.csv}"
SNAPSHOT="${SNAPSHOT:-$ROOT/data/snapshot}"
ANNOTATIONS_DIR="${ANNOTATIONS_DIR:-$ROOT/work/python/pride_scp_annotations/annotations}"
PUBLICATION_MANIFEST="${PUBLICATION_MANIFEST:-$ROOT/work/python/pride_candidate_publications_with_content.tsv}"
OUT="${OUT:-$ROOT/data/sdrf_annotation_gt106_pride_v011}"
MODEL="${MODEL:-qwen2.5:3b}"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434/api/generate}"
TIMEOUT="${TIMEOUT:-1200}"
BINARY="${BINARY:-$ROOT/target/release/pride-scp}"
FORCE="${FORCE:-0}"

[[ -f "$GT_MASTER" ]] || { echo "missing GT master: $GT_MASTER" >&2; exit 2; }
[[ -x "$BINARY" ]] || { echo "missing executable: $BINARY" >&2; exit 2; }
mkdir -p "$OUT"
ACCESSIONS="$OUT/gt106_pride_accessions.txt"
COHORT_META="$OUT/gt106_pride_cohort_selection.json"

python - "$GT_MASTER" "$ACCESSIONS" "$COHORT_META" <<'PY'
import csv, json, re, sys
from pathlib import Path
src, out, meta = map(Path, sys.argv[1:])
pxd_re = re.compile(r"\bPXD\d{6}\b", re.I)
with src.open(newline='', encoding='utf-8-sig') as fh:
    reader = csv.DictReader(fh)
    if not reader.fieldnames:
        raise SystemExit("GT master has no header")
    rows = list(reader)
fields = reader.fieldnames
repo_fields = [f for f in fields if f.lower().strip() in {
    'repository','hosting_repository','data_repository','repo','repository_name'
}]
acc_fields = [f for f in fields if f.lower().strip() in {
    'accession','dataset_accession','repository_accession','pxd','pxd_accession'
}]

def is_pride(row):
    if repo_fields:
        vals = [str(row.get(f,'')).strip().lower() for f in repo_fields]
        return any(v == 'pride' or v.startswith('pride ') for v in vals)
    # Fallback is intentionally conservative: require a cell whose normalized value is PRIDE.
    return any(str(v).strip().lower() == 'pride' for v in row.values())

def accession(row):
    for f in acc_fields:
        m = pxd_re.search(str(row.get(f,'')))
        if m:
            return m.group(0).upper()
    for v in row.values():
        m = pxd_re.search(str(v))
        if m:
            return m.group(0).upper()
    return None

accs = sorted({a for r in rows if is_pride(r) for a in [accession(r)] if a})
if len(accs) != 106:
    raise SystemExit(f"expected 106 PRIDE GT accessions, extracted {len(accs)}; repository fields={repo_fields}, accession fields={acc_fields}")
out.write_text("\n".join(accs) + "\n")
meta.write_text(json.dumps({
    "cohort": "frozen_GT196_PRIDE_subset",
    "accession_count": len(accs),
    "gt_master": str(src),
    "gt_use": "accession_selection_only",
    "runtime_sdrf_gt_metadata_used": False,
    "runtime_sdrf_gt_labels_used": False,
    "note": "GT selects the known 106 PRIDE SCP accessions for this reconstruction batch only. SDRF field values and provenance must come from PRIDE, existing SDRF, pipeline annotations, and publication/manuscript evidence."
}, indent=2) + "\n")
print(f"GT106 SDRF cohort: {len(accs)} PRIDE accessions -> {out}")
PY

args=(
  sdrf-annotate
  --accessions-file "$ACCESSIONS"
  --snapshot "$SNAPSHOT"
  --annotations-dir "$ANNOTATIONS_DIR"
  --publication-manifest "$PUBLICATION_MANIFEST"
  --output "$OUT"
  --model "$MODEL"
  --ollama-url "$OLLAMA_URL"
  --timeout "$TIMEOUT"
)
if [[ "$FORCE" == "1" ]]; then
  args+=(--force)
fi

"$BINARY" "${args[@]}"

echo "GT106 PRIDE SDRF reconstruction batch complete"
echo "  cohort:  $ACCESSIONS"
echo "  summary: $OUT/sdrf_annotation_summary.json"
echo "  results: $OUT/sdrf_annotation_results.tsv"
echo "  errors:  $OUT/errors/"
