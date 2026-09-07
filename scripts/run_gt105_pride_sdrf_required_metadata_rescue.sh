#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-$PWD}"
BIN="${BIN:-$ROOT/target/release/pride-scp}"
SNAPSHOT="${SNAPSHOT:-$ROOT/data/snapshot}"
ANNOTATIONS="${ANNOTATIONS:-$ROOT/work/python/pride_scp_annotations/annotations}"
PUB_MANIFEST="${PUB_MANIFEST:-$ROOT/work/python/pride_candidate_publications_with_content.tsv}"
RESOLVED_SDRF_DIR="${RESOLVED_SDRF_DIR:-$ROOT/data/sdrf_source_resolution_gt105_pride_v024/resolved}"
TRIAGE_ROOT="${TRIAGE_ROOT:-$ROOT/data/sdrf_recovery_triage_gt105_pride_v0312}"
ACCESSIONS_FILE="${ACCESSIONS_FILE:-$TRIAGE_ROOT/denovo_required_metadata.txt}"
OUT="${OUT:-$ROOT/data/sdrf_required_metadata_rescue_gt105_pride_v032}"
MODEL="${MODEL:-qwen2.5:3b}"

if [[ ! -x "$BIN" ]]; then
  echo "ERROR: pride-scp binary not executable: $BIN" >&2
  exit 2
fi
if [[ ! -f "$ACCESSIONS_FILE" ]]; then
  echo "ERROR: required-metadata accession lane not found: $ACCESSIONS_FILE" >&2
  echo "Run scripts/sdrf_postrun_triage.py against the accepted full-cohort result first." >&2
  exit 2
fi

mapfile -t ACCESSIONS < <(grep -E '^PXD[0-9]{6}$' "$ACCESSIONS_FILE" | sort -u)
if [[ ${#ACCESSIONS[@]} -eq 0 ]]; then
  echo "ERROR: no PXD accessions found in $ACCESSIONS_FILE" >&2
  exit 2
fi

mkdir -p "$OUT"
printf '%s\n' "${ACCESSIONS[@]}" > "$OUT/required_metadata_accessions.txt"

cat > "$OUT/run_manifest.json" <<JSON
{
  "phase": "GT105_PRIDE_SDRF_v032_required_metadata_rescue",
  "generator_version": "pride-scp-sdrf-v0.3.2",
  "input_lane": "$ACCESSIONS_FILE",
  "accessions": ${#ACCESSIONS[@]},
  "architectural_changes": [
    "prioritized manuscript Methods/isolation windows before generic proteomics windows",
    "expanded evidence-backed manual-dissection/manual-picking synonym mapping",
    "explicit microwell-chip isolation vocabulary-gap handling",
    "file-role-aware one-row-per-raw-file construction for single-cell, few-cell, blank, QC and bulk comparison runs"
  ],
  "gt_use": "accession cohort seed/evaluation only",
  "runtime_sdrf_gt_metadata_used": false,
  "runtime_sdrf_gt_labels_used": false
}
JSON

printf 'required-metadata rescue accessions=%d -> %s\n' "${#ACCESSIONS[@]}" "$OUT/required_metadata_accessions.txt"
cat "$OUT/run_manifest.json"

"$BIN" sdrf-annotate \
  --accessions-file "$OUT/required_metadata_accessions.txt" \
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
results = root / "sdrf_annotation_results.tsv"
if not results.is_file():
    raise SystemExit(f"missing results: {results}")
with results.open() as fh:
    rows = list(csv.DictReader(fh, delimiter="\t"))
print("\nRequired-metadata rescue summary")
print("accessions:", len(rows))
print("valid:", Counter(r["locally_valid"] for r in rows))
print("completeness:", Counter(r["completeness_status"] for r in rows))
print("generation mode:", Counter(r["generation_mode"] for r in rows))
for r in rows:
    acc = r["accession"]
    audit_path = root / "audit" / f"{acc}.sdrf.audit.json"
    role_counts = {}
    if audit_path.is_file():
        audit = json.loads(audit_path.read_text())
        role_counts = audit.get("study_design", {}).get("file_role_hint_counts", {})
    print(
        acc,
        "valid=" + r["locally_valid"],
        "errors=" + r["validation_errors"],
        "status=" + r["completeness_status"],
        "roles=" + json.dumps(role_counts, sort_keys=True),
    )
PY

echo "v0.3.2 required-metadata rescue complete"
echo "  summary:  $OUT/sdrf_annotation_summary.json"
echo "  results:  $OUT/sdrf_annotation_results.tsv"
echo "  evidence: $OUT/evidence/"
echo "  review:   $OUT/review/"
echo "  drafts:   $OUT/sdrf/"
