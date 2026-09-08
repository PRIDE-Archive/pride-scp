#!/usr/bin/env bash
#SBATCH --job-name=pride-scp-kg-merge
set -euo pipefail

START_EPOCH=$(date +%s)
START_ISO=$(date -u +%Y-%m-%dT%H:%M:%SZ)

: "${PERSIST_ROOT:?Set PERSIST_ROOT to the persistent project root}"
SIF="${SIF:-$PERSIST_ROOT/containers/pride-scp.sif}"
SIF_SHA="${SIF_SHA:-$SIF.sha256}"
INPUT_ROOT="${INPUT_ROOT:-$PERSIST_ROOT/data/semantic_inputs}"
RESULT_ROOT="${RESULT_ROOT:-$PERSIST_ROOT/results}"
RUN_NAME="${RUN_NAME:-kg-semantic}"
SCRATCH_ROOT="${SCRATCH_ROOT:-/tmp}"
BASE_RESULT="${BASE_RESULT:-$RESULT_ROOT/$RUN_NAME/base}"
SHARD_ROOT="${SHARD_ROOT:-$RESULT_ROOT/$RUN_NAME/shards}"
FINAL_RESULT="${FINAL_RESULT:-$RESULT_ROOT/$RUN_NAME/final}"

JOB_ID="${SLURM_JOB_ID:-local}"
JOB_SCRATCH="$SCRATCH_ROOT/pride-scp_${JOB_ID}_merge"
LOCAL_SIF="$JOB_SCRATCH/container/pride-scp.sif"
LOCAL_OUT="$JOB_SCRATCH/output"
MERGED="$JOB_SCRATCH/semantic_claims.merged.jsonl"
mkdir -p "$JOB_SCRATCH/container" "$LOCAL_OUT" "$FINAL_RESULT"

if command -v apptainer >/dev/null 2>&1; then CONTAINER_BIN=apptainer; else CONTAINER_BIN=singularity; fi
fail_handler() {
  rc=$?
  trap - EXIT INT TERM
  if [[ $rc -ne 0 ]]; then
    FAIL="$RESULT_ROOT/$RUN_NAME/failed/merge_${JOB_ID}"
    mkdir -p "$FAIL"; rsync -a "$LOCAL_OUT/" "$FAIL/output/" 2>/dev/null || true
    cp -f "$MERGED" "$FAIL/" 2>/dev/null || true
    echo "FAILED rc=$rc; diagnostics=$FAIL; node_scratch_retained=$JOB_SCRATCH" >&2
  else
    rm -rf "$JOB_SCRATCH"
  fi
  exit $rc
}
trap fail_handler EXIT INT TERM

echo "============================================================"
echo "PRIDE-SCP semantic claim merge + canonical resolve"
echo "job_id=$JOB_ID node=$(hostname) start=$START_ISO"
echo "base=$BASE_RESULT shards=$SHARD_ROOT final=$FINAL_RESULT"
echo "============================================================"

[[ -f "$SIF" && -f "$SIF_SHA" ]] || { echo "ERROR: missing SIF or SHA" >&2; exit 2; }
(
  cd "$(dirname "$SIF")"
  sha256sum -c "$(basename "$SIF_SHA")"
)
cp -f "$SIF" "$LOCAL_SIF"
[[ -f "$BASE_RESULT/scp_knowledge_graph.sqlite" ]] || { echo "ERROR: base graph missing" >&2; exit 2; }
cp -f "$BASE_RESULT/scp_knowledge_graph.sqlite" "$LOCAL_OUT/scp_knowledge_graph.sqlite"

mapfile -t CLAIM_FILES < <(find "$SHARD_ROOT" -mindepth 2 -maxdepth 2 -type f -name semantic_claims.jsonl | sort)
[[ ${#CLAIM_FILES[@]} -gt 0 ]] || { echo "ERROR: no semantic_claims.jsonl files found under $SHARD_ROOT" >&2; exit 2; }
: > "$MERGED"
for f in "${CLAIM_FILES[@]}"; do
  echo "merge_claims=$f"
  cat "$f" >> "$MERGED"
done
CLAIM_LINES=$(grep -cve '^$' "$MERGED" || true)
echo "merged_claim_files=${#CLAIM_FILES[@]} merged_claim_lines=$CLAIM_LINES"

"$CONTAINER_BIN" exec \
  --bind "$LOCAL_OUT:/output,$MERGED:/input/semantic_claims.jsonl:ro,$INPUT_ROOT:/portable:ro" \
  "$LOCAL_SIF" \
  python /opt/pride-scp/scripts/scp_knowledge_graph.py \
    --db /output/scp_knowledge_graph.sqlite \
    --import-claims /input/semantic_claims.jsonl

"$CONTAINER_BIN" exec --bind "$LOCAL_OUT:/output" "$LOCAL_SIF" \
  python /opt/pride-scp/scripts/scp_kg_resolve.py \
    --db /output/scp_knowledge_graph.sqlite \
    --output /output/resolution

"$CONTAINER_BIN" exec \
  --bind "$LOCAL_OUT:/output,$INPUT_ROOT:/portable:ro" \
  "$LOCAL_SIF" \
  python /opt/pride-scp/scripts/scp_knowledge_graph.py \
    --db /output/scp_knowledge_graph.sqlite \
    --export /output/exports \
    --accessions-file /portable/accessions.txt

cp -f "$MERGED" "$LOCAL_OUT/semantic_claims.merged.jsonl"
rsync -a --delete "$LOCAL_OUT/" "$FINAL_RESULT/"
END_EPOCH=$(date +%s)
echo "completed elapsed_seconds=$((END_EPOCH-START_EPOCH)) final_result=$FINAL_RESULT"
