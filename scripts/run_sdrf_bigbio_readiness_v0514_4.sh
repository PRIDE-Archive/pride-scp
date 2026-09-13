#!/usr/bin/env bash
set -euo pipefail

# v0.5.14.4 fail-closed BigBio SDRF readiness gate with publication compatibility hardening.
#
# This stage is intentionally non-generative.  It does not call Ollama and does not create
# sample/file/channel relationships.  Candidate SDRFs must already exist in explicitly supplied
# roots.  BigBio tooling can veto a candidate but can never fill missing source truth.

ROOT="${ROOT:-$(pwd)}"
PYTHON="${PYTHON:-python}"
ACCESSIONS_FILE="${ACCESSIONS_FILE:-}"
GRAPH_DB="${GRAPH_DB:-}"
SNAPSHOT="${SNAPSHOT:-$ROOT/data/snapshot}"
OUT="${OUT:-$ROOT/data/sdrf_bigbio_readiness_v0514_4}"
VALIDATOR_MODE="${VALIDATOR_MODE:-required}"
ONTOLOGY_MODE="${ONTOLOGY_MODE:-skip}"
SKILLS_MODE="${SKILLS_MODE:-optional}"
BIGBIO_SKILLS_ROOT="${BIGBIO_SKILLS_ROOT:-}"
REVIEW_APPROVED_MANIFEST="${REVIEW_APPROVED_MANIFEST:-}"
CANDIDATE_ROOTS="${CANDIDATE_ROOTS:-}"

[[ -n "$ACCESSIONS_FILE" && -s "$ACCESSIONS_FILE" ]] || {
  echo "ACCESSIONS_FILE must point to a non-empty accession list" >&2
  exit 2
}
[[ -d "$SNAPSHOT" ]] || { echo "missing snapshot: $SNAPSHOT" >&2; exit 2; }

if [[ -z "$CANDIDATE_ROOTS" ]]; then
  defaults=(
    "$ROOT/data/sdrf_source_resolution_gt105_pride_v024/resolved"
    "$ROOT/data/sdrf_annotation_gt106_pride_v024_all"
    "$ROOT/data/sdrf_annotation_gt106_pride_v024_missing_sdrf"
  )
  selected=()
  for p in "${defaults[@]}"; do
    [[ -d "$p" ]] && selected+=("$p")
  done
  if ((${#selected[@]} == 0)); then
    echo "CANDIDATE_ROOTS is unset and no known candidate roots exist" >&2
    echo "Set colon-separated CANDIDATE_ROOTS explicitly; the readiness gate never recursively guesses." >&2
    exit 2
  fi
  CANDIDATE_ROOTS="$(IFS=:; echo "${selected[*]}")"
fi

args=(
  "$ROOT/scripts/sdrf_bigbio_readiness.py"
  --accessions-file "$ACCESSIONS_FILE"
  --snapshot "$SNAPSHOT"
  --output "$OUT"
  --validator-mode "$VALIDATOR_MODE"
  --ontology-mode "$ONTOLOGY_MODE"
  --skills-mode "$SKILLS_MODE"
)

[[ -n "$GRAPH_DB" ]] && args+=(--graph-db "$GRAPH_DB")
[[ -n "$BIGBIO_SKILLS_ROOT" ]] && args+=(--sdrf-skills-root "$BIGBIO_SKILLS_ROOT")
[[ -n "$REVIEW_APPROVED_MANIFEST" ]] && args+=(--review-approved-manifest "$REVIEW_APPROVED_MANIFEST")

IFS=':' read -r -a roots <<< "$CANDIDATE_ROOTS"
for root in "${roots[@]}"; do
  [[ -n "$root" ]] && args+=(--candidate-root "$root")
done

mkdir -p "$OUT"
"$PYTHON" "${args[@]}"

echo "BigBio-aligned SDRF readiness complete"
echo "  summary:   $OUT/sdrf_readiness_summary.json"
echo "  table:     $OUT/sdrf_readiness.tsv"
echo "  projected: $OUT/projected"
echo "  sandbox:   $OUT/submission/sandbox"
echo "  datasets:  $OUT/submission/datasets"
