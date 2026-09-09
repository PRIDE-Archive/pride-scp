#!/usr/bin/env bash
#SBATCH --job-name=pride-scp-semantic
set -euo pipefail

START_EPOCH=$(date +%s)
START_ISO=$(date -u +%Y-%m-%dT%H:%M:%SZ)

: "${PERSIST_ROOT:?Set PERSIST_ROOT to the persistent project root}"
SIF="${SIF:-$PERSIST_ROOT/containers/pride-scp.sif}"
SIF_SHA="${SIF_SHA:-$SIF.sha256}"
INPUT_ROOT="${INPUT_ROOT:-$PERSIST_ROOT/data/semantic_inputs}"
MODEL_ROOT="${MODEL_ROOT:-$PERSIST_ROOT/models/ollama}"
CACHE_ROOT="${CACHE_ROOT:-$PERSIST_ROOT/persistent_cache}"
RESULT_ROOT="${RESULT_ROOT:-$PERSIST_ROOT/results}"
RUN_NAME="${RUN_NAME:-kg-semantic}"
SCRATCH_ROOT="${SCRATCH_ROOT:-/tmp}"
MODEL="${MODEL:-qwen2.5:3b}"
USE_GPU="${USE_GPU:-1}"
STAGE_INPUT="${STAGE_INPUT:-1}"
STAGE_MODEL="${STAGE_MODEL:-1}"
STAGE_CACHE="${STAGE_CACHE:-1}"

PUBLICATION_MODE="${PUBLICATION_MODE:-semantic}"
MAX_PUBLICATION_CHUNKS="${MAX_PUBLICATION_CHUNKS:-6}"
SEMANTIC_EXCERPT_CHARS="${SEMANTIC_EXCERPT_CHARS:-2800}"
LLM_MAX_PACKET_CHARS="${LLM_MAX_PACKET_CHARS:-8000}"
LLM_MAX_PASSAGES_PER_PACKET="${LLM_MAX_PASSAGES_PER_PACKET:-3}"
LLM_MAX_CLAIMS="${LLM_MAX_CLAIMS:-20}"
LLM_MAX_PACKETS="${LLM_MAX_PACKETS:-0}"
LLM_SEED="${LLM_SEED:-42}"
LLM_FORCE="${LLM_FORCE:-0}"
OLLAMA_NUM_CTX="${OLLAMA_NUM_CTX:-8192}"
OLLAMA_NUM_PREDICT="${OLLAMA_NUM_PREDICT:-3072}"
OLLAMA_TIMEOUT="${OLLAMA_TIMEOUT:-1200}"
LLM_RETRIES="${LLM_RETRIES:-1}"

JOB_ID="${SLURM_JOB_ID:-local}"
TASK_ID="${SLURM_ARRAY_TASK_ID:-0}"
ARRAY_MIN="${SLURM_ARRAY_TASK_MIN:-0}"
SHARD_INDEX=$((TASK_ID-ARRAY_MIN))
N_SHARDS="${N_SHARDS:-${SLURM_ARRAY_TASK_COUNT:-1}}"
CPUS="${SLURM_CPUS_PER_TASK:-1}"
WORKER_CPUS="${WORKER_CPUS:-$((CPUS>1 ? CPUS-1 : 1))}"
LLM_THREADS="${OLLAMA_NUM_THREAD:-$WORKER_CPUS}"
MEMORY="${SLURM_MEM_PER_NODE:-${SLURM_MEM_PER_CPU:-unknown}}"

JOB_SCRATCH="$SCRATCH_ROOT/pride-scp_${JOB_ID}_${TASK_ID}"
LOCAL_SIF="$JOB_SCRATCH/container/pride-scp.sif"
LOCAL_INPUT="$JOB_SCRATCH/input"
LOCAL_MODEL="$JOB_SCRATCH/models/ollama"
LOCAL_CACHE="$JOB_SCRATCH/cache/semantic_claims"
LOCAL_OUT="$JOB_SCRATCH/output"
SHARD_FILE="$JOB_SCRATCH/shard_accessions.txt"
PERSIST_OUT="$RESULT_ROOT/$RUN_NAME/shards/task_$(printf '%04d' "$TASK_ID")"
mkdir -p "$JOB_SCRATCH/container" "$LOCAL_CACHE" "$LOCAL_OUT" "$PERSIST_OUT"

