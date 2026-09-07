#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-$PWD}"
BIN="${BIN:-$ROOT/target/release/pride-scp}"
SNAPSHOT="${SNAPSHOT:-$ROOT/data/snapshot}"
ANNOTATIONS="${ANNOTATIONS:-$ROOT/work/python/pride_scp_annotations/annotations}"
PUB_MANIFEST="${PUB_MANIFEST:-$ROOT/work/python/pride_candidate_publications_with_content.tsv}"
RESOLVED_SDRF_DIR="${RESOLVED_SDRF_DIR:-$ROOT/data/sdrf_source_resolution_gt105_pride_v024/resolved}"
V032_RESULTS="${V032_RESULTS:-$ROOT/data/sdrf_required_metadata_rescue_gt105_pride_v032/sdrf_annotation_results.tsv}"
OUT="${OUT:-$ROOT/data/sdrf_required_metadata_fulltext_rescue_gt105_pride_v033}"
MODEL="${MODEL:-qwen2.5:3b}"

if [[ ! -x "$BIN" ]]; then
  echo "ERROR: pride-scp binary not executable: $BIN" >&2
  exit 2
fi
if [[ ! -f "$V032_RESULTS" ]]; then
  echo "ERROR: v0.3.2 required-metadata results not found: $V032_RESULTS" >&2
  exit 2
fi

mkdir -p "$OUT"
python - "$V032_RESULTS" "$OUT/residual_required_metadata_accessions.txt" <<'PY'
import csv, sys
src, dst = sys.argv[1:]
with open(src) as fh:
    rows = list(csv.DictReader(fh, delimiter="\t"))
acc = sorted({r["accession"] for r in rows if r.get("completeness_status") == "incomplete_required_metadata"})
if not acc:
    raise SystemExit("no incomplete_required_metadata accessions remain in v0.3.2 results")
with open(dst, "w") as out:
    out.write("\n".join(acc) + "\n")
print(f"residual required-metadata accessions={len(acc)} -> {dst}")
for a in acc:
    print("  ", a)
PY

cat > "$OUT/run_manifest.json" <<JSON
{
  "phase": "GT105_PRIDE_SDRF_v033_fulltext_required_metadata_rescue",
  "generator_version": "pride-scp-sdrf-v0.3.3",
  "input_results": "$V032_RESULTS",
  "architectural_changes": [
    "decouple manuscript source scan length from Ollama evidence/prompt budget",
    "scan up to 2,000,000 manuscript characters for deterministic keyword windows",
    "retain bounded evidence-item and evidence-character budgets after window extraction",
    "broaden microdissect stem matching without changing template vocabulary policy"
  ],
  "gt_use": "accession cohort seed/evaluation only",
  "runtime_sdrf_gt_metadata_used": false,
  "runtime_sdrf_gt_labels_used": false
}
JSON
cat "$OUT/run_manifest.json"

"$BIN" sdrf-annotate \
  --accessions-file "$OUT/residual_required_metadata_accessions.txt" \
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
print("\nv0.3.3 full-text required-metadata rescue summary")
print("accessions:", len(rows))
print("valid:", Counter(r["locally_valid"] for r in rows))
print("completeness:", Counter(r["completeness_status"] for r in rows))
for r in rows:
    acc = r["accession"]
    ev_path = root / "evidence" / f"{acc}.evidence.json"
    ev = json.loads(ev_path.read_text()) if ev_path.is_file() else {}
    md = ev.get("metadata_scaffold", {})
    iso = md.get("values", {}).get("single_cell_isolation_method", "")
    iso_refs = md.get("evidence_refs", {}).get("single_cell_isolation_method", [])
    print(
        f"{acc} valid={r['locally_valid']} errors={r['validation_errors']} "
        f"status={r['completeness_status']} isolation={iso!r} refs={iso_refs}"
    )
PY

printf 'v0.3.3 full-text required-metadata rescue complete\n'
printf '  summary:  %s\n' "$OUT/sdrf_annotation_summary.json"
printf '  results:  %s\n' "$OUT/sdrf_annotation_results.tsv"
printf '  evidence: %s\n' "$OUT/evidence/"
printf '  review:   %s\n' "$OUT/review/"
printf '  drafts:   %s\n' "$OUT/sdrf/"
