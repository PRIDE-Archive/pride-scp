#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-$PWD}"
BIN="${BIN:-$ROOT/target/release/pride-scp}"
SNAPSHOT="${SNAPSHOT:-$ROOT/data/snapshot}"
ANNOTATIONS="${ANNOTATIONS:-$ROOT/work/python/pride_scp_annotations/annotations}"
RESOLVED_SDRF_DIR="${RESOLVED_SDRF_DIR:-$ROOT/data/sdrf_source_resolution_gt105_pride_v024/resolved}"
V035_ROOT="${V035_ROOT:-$ROOT/data/sdrf_required_metadata_publication_text_rescue_gt105_pride_v035}"
V035_RESULTS="${V035_RESULTS:-$V035_ROOT/sdrf_annotation_results.tsv}"
PUB_MANIFEST="${PUB_MANIFEST:-$V035_ROOT/publication_manifest_with_text.tsv}"
OUT="${OUT:-$ROOT/data/sdrf_required_metadata_isolation_context_rescue_gt105_pride_v036}"
MODEL="${MODEL:-qwen2.5:3b}"

if [[ ! -x "$BIN" ]]; then
  echo "ERROR: pride-scp binary not executable: $BIN" >&2
  exit 2
fi
if [[ ! -f "$V035_RESULTS" ]]; then
  echo "ERROR: v0.3.5 results not found: $V035_RESULTS" >&2
  exit 2
fi
if [[ ! -f "$PUB_MANIFEST" ]]; then
  echo "ERROR: v0.3.5 materialized publication manifest not found: $PUB_MANIFEST" >&2
  exit 2
fi

mkdir -p "$OUT"

python - "$V035_RESULTS" "$V035_ROOT/evidence" "$OUT/isolation_rescue_accessions.txt" <<'PY'
import csv, json, sys
from pathlib import Path
results, evidence_dir, dst = sys.argv[1:]
with open(results) as fh:
    rows = list(csv.DictReader(fh, delimiter="\t"))
evroot = Path(evidence_dir)
selected = []
for r in rows:
    acc = r["accession"]
    evp = evroot / f"{acc}.evidence.json"
    ev = json.loads(evp.read_text()) if evp.is_file() else {}
    iso = ev.get("metadata_scaffold", {}).get("values", {}).get("single_cell_isolation_method", "").strip()
    if not iso:
        selected.append(acc)
selected = sorted(set(selected))
if not selected:
    raise SystemExit("no v0.3.5 accessions remain without deterministic isolation metadata")
Path(dst).write_text("\n".join(selected) + "\n")
print(f"v0.3.6 isolation-context rescue accessions={len(selected)} -> {dst}")
for acc in selected:
    print("  ", acc)
PY

cat > "$OUT/run_manifest.json" <<JSON
{
  "phase": "GT105_PRIDE_SDRF_v036_isolation_context_rescue",
  "generator_version": "pride-scp-sdrf-v0.3.6",
  "input_results": "$V035_RESULTS",
  "publication_manifest": "$PUB_MANIFEST",
  "architectural_changes": [
    "center manuscript evidence windows on actual regex matches instead of blank-line paragraph starts",
    "align single-cell isolation field relevance vocabulary with deterministic isolation extraction",
    "require explicit manual action or instrumentation evidence before mapping to manual picking",
    "reuse v0.3.5 materialized publication text without expanding the Ollama prompt"
  ],
  "gt_use": "accession cohort seed/evaluation only",
  "runtime_sdrf_gt_metadata_used": false,
  "runtime_sdrf_gt_labels_used": false
}
JSON
cat "$OUT/run_manifest.json"

"$BIN" sdrf-annotate \
  --accessions-file "$OUT/isolation_rescue_accessions.txt" \
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
import csv, json, re, sys
from collections import Counter
from pathlib import Path
root = Path(sys.argv[1])
with (root / "sdrf_annotation_results.tsv").open() as fh:
    rows = list(csv.DictReader(fh, delimiter="\t"))
print("\nv0.3.6 isolation-context rescue summary")
print("accessions:", len(rows))
print("valid:", Counter(r["locally_valid"] for r in rows))
print("completeness:", Counter(r["completeness_status"] for r in rows))
pat = re.compile(r"tweezer|manual|dissect|microdissect|cellenone|mechanically dissociat|individually transferred", re.I)
for r in rows:
    acc = r["accession"]
    evp = root / "evidence" / f"{acc}.evidence.json"
    ev = json.loads(evp.read_text()) if evp.is_file() else {}
    md = ev.get("metadata_scaffold", {})
    iso = md.get("values", {}).get("single_cell_isolation_method", "")
    refs = md.get("evidence_refs", {}).get("single_cell_isolation_method", [])
    manuscript = [x for x in ev.get("evidence", []) if x.get("source_kind") == "manuscript_text"]
    relevant = []
    for item in manuscript:
        if pat.search((item.get("text") or "")):
            snippet = " ".join((item.get("text") or "").split())[:260]
            relevant.append((item.get("id", ""), snippet))
    print(
        f"{acc} valid={r['locally_valid']} errors={r['validation_errors']} "
        f"status={r['completeness_status']} relation={r['relation_mode']} "
        f"isolation={iso!r} refs={refs} manuscript_sources={len(ev.get('manuscript_sources', []))} "
        f"manuscript_items={len(manuscript)} isolation_windows={len(relevant)}"
    )
    for ref, snippet in relevant[:3]:
        print(f"  {ref}: {snippet}")
PY

printf 'v0.3.6 isolation-context rescue complete\n'
printf '  summary:  %s\n' "$OUT/sdrf_annotation_summary.json"
printf '  results:  %s\n' "$OUT/sdrf_annotation_results.tsv"
printf '  evidence: %s\n' "$OUT/evidence/"
printf '  review:   %s\n' "$OUT/review/"
printf '  drafts:   %s\n' "$OUT/sdrf/"
