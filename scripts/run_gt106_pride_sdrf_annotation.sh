#!/usr/bin/env bash
set -euo pipefail

# Evaluation/reference cohort only: GT is used solely to select known PRIDE SCP
# accessions for this SDRF reconstruction batch. The SDRF generator never receives
# GT labels, GT metadata annotations, canonical-family annotations, or GT identity edges.
#
# COHORT_MODE:
#   all           - all 106 frozen PRIDE GT accessions
#   missing-sdrf  - only accessions without a deposited SDRF in the snapshot
#   existing-sdrf - only accessions with a deposited SDRF in the snapshot
#
# LIST_ONLY=1 builds the selected cohort files/summary and exits without Ollama.

ROOT="${ROOT:-$(pwd)}"
GT_MASTER="${GT_MASTER:-$ROOT/gpt/final_curation_20260831/PRIDE_SCP_GT_REFERENCE_MASTER_2026-08-31_FINAL_v196.csv}"
SNAPSHOT="${SNAPSHOT:-$ROOT/data/snapshot}"
ANNOTATIONS_DIR="${ANNOTATIONS_DIR:-$ROOT/work/python/pride_scp_annotations/annotations}"
PUBLICATION_MANIFEST="${PUBLICATION_MANIFEST:-$ROOT/work/python/pride_candidate_publications_with_content.tsv}"
MODEL="${MODEL:-qwen2.5:3b}"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434/api/generate}"
TIMEOUT="${TIMEOUT:-1200}"
BINARY="${BINARY:-$ROOT/target/release/pride-scp}"
FORCE="${FORCE:-0}"
COHORT_MODE="${COHORT_MODE:-all}"
LIST_ONLY="${LIST_ONLY:-0}"

case "$COHORT_MODE" in
  all|missing-sdrf|existing-sdrf) ;;
  *) echo "invalid COHORT_MODE=$COHORT_MODE (expected all, missing-sdrf, or existing-sdrf)" >&2; exit 2 ;;
esac
MODE_TAG="${COHORT_MODE//-/_}"
OUT="${OUT:-$ROOT/data/sdrf_annotation_gt106_pride_v021_${MODE_TAG}}"

[[ -f "$GT_MASTER" ]] || { echo "missing GT master: $GT_MASTER" >&2; exit 2; }
[[ -d "$SNAPSHOT" ]] || { echo "missing snapshot: $SNAPSHOT" >&2; exit 2; }
if [[ "$LIST_ONLY" != "1" ]]; then
  [[ -x "$BINARY" ]] || { echo "missing executable: $BINARY" >&2; exit 2; }
fi
mkdir -p "$OUT"
ALL_ACCESSIONS="$OUT/gt106_pride_accessions_all.txt"
ACCESSIONS="$OUT/gt106_pride_accessions.txt"
COHORT_META="$OUT/gt106_pride_cohort_selection.json"

python - "$GT_MASTER" "$SNAPSHOT" "$COHORT_MODE" "$ALL_ACCESSIONS" "$ACCESSIONS" "$COHORT_META" <<'PY'
import csv, json, re, sys
from pathlib import Path
src, snapshot, mode, all_out, selected_out, meta = sys.argv[1:]
src = Path(src); snapshot = Path(snapshot); all_out = Path(all_out); selected_out = Path(selected_out); meta = Path(meta)
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

all_accs = sorted({a for r in rows if is_pride(r) for a in [accession(r)] if a})
if len(all_accs) != 106:
    raise SystemExit(f"expected 106 PRIDE GT accessions, extracted {len(all_accs)}; repository fields={repo_fields}, accession fields={acc_fields}")

def has_sdrf(acc):
    return (snapshot / 'sdrf' / f'{acc}.sdrf.tsv').is_file() and (snapshot / 'sdrf' / f'{acc}.sdrf.tsv').stat().st_size > 0

existing = [a for a in all_accs if has_sdrf(a)]
missing = [a for a in all_accs if not has_sdrf(a)]
if mode == 'all':
    selected = all_accs
elif mode == 'missing-sdrf':
    selected = missing
elif mode == 'existing-sdrf':
    selected = existing
else:
    raise SystemExit(f'unsupported mode {mode}')

all_out.write_text("\n".join(all_accs) + "\n")
selected_out.write_text(("\n".join(selected) + "\n") if selected else "")
meta.write_text(json.dumps({
    "cohort": "frozen_GT196_PRIDE_subset",
    "cohort_mode": mode,
    "all_pride_gt_accessions": len(all_accs),
    "existing_sdrf_accessions": len(existing),
    "missing_sdrf_accessions": len(missing),
    "selected_accessions": len(selected),
    "gt_master": str(src),
    "snapshot": str(snapshot),
    "gt_use": "accession_selection_only",
    "runtime_sdrf_gt_metadata_used": False,
    "runtime_sdrf_gt_labels_used": False,
    "note": "GT selects the known PRIDE SCP cohort only. SDRF field values and provenance come from PRIDE, deposited SDRF, pipeline annotations, and publication/manuscript evidence."
}, indent=2) + "\n")
print(f"GT106 SDRF cohort: all={len(all_accs)} existing_sdrf={len(existing)} missing_sdrf={len(missing)} selected={len(selected)} mode={mode}")
print(f"  selected -> {selected_out}")
PY

cat "$COHORT_META"

if [[ "$LIST_ONLY" == "1" ]]; then
  echo "LIST_ONLY=1: cohort files generated; Ollama annotation not started"
  exit 0
fi
if [[ ! -s "$ACCESSIONS" ]]; then
  echo "selected cohort is empty: $ACCESSIONS" >&2
  exit 3
fi

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
echo "  mode:     $COHORT_MODE"
echo "  cohort:   $ACCESSIONS"
echo "  metadata: $COHORT_META"
echo "  summary:  $OUT/sdrf_annotation_summary.json"
echo "  results:  $OUT/sdrf_annotation_results.tsv"
echo "  errors:   $OUT/errors/"
