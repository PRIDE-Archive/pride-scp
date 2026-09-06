#!/usr/bin/env bash
set -euo pipefail

# Bounded de-novo SDRF reconstruction baseline for five source-unresolved
# primary-PRIDE SCP accessions. The resolved-SDRF lane is frozen at the v0.2.6
# deterministic audit. This pilot intentionally makes no generator/prompt changes:
# it measures the current manuscript-assisted de-novo path before tuning it.
#
# GT use remains cohort/evaluation only. SDRF values come from PRIDE project/file
# metadata, existing PRIDE_SCP annotations, manuscript evidence, and Ollama.

ROOT="${ROOT:-$(pwd)}"
SNAPSHOT="${SNAPSHOT:-$ROOT/data/snapshot}"
ANNOTATIONS_DIR="${ANNOTATIONS_DIR:-$ROOT/work/python/pride_scp_annotations/annotations}"
PUBLICATION_MANIFEST="${PUBLICATION_MANIFEST:-$ROOT/work/python/pride_candidate_publications_with_content.tsv}"
AUDIT_ROOT="${AUDIT_ROOT:-$ROOT/data/sdrf_audit_gt105_pride_v026}"
UNRESOLVED="${UNRESOLVED:-$AUDIT_ROOT/unresolved_accessions.txt}"
BINARY="${BINARY:-$ROOT/target/release/pride-scp}"
MODEL="${MODEL:-qwen2.5:3b}"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434/api/generate}"
TIMEOUT="${TIMEOUT:-1200}"
FORCE="${FORCE:-0}"
OUT="${OUT:-$ROOT/data/sdrf_denovo_pilot_gt105_pride_v027_baseline}"

[[ -x "$BINARY" ]] || { echo "missing executable: $BINARY" >&2; exit 2; }
[[ -d "$SNAPSHOT" ]] || { echo "missing snapshot: $SNAPSHOT" >&2; exit 2; }
[[ -s "$UNRESOLVED" ]] || {
  echo "missing unresolved cohort: $UNRESOLVED" >&2
  echo "run ./scripts/run_gt105_pride_sdrf_audit.sh first" >&2
  exit 2
}
[[ -d "$ANNOTATIONS_DIR" ]] || { echo "missing annotations dir: $ANNOTATIONS_DIR" >&2; exit 2; }
[[ -f "$PUBLICATION_MANIFEST" ]] || { echo "missing publication manifest: $PUBLICATION_MANIFEST" >&2; exit 2; }

mkdir -p "$OUT"
PILOT="$OUT/pilot_accessions.txt"
META="$OUT/pilot_selection.json"

cat > "$PILOT" <<'ACCESSIONS'
PXD001641
PXD004174
PXD028040
PXD029320
PXD042367
ACCESSIONS

python - "$UNRESOLVED" "$PILOT" "$META" <<'PY'
import json, sys
from pathlib import Path
unresolved_path, pilot_path, meta_path = map(Path, sys.argv[1:])
unresolved = {x.strip() for x in unresolved_path.read_text().splitlines() if x.strip()}
pilot = [x.strip() for x in pilot_path.read_text().splitlines() if x.strip()]
missing = [x for x in pilot if x not in unresolved]
if missing:
    raise SystemExit("pilot accession(s) are not in the accepted unresolved cohort: " + ", ".join(missing))
if len(pilot) != 5 or len(set(pilot)) != 5:
    raise SystemExit(f"expected five unique pilot accessions, got {pilot}")
meta = {
    "phase": "GT105_PRIDE_SDRF_de_novo_baseline_pilot",
    "generator_change": False,
    "purpose": "measure current de-novo reconstruction behavior before any further prompt/architecture tuning",
    "unresolved_cohort_file": str(unresolved_path),
    "pilot_accessions": pilot,
    "selection_rationale": {
        "PXD001641": "early single-muscle-fiber proteomics; tests older label-free/sample-file reconstruction",
        "PXD004174": "label-free single Xenopus blastomeres; tests single-cell CE-ESI-HRMS and vendor-container naming",
        "PXD028040": "patch-clamp single-neuron proteomics; tests unconventional single-cell isolation evidence",
        "PXD029320": "multiplexed TMT single-cell proteomics; tests carrier/reference/channel reconstruction",
        "PXD042367": "single/few-cell spatial tissue proteomics; tests mixed cell-count/sample-design reconstruction",
    },
    "gt_use": "accession cohort seed/evaluation only",
    "runtime_sdrf_gt_metadata_used": False,
    "runtime_sdrf_gt_labels_used": False,
}
meta_path.write_text(json.dumps(meta, indent=2) + "\n")
print(f"de-novo pilot accessions={len(pilot)} -> {pilot_path}")
PY
cat "$META"

args=(
  sdrf-annotate
  --accessions-file "$PILOT"
  --snapshot "$SNAPSHOT"
  --annotations-dir "$ANNOTATIONS_DIR"
  --publication-manifest "$PUBLICATION_MANIFEST"
  --output "$OUT"
  --model "$MODEL"
  --ollama-url "$OLLAMA_URL"
  --timeout "$TIMEOUT"
  --max-evidence-items 128
  --max-evidence-chars 60000
  --max-files-in-prompt 64
)
if [[ "$FORCE" == "1" ]]; then
  args+=(--force)
fi

"$BINARY" "${args[@]}"

echo "De-novo SDRF baseline pilot complete"
echo "  selection: $META"
echo "  summary:   $OUT/sdrf_annotation_summary.json"
echo "  results:   $OUT/sdrf_annotation_results.tsv"
echo "  proposals: $OUT/proposals/"
echo "  review:    $OUT/review/"
echo "  drafts:    $OUT/sdrf/"
