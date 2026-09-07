#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-$PWD}"
BIN="${BIN:-$ROOT/target/release/pride-scp}"
SNAPSHOT="${SNAPSHOT:-$ROOT/data/snapshot}"
ANNOTATIONS="${ANNOTATIONS:-$ROOT/work/python/pride_scp_annotations/annotations}"
RESOLVED_SDRF_DIR="${RESOLVED_SDRF_DIR:-$ROOT/data/sdrf_source_resolution_gt105_pride_v024/resolved}"
V041="${V041:-$ROOT/data/sdrf_mapping_relation_recheck_gt105_pride_v041}"
PUB_MANIFEST="${PUB_MANIFEST:-$V041/publication_manifest_with_text.tsv}"
OUT="${OUT:-$ROOT/data/sdrf_relation_scope_recheck_gt105_pride_v042}"
MODEL="${MODEL:-qwen2.5:3b}"

if [[ ! -x "$BIN" ]]; then
  echo "ERROR: pride-scp binary not executable: $BIN" >&2
  exit 2
fi
if [[ ! -f "$PUB_MANIFEST" ]]; then
  echo "ERROR: v0.4.1 publication manifest not found: $PUB_MANIFEST" >&2
  exit 2
fi

mkdir -p "$OUT"
cat > "$OUT/relation_scope_accessions.txt" <<'ACCESSIONS'
PXD004892
PXD017755
PXD035339
PXD056528
ACCESSIONS

cat > "$OUT/run_manifest.json" <<JSON
{
  "phase": "GT105_PRIDE_SDRF_v042_relation_scope_recheck",
  "generator_version": "pride-scp-sdrf-v0.4.2",
  "publication_manifest": "$PUB_MANIFEST",
  "architectural_changes": [
    "require isobaric reporter evidence to be locally linked to the single-cell branch before asserting multiplexed_cells_per_data_file",
    "retain dataset-level TMT/TMTpro/iTRAQ only as diagnostic chemistry when it is not locally linked",
    "exclude plexDIA and lexical plex from isobaric reporter chemistry",
    "reject model-proposed de-novo reporter multiplexing when the deterministic scoped scaffold remains uncertain",
    "report unresolved non-multiplex relation cases as sample_to_file_relation_unresolved rather than channel mapping blockers"
  ],
  "gt_use": "accession cohort seed/evaluation only",
  "runtime_sdrf_gt_metadata_used": false,
  "runtime_sdrf_gt_labels_used": false
}
JSON
cat "$OUT/run_manifest.json"

"$BIN" sdrf-annotate \
  --accessions-file "$OUT/relation_scope_accessions.txt" \
  --snapshot "$SNAPSHOT" \
  --resolved-sdrf-dir "$RESOLVED_SDRF_DIR" \
  --annotations-dir "$ANNOTATIONS" \
  --publication-manifest "$PUB_MANIFEST" \
  --output "$OUT" \
  --model "$MODEL" \
  --max-evidence-items 128 \
  --max-evidence-chars 60000 \
  --max-files-in-prompt 48 \
  --force

python - "$OUT" <<'PY'
import csv, json, sys
from collections import Counter
from pathlib import Path
root = Path(sys.argv[1])
with (root / "sdrf_annotation_results.tsv").open() as fh:
    rows = list(csv.DictReader(fh, delimiter="\t"))
print("\nv0.4.2 relation-scope recheck summary")
print("accessions:", len(rows))
print("valid:", Counter(r["locally_valid"] for r in rows))
print("completeness:", Counter(r["completeness_status"] for r in rows))
for r in rows:
    acc = r["accession"]
    evp = root / "evidence" / f"{acc}.evidence.json"
    ev = json.loads(evp.read_text()) if evp.is_file() else {}
    design = ev.get("study_design", {}) or {}
    review_path = root / "review" / f"{acc}.review.json"
    review = json.loads(review_path.read_text()) if review_path.is_file() else {}
    codes = [x.get("code", "") for x in review.get("issues", []) if x.get("level") == "error"]
    print(
        f"{acc} valid={r['locally_valid']} errors={r['validation_errors']} "
        f"status={r['completeness_status']} result_relation={r['relation_mode']} "
        f"scaffold_relation={design.get('relation_mode_hint','-')}/{design.get('relation_confidence','-')} "
        f"chemistry={design.get('multiplex_chemistry_hint') or '-'} "
        f"mapping_status={design.get('multiplex_mapping_status','-')} "
        f"relation_refs={','.join(design.get('relation_evidence_refs', []) or []) or '-'} "
        f"errors={','.join(codes) or '-'}"
    )
PY

printf '\nv0.4.2 relation-scope recheck complete\n'
printf '  results:  %s\n' "$OUT/sdrf_annotation_results.tsv"
printf '  evidence: %s\n' "$OUT/evidence/"
printf '  review:   %s\n' "$OUT/review/"
printf '  drafts:   %s\n' "$OUT/sdrf/"
