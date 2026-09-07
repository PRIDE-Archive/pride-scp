#!/usr/bin/env bash
set -euo pipefail

# v0.3.1 de-novo SDRF pilot after v0.3.0 correctly separated study designs but
# left two downstream bottlenecks: template-aware isolation metadata for one-cell
# studies and unresolved channel/archive mapping for multiplexed/container studies.
# The same five-accession cohort is rerun so v0.3.1 can be compared directly with
# v0.3.0. GT remains accession-cohort/evaluation only.

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
OUT="${OUT:-$ROOT/data/sdrf_denovo_pilot_gt105_pride_v031_template_aware}"

[[ -x "$BINARY" ]] || { echo "missing executable: $BINARY" >&2; exit 2; }
[[ -d "$SNAPSHOT" ]] || { echo "missing snapshot: $SNAPSHOT" >&2; exit 2; }
[[ -s "$UNRESOLVED" ]] || { echo "missing unresolved cohort: $UNRESOLVED" >&2; exit 2; }
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
meta = {
    "phase": "GT105_PRIDE_SDRF_de_novo_v031_template_aware_scaffold",
    "baseline": "data/sdrf_denovo_pilot_gt105_pride_v030_scaffold",
    "pilot_accessions": pilot,
    "architectural_changes": [
        "expanded manuscript windows for isolation/dissection/patch-clamp evidence",
        "deterministic metadata scaffold for structured organism/instrument and strongly supported method facts",
        "template-aware isolation vocabulary handling with explicit unsupported-method gap recording",
        "multiplex chemistry and carrier/reference channel-role hints in study_design",
        "mapping-scaffold validation emits one structural blocker instead of row-level placeholder error storms",
        "reduced prompt evidence/file sample after deterministic scaffolding",
    ],
    "gt_use": "accession cohort seed/evaluation only",
    "runtime_sdrf_gt_metadata_used": False,
    "runtime_sdrf_gt_labels_used": False,
}
meta_path.write_text(json.dumps(meta, indent=2) + "\n")
print(f"v0.3.1 template-aware pilot accessions={len(pilot)} -> {pilot_path}")
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
  --max-evidence-items 96
  --max-evidence-chars 45000
  --max-files-in-prompt 32
)
if [[ "$FORCE" == "1" ]]; then
  args+=(--force)
fi

"$BINARY" "${args[@]}"

echo "v0.3.1 de-novo SDRF template-aware pilot complete"
echo "  selection: $META"
echo "  summary:   $OUT/sdrf_annotation_summary.json"
echo "  results:   $OUT/sdrf_annotation_results.tsv"
echo "  evidence:  $OUT/evidence/   # includes study_design + metadata_scaffold"
echo "  proposals: $OUT/proposals/"
echo "  review:    $OUT/review/"
echo "  drafts:    $OUT/sdrf/"
