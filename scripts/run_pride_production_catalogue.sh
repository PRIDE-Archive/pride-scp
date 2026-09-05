#!/usr/bin/env bash
set -euo pipefail

# GT-independent PRIDE production catalogue.
# This runner intentionally accepts no GT/reference inputs. GT evaluation, when
# desired, must be run separately after the catalogue has been generated.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BIN="${PRIDE_SCP_BIN:-$ROOT/target/release/pride-scp}"
SNAPSHOT_DIR="${SNAPSHOT_DIR:-$ROOT/data/snapshot}"
OUT_ROOT="${OUT_ROOT:-$ROOT/data/pride_scp_production_catalogue_v1}"
DISCOVERY_DIR="$OUT_ROOT/discovery"
AUDIT_DIR="$OUT_ROOT/candidate_audit"
CURATION_DIR="$OUT_ROOT/curation"
CATALOGUE_DIR="$OUT_ROOT/catalogue"
ANNOTATIONS_DIR="${ANNOTATIONS_DIR:-$ROOT/work/python/pride_scp_annotations/annotations}"
MODEL="${MODEL:-qwen2.5:3b}"
CPU_THREADS="${CPU_THREADS:-4}"
CURATION_FORCE="${CURATION_FORCE:-0}"
CURATION_PACKETS_ONLY="${CURATION_PACKETS_ONLY:-0}"
EXPECTED_CURATION_VERSION="${EXPECTED_CURATION_VERSION:-v19-shadow-2.7.1}"
# Reproducibility guards for the current frozen 31-Aug-2026 PRIDE snapshot.
# Set either to 0 to disable when intentionally running a newer snapshot.
EXPECTED_PRIMARY_PROJECTS="${EXPECTED_PRIMARY_PROJECTS:-40364}"
EXPECTED_PRIDE_CANDIDATES="${EXPECTED_PRIDE_CANDIDATES:-334}"

for path in "$SNAPSHOT_DIR" "$ANNOTATIONS_DIR"; do
  if [[ ! -e "$path" ]]; then
    echo "ERROR: required input missing: $path" >&2
    exit 2
  fi
done
if [[ ! -x "$BIN" ]]; then
  echo "ERROR: build the Rust CLI first: cargo build --release --locked" >&2
  exit 2
fi

ACTUAL_CURATION_VERSION="$(python "$ROOT/python/curation/pride_scp_curation_v19.py" --version)"
if [[ "$ACTUAL_CURATION_VERSION" != "$EXPECTED_CURATION_VERSION" ]]; then
  echo "ERROR: curation source version mismatch: expected=$EXPECTED_CURATION_VERSION actual=$ACTUAL_CURATION_VERSION" >&2
  exit 3
fi

mkdir -p "$OUT_ROOT"
echo "PRIDE production lane: GT-independent / source-scope=pride-primary / curator=$ACTUAL_CURATION_VERSION"

"$BIN" discover \
  --snapshot "$SNAPSHOT_DIR" \
  --config "$ROOT/config/discovery_terms.json" \
  --output "$DISCOVERY_DIR" \
  --min-score 1 \
  --expected-positive-count 0 \
  --source-scope pride-primary

"$BIN" candidate-audit \
  --candidates "$DISCOVERY_DIR/candidates.jsonl" \
  --config "$ROOT/config/discovery_terms.json" \
  --output "$AUDIT_DIR"

python - "$DISCOVERY_DIR/discovery_summary.json" "$DISCOVERY_DIR/candidates.jsonl" "$EXPECTED_PRIMARY_PROJECTS" "$EXPECTED_PRIDE_CANDIDATES" <<'PY'
import json, sys
from pathlib import Path
summary = json.loads(Path(sys.argv[1]).read_text())
rows = [json.loads(x) for x in Path(sys.argv[2]).read_text().splitlines() if x.strip()]
expected_projects = int(sys.argv[3])
expected_candidates = int(sys.argv[4])
assert summary.get("source_scope") == "pride-primary", summary
assert int(summary.get("registry_supplements_scanned", -1)) == 0, summary
assert int(summary.get("native_massive_projects_scanned", -1)) == 0, summary
if expected_projects:
    assert int(summary.get("primary_projects_scanned", -1)) == expected_projects, summary
if expected_candidates:
    assert len(rows) == expected_candidates, (len(rows), expected_candidates)
