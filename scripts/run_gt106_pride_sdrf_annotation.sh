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
OUT="${OUT:-$ROOT/data/sdrf_annotation_gt106_pride_v022_${MODE_TAG}}"

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
from collections import Counter
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

def is_pride_gt_label(row):
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

def registry_host(acc):
    path = snapshot / 'registry' / 'projects' / f'{acc}.json'
    if not path.is_file():
        return ''
    try:
        obj = json.loads(path.read_text())
    except Exception:
        return ''
    return str(obj.get('registryHostingRepository') or '').strip()

def source_host(acc):
    if (snapshot / 'projects' / f'{acc}.json').is_file():
        return 'PRIDE'
    return registry_host(acc) or 'unknown'

def has_sdrf(acc):
    p = snapshot / 'sdrf' / f'{acc}.sdrf.tsv'
    return p.is_file() and p.stat().st_size > 0

def sdrf_source_class(acc):
    p = snapshot / 'sdrf' / f'{acc}.sdrf.tsv'
    if not p.is_file() or p.stat().st_size == 0:
        return 'missing'
    try:
        with p.open(newline='', encoding='utf-8-sig', errors='replace') as fh:
            r = csv.reader(fh, delimiter='\t')
            header = [x.strip().lower() for x in next(r)]
            try:
                j = header.index('comment[sdrf annotation tool]')
            except ValueError:
                return 'repository_or_unknown'
            vals = []
            for i, row in enumerate(r):
                if j < len(row) and row[j].strip():
                    vals.append(row[j].strip().lower())
                if i >= 50:
                    break
        text = ' | '.join(vals)
        if 'manual curation' in text or 'bigbio' in text:
            return 'community_curated'
        if 'hamlet' in text or 'agentic' in text:
            return 'agentic'
        if 'pride-scp' in text:
            return 'pride_scp_generated'
        return 'repository_or_unknown'
    except Exception:
        return 'unclassified'

all_accs = sorted({a for r in rows if is_pride_gt_label(r) for a in [accession(r)] if a})
if len(all_accs) != 106:
    raise SystemExit(f"expected 106 GT rows labelled PRIDE, extracted {len(all_accs)}; repository fields={repo_fields}, accession fields={acc_fields}")

inventory = []
for acc in all_accs:
    host = source_host(acc)
    inventory.append({
        'accession': acc,
        'gt_repository_label': 'PRIDE',
        'source_resolved_hosting_repository': host,
        'primary_pride_project_snapshot': 'yes' if host == 'PRIDE' else 'no',
        'sdrf_present': 'yes' if has_sdrf(acc) else 'no',
        'sdrf_source_class_content_heuristic': sdrf_source_class(acc),
    })

inventory_path = meta.parent / 'gt106_pride_sdrf_source_inventory.tsv'
with inventory_path.open('w', newline='') as fh:
    w = csv.DictWriter(fh, fieldnames=list(inventory[0]), delimiter='\t')
    w.writeheader(); w.writerows(inventory)

resolved_pride = [r['accession'] for r in inventory if r['source_resolved_hosting_repository'] == 'PRIDE']
non_pride = [r for r in inventory if r['source_resolved_hosting_repository'] != 'PRIDE']
non_pride_path = meta.parent / 'gt106_non_pride_or_unresolved_accessions.tsv'
with non_pride_path.open('w', newline='') as fh:
    w = csv.DictWriter(fh, fieldnames=['accession','gt_repository_label','source_resolved_hosting_repository'], delimiter='\t')
    w.writeheader()
    for r in non_pride:
        w.writerow({k:r[k] for k in w.fieldnames})

existing = [a for a in resolved_pride if has_sdrf(a)]
missing = [a for a in resolved_pride if not has_sdrf(a)]
if mode == 'all':
    selected = resolved_pride
elif mode == 'missing-sdrf':
    selected = missing
elif mode == 'existing-sdrf':
    selected = existing
else:
    raise SystemExit(f'unsupported mode {mode}')

all_out.write_text("\n".join(resolved_pride) + "\n")
selected_out.write_text(("\n".join(selected) + "\n") if selected else "")
source_counts = Counter(r['sdrf_source_class_content_heuristic'] for r in inventory if r['accession'] in resolved_pride and r['sdrf_present']=='yes')
meta.write_text(json.dumps({
    "cohort": "frozen_GT196_rows_labelled_PRIDE_resolved_against_source_snapshot",
    "cohort_mode": mode,
    "gt_rows_labelled_pride": len(all_accs),
    "source_resolved_primary_pride_accessions": len(resolved_pride),
    "source_resolved_non_pride_or_unresolved": len(non_pride),
    "existing_sdrf_accessions": len(existing),
    "missing_sdrf_accessions": len(missing),
    "selected_accessions": len(selected),
    "existing_sdrf_source_class_counts": dict(sorted(source_counts.items())),
    "gt_master": str(src),
    "snapshot": str(snapshot),
    "gt_use": "accession_cohort_seed_only",
    "runtime_sdrf_gt_metadata_used": False,
    "runtime_sdrf_gt_labels_used": False,
    "source_inventory": str(inventory_path),
    "non_pride_or_unresolved_inventory": str(non_pride_path),
    "note": "The frozen GT repository label is not treated as source truth. Runtime scope is restricted to accessions present in the primary PRIDE snapshot; registry hostingRepository is used to explain mismatches. SDRF fields never come from GT. SDRF source class is a content heuristic from comment[sdrf annotation tool], not authoritative repository provenance."
}, indent=2) + "\n")
print(f"GT-labelled PRIDE rows={len(all_accs)}; source-resolved primary PRIDE={len(resolved_pride)}; non-PRIDE/unresolved={len(non_pride)}")
print(f"primary PRIDE SDRF: existing={len(existing)} missing={len(missing)} selected={len(selected)} mode={mode}")
print(f"  selected -> {selected_out}")
print(f"  source inventory -> {inventory_path}")
if non_pride:
    print("  deferred non-PRIDE/unresolved -> " + ", ".join(f"{r['accession']}({r['source_resolved_hosting_repository']})" for r in non_pride))
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
