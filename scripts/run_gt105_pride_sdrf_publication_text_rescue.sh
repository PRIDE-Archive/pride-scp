#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-$PWD}"
BIN="${BIN:-$ROOT/target/release/pride-scp}"
SNAPSHOT="${SNAPSHOT:-$ROOT/data/snapshot}"
ANNOTATIONS="${ANNOTATIONS:-$ROOT/work/python/pride_scp_annotations/annotations}"
PUB_MANIFEST="${PUB_MANIFEST:-$ROOT/work/python/pride_candidate_publications_with_content.tsv}"
RESOLVED_SDRF_DIR="${RESOLVED_SDRF_DIR:-$ROOT/data/sdrf_source_resolution_gt105_pride_v024/resolved}"
V034_RESULTS="${V034_RESULTS:-$ROOT/data/sdrf_required_metadata_evidence_budget_rescue_gt105_pride_v034/sdrf_annotation_results.tsv}"
OUT="${OUT:-$ROOT/data/sdrf_required_metadata_publication_text_rescue_gt105_pride_v035}"
MODEL="${MODEL:-qwen2.5:3b}"
WORKERS="${WORKERS:-4}"

if [[ ! -x "$BIN" ]]; then
  echo "ERROR: pride-scp binary not executable: $BIN" >&2
  exit 2
fi
if [[ ! -f "$V034_RESULTS" ]]; then
  echo "ERROR: v0.3.4 results not found: $V034_RESULTS" >&2
  exit 2
fi
if [[ ! -f "$PUB_MANIFEST" ]]; then
  echo "ERROR: publication manifest not found: $PUB_MANIFEST" >&2
  exit 2
fi

mkdir -p "$OUT"

python - "$V034_RESULTS" "$OUT/residual_required_metadata_accessions.txt" <<'PY'
import csv, sys
src, dst = sys.argv[1:]
with open(src) as fh:
    rows = list(csv.DictReader(fh, delimiter="\t"))
acc = sorted({r["accession"] for r in rows if r.get("completeness_status") == "incomplete_required_metadata"})
if not acc:
    raise SystemExit("no incomplete_required_metadata accessions remain in v0.3.4 results")
with open(dst, "w") as out:
    out.write("\n".join(acc) + "\n")
print(f"v0.3.5 publication-text rescue accessions={len(acc)} -> {dst}")
for a in acc:
    print("  ", a)
PY

cat > "$OUT/run_manifest.json" <<JSON
{
  "phase": "GT105_PRIDE_SDRF_v035_publication_text_materialization_rescue",
  "sdrf_generator_version": "pride-scp-sdrf-v0.3.4",
  "publication_content_version": "pride-scp-v0.1.8",
  "input_results": "$V034_RESULTS",
  "input_publication_manifest": "$PUB_MANIFEST",
  "architectural_changes": [
    "materialize normalized text for validated PDF-backed publication rows upstream of Rust SDRF annotation",
    "keep PDF as canonical Stage-04 publication artifact while populating publication_content_text_path",
    "restrict publication re-resolution to the residual accession cohort",
    "do not add PDF parsing to the Rust SDRF crate"
  ],
  "gt_use": "accession cohort seed/evaluation only",
  "runtime_sdrf_gt_metadata_used": false,
  "runtime_sdrf_gt_labels_used": false
}
JSON
cat "$OUT/run_manifest.json"

python "$ROOT/python/stages/03_resolve_publication_content.py" \
  "$PUB_MANIFEST" \
  --output "$OUT/publication_manifest_with_text.tsv" \
  --content-dir "$OUT/publication_content" \
  --accessions-file "$OUT/residual_required_metadata_accessions.txt" \
  --workers "$WORKERS"

python - "$OUT/publication_manifest_with_text.tsv" "$OUT/residual_required_metadata_accessions.txt" <<'PY'
import csv, json, sys
from collections import Counter, defaultdict
manifest, acc_file = sys.argv[1:]
wanted = {x.strip() for x in open(acc_file) if x.strip()}
with open(manifest) as fh:
    rows = list(csv.DictReader(fh, delimiter="\t"))
by = defaultdict(list)
for r in rows:
    by[r.get("accession", "")].append(r)
print("\nPublication text materialization inventory")
for acc in sorted(wanted):
    rr = by.get(acc, [])
    text = [r for r in rr if (r.get("publication_content_text_path") or "").strip()]
    kinds = Counter((r.get("publication_content_kind") or "").strip() for r in rr)
    print(f"{acc} rows={len(rr)} text_rows={len(text)} kinds={dict(kinds)}")
    for r in text[:3]:
        print("  text:", r.get("publication_content_text_path", ""))
missing = sorted(acc for acc in wanted if not any((r.get("publication_content_text_path") or "").strip() for r in by.get(acc, [])))
print("materialized_accessions:", len(wanted) - len(missing))
print("without_text:", len(missing), missing)
PY

"$BIN" sdrf-annotate \
  --accessions-file "$OUT/residual_required_metadata_accessions.txt" \
  --snapshot "$SNAPSHOT" \
  --resolved-sdrf-dir "$RESOLVED_SDRF_DIR" \
  --annotations-dir "$ANNOTATIONS" \
  --publication-manifest "$OUT/publication_manifest_with_text.tsv" \
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
print("\nv0.3.5 publication-text rescue summary")
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
    kinds = Counter(x.get("source_kind", "") for x in ev.get("evidence", []))
    print(
        f"{acc} valid={r['locally_valid']} errors={r['validation_errors']} "
        f"status={r['completeness_status']} isolation={iso!r} refs={iso_refs} "
        f"manuscript_sources={len(ev.get('manuscript_sources', []))} "
        f"manuscript_items={kinds.get('manuscript_text', 0)} evidence_items={len(ev.get('evidence', []))}"
    )
PY

printf 'v0.3.5 publication-text rescue complete\n'
printf '  materialized manifest: %s\n' "$OUT/publication_manifest_with_text.tsv"
printf '  summary:               %s\n' "$OUT/sdrf_annotation_summary.json"
printf '  results:               %s\n' "$OUT/sdrf_annotation_results.tsv"
printf '  evidence:              %s\n' "$OUT/evidence/"
printf '  review:                %s\n' "$OUT/review/"
printf '  drafts:                %s\n' "$OUT/sdrf/"