for row in rows:
    acc = str(row.get("accession", "")).upper()
    path = str(row.get("project_json_path", "")).replace("\\", "/")
    assert acc.startswith("PXD"), (acc, path)
    assert "/registry/projects/" not in path and "/native/" not in path, (acc, path)
    assert "/projects/" in path, (acc, path)
print(f"production discovery guard passed: primary_projects={summary['primary_projects_scanned']} candidates={len(rows)}")
PY

CURATION_ARGS=(
  "$DISCOVERY_DIR/candidates.jsonl"
  --annotations-dir "$ANNOTATIONS_DIR"
  --output-dir "$CURATION_DIR"
  --model "$MODEL"
  --cpu-threads "$CPU_THREADS"
)
if [[ "$CURATION_FORCE" == "1" ]]; then
  CURATION_ARGS+=(--force)
fi
if [[ "$CURATION_PACKETS_ONLY" == "1" ]]; then
  CURATION_ARGS+=(--packets-only)
fi

python "$ROOT/python/curation/pride_scp_curation_v19.py" "${CURATION_ARGS[@]}"

if [[ "$CURATION_PACKETS_ONLY" == "1" ]]; then
  echo "PRIDE production packets-only pass complete; catalogue export intentionally skipped."
  echo "  discovery: $DISCOVERY_DIR"
  echo "  packets:   $CURATION_DIR/packets"
  exit 0
fi

python "$ROOT/python/catalogue/build_pride_production_catalogue.py" \
  --candidates-jsonl "$DISCOVERY_DIR/candidates.jsonl" \
  --candidate-diagnostics "$AUDIT_DIR/candidate_diagnostics.tsv" \
  --curation-summary "$CURATION_DIR/curation_v19_summary.tsv" \
  --curation-version "$ACTUAL_CURATION_VERSION" \
  --output-dir "$CATALOGUE_DIR"

python - "$OUT_ROOT" "$DISCOVERY_DIR/discovery_summary.json" "$AUDIT_DIR/candidate_audit_summary.json" "$CURATION_DIR/curation_v19_metrics.json" "$CATALOGUE_DIR/pride_scp_production_catalogue_summary.json" <<'PY'
import json, sys
from pathlib import Path
out = Path(sys.argv[1])
discovery = json.loads(Path(sys.argv[2]).read_text())
audit = json.loads(Path(sys.argv[3]).read_text())
curation = json.loads(Path(sys.argv[4]).read_text())
catalogue = json.loads(Path(sys.argv[5]).read_text())
summary = {
    "phase": "PRIDE_GT_independent_production_catalogue_v1",
    "acceptance_passed": (
        discovery.get("source_scope") == "pride-primary"
        and int(discovery.get("registry_supplements_scanned", 0)) == 0
        and int(discovery.get("native_massive_projects_scanned", 0)) == 0
        and int(curation.get("errors", -1)) == 0
        and int(curation.get("successful", -1)) == int(discovery.get("candidates_emitted", -2))
        and catalogue.get("gt_used_for_candidate_selection") is False
        and catalogue.get("gt_used_for_curation") is False
    ),
    "gt_runtime_inputs": [],
    "source_scope": discovery.get("source_scope"),
    "primary_projects_scanned": discovery.get("primary_projects_scanned"),
    "candidates": discovery.get("candidates_emitted"),
    "candidate_priority_counts": {
        "A_specific": audit.get("priority_a_specific"),
        "B_method": audit.get("priority_b_method"),
        "C_broad": audit.get("priority_c_broad"),
        "D_adjacent": audit.get("priority_d_adjacent"),
    },
    "curation_version": curation.get("curation_pipeline_version"),
    "curation_decisions": curation.get("decision_counts", {}),
    "automated_catalogue_includes": catalogue.get("automated_catalogue_includes"),
    "review_queue": catalogue.get("review_queue"),
    "excluded_candidates": catalogue.get("excluded_candidates"),
    "note": "GT-independent production run. Any later GT comparison is evaluation-only and must not alter these outputs.",
}
(out / "pride_scp_production_run_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
print(json.dumps(summary, indent=2, sort_keys=True))
if not summary["acceptance_passed"]:
    raise SystemExit("PRIDE production acceptance gate failed")
PY

echo "PRIDE GT-independent production catalogue complete"
echo "  discovery: $DISCOVERY_DIR"
echo "  audit:     $AUDIT_DIR"
echo "  curation:  $CURATION_DIR"
echo "  catalogue: $CATALOGUE_DIR"
echo "  summary:   $OUT_ROOT/pride_scp_production_run_summary.json"
