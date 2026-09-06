#!/usr/bin/env bash
set -euo pipefail

# Resolve real SDRF sources for the source-resolved primary-PRIDE subset of the
# frozen GT106 cohort. GT provides the accession seed only. No GT field values,
# labels, or canonical annotations enter SDRF source selection.

ROOT="${ROOT:-$(pwd)}"
SNAPSHOT="${SNAPSHOT:-$ROOT/data/snapshot}"
BINARY="${BINARY:-$ROOT/target/release/pride-scp}"
TIMEOUT="${TIMEOUT:-120}"
FORCE="${FORCE:-0}"
COHORT_OUT="${COHORT_OUT:-$ROOT/data/sdrf_annotation_gt106_pride_v024_source_cohort}"
OUT="${OUT:-$ROOT/data/sdrf_source_resolution_gt105_pride_v024}"

[[ -x "$BINARY" ]] || { echo "missing executable: $BINARY" >&2; exit 2; }
[[ -d "$SNAPSHOT" ]] || { echo "missing snapshot: $SNAPSHOT" >&2; exit 2; }

# Reuse the source-truth cohort resolver. This should currently produce 105
# primary PRIDE accessions and defer PXD047101 to MassIVE, but the script does
# not hard-code either count.
LIST_ONLY=1 \
COHORT_MODE=all \
OUT="$COHORT_OUT" \
  "$ROOT/scripts/run_gt106_pride_sdrf_annotation.sh"

ACCESSIONS="$COHORT_OUT/gt106_pride_accessions.txt"
[[ -s "$ACCESSIONS" ]] || { echo "empty source-resolved PRIDE cohort: $ACCESSIONS" >&2; exit 3; }
mkdir -p "$OUT"

args=(
  sdrf-resolve
  --accessions-file "$ACCESSIONS"
  --snapshot "$SNAPSHOT"
  --output "$OUT"
  --timeout "$TIMEOUT"
)
if [[ "$FORCE" == "1" ]]; then
  args+=(--force)
fi

"$BINARY" "${args[@]}"

echo "GT-seeded source-resolved PRIDE SDRF source resolution complete"
echo "  cohort:   $ACCESSIONS"
echo "  summary:  $OUT/sdrf_source_resolution_summary.json"
echo "  sources:  $OUT/sdrf_source_resolution.tsv"
echo "  resolved: $OUT/resolved/"
echo "  audits:   $OUT/audit/"
