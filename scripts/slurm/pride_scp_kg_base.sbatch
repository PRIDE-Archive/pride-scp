#!/usr/bin/env bash
#SBATCH --job-name=pride-scp-kg-base
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
STAGE_INPUT="${STAGE_INPUT:-1}"
BASE_RESOLVE="${BASE_RESOLVE:-0}"

JOB_ID="${SLURM_JOB_ID:-local}"
CPUS="${SLURM_CPUS_PER_TASK:-1}"
MEMORY="${SLURM_MEM_PER_NODE:-${SLURM_MEM_PER_CPU:-unknown}}"
JOB_SCRATCH="$SCRATCH_ROOT/pride-scp_${JOB_ID}_base"
LOCAL_SIF="$JOB_SCRATCH/container/pride-scp.sif"
LOCAL_INPUT="$JOB_SCRATCH/input"
LOCAL_OUT="$JOB_SCRATCH/output"
PERSIST_OUT="$RESULT_ROOT/$RUN_NAME/base"
mkdir -p "$JOB_SCRATCH/container" "$LOCAL_OUT" "$PERSIST_OUT"

if command -v apptainer >/dev/null 2>&1; then CONTAINER_BIN=apptainer; else CONTAINER_BIN=singularity; fi

fail_handler() {
  rc=$?
  trap - EXIT INT TERM
  if [[ $rc -ne 0 ]]; then
    FAIL="$RESULT_ROOT/$RUN_NAME/failed/base_${JOB_ID}"
    mkdir -p "$FAIL"
    rsync -a "$LOCAL_OUT/" "$FAIL/output/" 2>/dev/null || true
    cp -f "$JOB_SCRATCH"/*.txt "$FAIL/" 2>/dev/null || true
    echo "FAILED rc=$rc; preserved diagnostics under $FAIL and node scratch $JOB_SCRATCH" >&2
  else
    rm -rf "$JOB_SCRATCH"
  fi
  exit $rc
}
trap fail_handler EXIT INT TERM

echo "============================================================"
echo "PRIDE-SCP base KG build"
echo "job_id=$JOB_ID node=$(hostname) start=$START_ISO"
echo "cpus=$CPUS memory=$MEMORY container_runtime=$($CONTAINER_BIN --version | head -1)"
echo "sif=$SIF input_root=$INPUT_ROOT result=$PERSIST_OUT run_name=$RUN_NAME base_resolve=$BASE_RESOLVE"
echo "============================================================"

[[ -f "$SIF" && -f "$SIF_SHA" ]] || { echo "ERROR: missing SIF or SHA file" >&2; exit 2; }
(
  cd "$(dirname "$SIF")"
  sha256sum -c "$(basename "$SIF_SHA")"
)
cp -f "$SIF" "$LOCAL_SIF"

if [[ "$STAGE_INPUT" == "1" ]]; then
  mkdir -p "$LOCAL_INPUT"
  rsync -a "$INPUT_ROOT/" "$LOCAL_INPUT/"
else
  LOCAL_INPUT="$INPUT_ROOT"
fi
[[ -f "$LOCAL_INPUT/accessions.txt" && -f "$LOCAL_INPUT/publication_manifest.tsv" ]] || { echo "ERROR: portable input bundle incomplete" >&2; exit 2; }
if [[ -f "$LOCAL_INPUT/bundle.sha256" ]]; then
  (cd "$LOCAL_INPUT" && sha256sum -c bundle.sha256)
fi

if [[ -f "$LOCAL_INPUT/seed_graph.sqlite" ]]; then
  cp -f "$LOCAL_INPUT/seed_graph.sqlite" "$LOCAL_OUT/scp_knowledge_graph.sqlite"
  echo "seed_graph=$LOCAL_INPUT/seed_graph.sqlite"
fi

V053_ARGS=()
[[ -d "$LOCAL_INPUT/v053_audit" ]] && V053_ARGS=(--v053-audit /input/v053_audit)

"$CONTAINER_BIN" exec \
  --bind "$LOCAL_INPUT:/input:ro,$LOCAL_OUT:/output" \
  "$LOCAL_SIF" \
  python /opt/pride-scp/scripts/scp_kg_build.py \
    --accessions-file /input/accessions.txt \
    --snapshot /input/snapshot \
    --publication-manifest /input/publication_manifest.tsv \
    "${V053_ARGS[@]}" \
    --output /output

if [[ "$BASE_RESOLVE" == "1" ]]; then
  "$CONTAINER_BIN" exec --bind "$LOCAL_OUT:/output" "$LOCAL_SIF" \
    python /opt/pride-scp/scripts/scp_kg_resolve.py \
      --db /output/scp_knowledge_graph.sqlite \
      --output /output/resolution
else
  echo "base_resolve=0; deferring canonical resolution until semantic-claim merge"
fi

rsync -a --delete "$LOCAL_OUT/" "$PERSIST_OUT/"
END_EPOCH=$(date +%s)
echo "completed elapsed_seconds=$((END_EPOCH-START_EPOCH)) final_result=$PERSIST_OUT"