if command -v apptainer >/dev/null 2>&1; then CONTAINER_BIN=apptainer; else CONTAINER_BIN=singularity; fi

fail_handler() {
  rc=$?
  trap - EXIT INT TERM
  if [[ $rc -ne 0 ]]; then
    FAIL="$RESULT_ROOT/$RUN_NAME/failed/task_$(printf '%04d' "$TASK_ID")_${JOB_ID}"
    mkdir -p "$FAIL"
    rsync -a "$LOCAL_OUT/" "$FAIL/output/" 2>/dev/null || true
    cp -f "$SHARD_FILE" "$FAIL/" 2>/dev/null || true
    cp -f "$JOB_SCRATCH"/*.log "$FAIL/" 2>/dev/null || true
    echo "FAILED rc=$rc; diagnostics=$FAIL; node_scratch_retained=$JOB_SCRATCH" >&2
  else
    rm -rf "$JOB_SCRATCH"
  fi
  exit $rc
}
trap fail_handler EXIT INT TERM

echo "============================================================"
echo "PRIDE-SCP semantic LLM shard"
echo "job_id=$JOB_ID array_task=$TASK_ID shard_index=$SHARD_INDEX/$N_SHARDS node=$(hostname) start=$START_ISO"
echo "cpus=$CPUS worker_cpus=$WORKER_CPUS llm_threads=$LLM_THREADS memory=$MEMORY"
echo "use_gpu=$USE_GPU CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES:-unset} model=$MODEL seed=$LLM_SEED force=$LLM_FORCE"
echo "sif=$SIF input_root=$INPUT_ROOT model_root=$MODEL_ROOT cache_root=$CACHE_ROOT"
echo "result=$PERSIST_OUT run_name=$RUN_NAME"
echo "============================================================"

[[ -f "$SIF" && -f "$SIF_SHA" ]] || { echo "ERROR: missing SIF or SHA" >&2; exit 2; }
(
  cd "$(dirname "$SIF")"
  sha256sum -c "$(basename "$SIF_SHA")"
)
cp -f "$SIF" "$LOCAL_SIF"

if [[ "$STAGE_INPUT" == "1" ]]; then
  mkdir -p "$LOCAL_INPUT"; rsync -a "$INPUT_ROOT/" "$LOCAL_INPUT/"
else
  LOCAL_INPUT="$INPUT_ROOT"
fi
[[ -f "$LOCAL_INPUT/accessions.txt" && -f "$LOCAL_INPUT/publication_manifest.tsv" ]] || { echo "ERROR: input bundle incomplete" >&2; exit 2; }
if [[ -f "$LOCAL_INPUT/bundle.sha256" ]]; then (cd "$LOCAL_INPUT" && sha256sum -c bundle.sha256); fi

awk -v idx="$SHARD_INDEX" -v n="$N_SHARDS" 'NF && $1 !~ /^#/ && ((NR-1)%n)==idx {print $1}' "$LOCAL_INPUT/accessions.txt" > "$SHARD_FILE"
SHARD_N=$(wc -l < "$SHARD_FILE")
echo "shard_accessions=$SHARD_N"
cat "$SHARD_FILE"
if [[ "$SHARD_N" -eq 0 ]]; then
  echo "empty shard; nothing to do"
  exit 0
fi

if [[ "$STAGE_MODEL" == "1" ]]; then
  mkdir -p "$LOCAL_MODEL"; rsync -a "$MODEL_ROOT/" "$LOCAL_MODEL/"
else
  LOCAL_MODEL="$MODEL_ROOT"
fi
[[ -d "$LOCAL_MODEL" ]] || { echo "ERROR: model root missing: $LOCAL_MODEL" >&2; exit 2; }
if [[ -f "$LOCAL_MODEL/model-store.sha256" ]]; then
  (cd "$LOCAL_MODEL" && sha256sum -c model-store.sha256)
fi

PERSIST_SEM_CACHE="$CACHE_ROOT/semantic_claims"
mkdir -p "$PERSIST_SEM_CACHE"
if [[ "$STAGE_CACHE" == "1" ]]; then
  rsync -a "$PERSIST_SEM_CACHE/" "$LOCAL_CACHE/"
else
  LOCAL_CACHE="$PERSIST_SEM_CACHE"
fi

GPU_ARGS=()
if [[ "$USE_GPU" == "1" ]]; then
  GPU_ARGS=(--nv)
  command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi || echo "WARNING: nvidia-smi unavailable on host; container --nv will determine GPU availability" >&2
fi

mkdir -p "$JOB_SCRATCH/runtime_tmp" "$JOB_SCRATCH/home"
export RAYON_NUM_THREADS="$WORKER_CPUS"
export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1

EXTRACTOR_EXTRA_ARGS=(--seed "$LLM_SEED" --max-packets "$LLM_MAX_PACKETS")
if [[ "$LLM_FORCE" == "1" ]]; then
  EXTRACTOR_EXTRA_ARGS+=(--force)
fi

"$CONTAINER_BIN" exec "${GPU_ARGS[@]}" \
  --bind "$LOCAL_INPUT:/input:ro,$LOCAL_MODEL:/models/ollama,$LOCAL_CACHE:/cache,$LOCAL_OUT:/output,$SHARD_FILE:/scratch/shard_accessions.txt:ro,$JOB_SCRATCH/runtime_tmp:/runtime_tmp,$JOB_SCRATCH/home:/home/hpc" \
  "$LOCAL_SIF" \
  env \
    HOME=/home/hpc \
    MODEL="$MODEL" \
    OLLAMA_MODELS=/models/ollama \
    OLLAMA_HOST=127.0.0.1:11434 \
    OLLAMA_NO_CLOUD=1 \
    OLLAMA_LOG=/runtime_tmp/ollama.log \
    PRIDE_SCP_RUNTIME_TMP=/runtime_tmp \
    RAYON_NUM_THREADS="$WORKER_CPUS" \
    OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 \
  pride-scp-with-ollama \
    python /opt/pride-scp/scripts/scp_kg_extract_semantic_claims.py \
      --accessions-file /scratch/shard_accessions.txt \
      --snapshot /input/snapshot \
      --publication-manifest /input/publication_manifest.tsv \
      --output /output \
      --cache-dir /cache \
      --model "$MODEL" \
      --ollama-url http://127.0.0.1:11434/api/generate \
      --timeout "$OLLAMA_TIMEOUT" \
      --num-ctx "$OLLAMA_NUM_CTX" \
      --num-predict "$OLLAMA_NUM_PREDICT" \
      --num-thread "$LLM_THREADS" \
      --keep-alive 30m \
      --retries "$LLM_RETRIES" \
      --publication-mode "$PUBLICATION_MODE" \
      --max-publication-chunks "$MAX_PUBLICATION_CHUNKS" \
      --semantic-excerpt-chars "$SEMANTIC_EXCERPT_CHARS" \
      --max-packet-chars "$LLM_MAX_PACKET_CHARS" \
      --max-passages-per-packet "$LLM_MAX_PASSAGES_PER_PACKET" \
      --max-claims "$LLM_MAX_CLAIMS" \
      "${EXTRACTOR_EXTRA_ARGS[@]}" \
      --require-all-packets

rsync -a --delete "$LOCAL_OUT/" "$PERSIST_OUT/"
if [[ "$STAGE_CACHE" == "1" ]]; then
  rsync -a --ignore-existing "$LOCAL_CACHE/" "$PERSIST_SEM_CACHE/"
fi
cp -f "$JOB_SCRATCH/runtime_tmp/ollama.log" "$PERSIST_OUT/ollama.log" 2>/dev/null || true
END_EPOCH=$(date +%s)
echo "completed elapsed_seconds=$((END_EPOCH-START_EPOCH)) final_result=$PERSIST_OUT"
