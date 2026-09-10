# PRIDE-SCP reproducible Docker → Singularity/Apptainer → Slurm deployment

This deployment layer keeps scientific runtime code independent of any particular cluster. Docker is
the canonical software build. Singularity/Apptainer only converts that image to SIF; it does not
perform a second source build.

## Design

The runtime image contains:

- the release `pride-scp` Rust CLI built with `cargo build --release --locked`;
- Python 3.12.12 installed during the Docker build with `uv`;
- exact transitive Python dependencies from `python/requirements.lock`;
- all maintained Python scripts/modules/resources needed by the pipeline;
- Ollama runtime CPU/CUDA backends from a pinned official Ollama image;
- optional baked Ollama model state.

No Cargo, uv/pip dependency resolution, or model download is required during Slurm jobs.

For larger models, prefer an **external, pre-fetched Ollama model store** under persistent storage.
Each GPU array task stages that model store to node-local scratch before inference. This avoids a very
large SIF while still keeping compute nodes completely offline.

## Local Docker build

```bash
export PRIDE_SCP_IMAGE_REPOSITORY=pride-scp
export PRIDE_SCP_IMAGE_TAG=v0.5.8-hpc
export PRIDE_SCP_DOCKER_TARGET=runtime
export PRIDE_SCP_DOCKER_PLATFORM=linux/amd64

# Default: Ollama runtime in image, model weights external.
export PRIDE_SCP_BAKE_OLLAMA_MODEL=0
export PRIDE_SCP_OLLAMA_MODEL=qwen2.5:3b

./containers/build_docker.sh
```

For a completely self-contained offline image/SIF:

```bash
export PRIDE_SCP_BAKE_OLLAMA_MODEL=1
export PRIDE_SCP_OLLAMA_MODEL=qwen2.5:3b
./containers/build_docker.sh
```

Baking a larger model is supported but can make Docker/SIF conversion and deployment unnecessarily
large. External model stores are therefore the preferred HPC mode.

A definitive scientific build should be from a clean tree:

```bash
PRIDE_SCP_REQUIRE_CLEAN=1 ./containers/build_docker.sh
```

The image records provenance in:

```text
/opt/pride-scp/build-info.txt
```

## Prefetch an Ollama model without relying on compute-node internet

On an internet-connected Docker host:

```bash
MODEL=qwen2.5:3b
MODEL_DIR="$PWD/models/ollama-qwen2.5-3b"

./containers/prefetch_ollama_model.sh "$MODEL" "$MODEL_DIR"
```

The generated `model-store.sha256` uses relative paths and can be checked after rsync/staging:

```bash
(cd "$MODEL_DIR" && sha256sum -c model-store.sha256)
```

Any Ollama model may be used as long as it has been pre-fetched and fits the target CPU/GPU memory.
No Slurm launcher calls `ollama pull`.

## Convert the canonical Docker image to SIF

```bash
IMAGE="pride-scp:v0.5.8-hpc"
SIF="$PWD/containers/pride-scp_v0.5.8-hpc.sif"

./containers/build_singularity.sh "$IMAGE" "$SIF"
```

This creates:

```text
pride-scp_v0.5.8-hpc.sif
pride-scp_v0.5.8-hpc.sif.sha256
pride-scp_v0.5.8-hpc.sif.meta.txt
```

The conversion script prefers Apptainer when available and otherwise uses SingularityCE. It first
tries the local Docker daemon transport and falls back to a Docker archive. It smoke-tests the Rust
CLI, Python imports, semantic-extractor self-test and Ollama binary from the resulting SIF.

## Create a portable semantic-input bundle

Existing publication manifests may contain workstation-local absolute paths. Never deploy those paths
as-is. Build a portable bundle instead:

```bash
ACCESSIONS_FILE="$PWD/data/accessions_for_hpc.txt"
BUNDLE="$PWD/hpc_inputs/semantic_105"

python scripts/prepare_hpc_semantic_inputs.py \
  --accessions-file "$ACCESSIONS_FILE" \
  --snapshot "$PWD/data/snapshot" \
  --publication-manifest "$PWD/data/sdrf_residual_external_publication_recovery_gt105_pride_v050/publication_recovery/combined_publication_manifest.tsv" \
  --v053-audit "$PWD/data/sdrf_generalized_evidence_graph_v053/audit" \
  --seed-graph "$PWD/data/scp_global_knowledge_graph_v055/scp_knowledge_graph.sqlite" \
  --output "$BUNDLE" \
  --rebuild

(cd "$BUNDLE" && sha256sum -c bundle.sha256)
```

The bundle contains only the selected accession metadata/file manifests, locally resolved publication
text, a rewritten relative-path publication manifest, optional v0.5.3 hypothesis tables and an
optional pre-enriched global KG seed.

## Codon deployment profile

Codon is only an example profile. The application and launchers do not hard-code these paths.

```bash
CLUSTER_HOST=sing@codon-slurm-login-01
PERSIST_ROOT=/nfs/research/juan/DIA/singj/pride-scp
SIF="$PWD/containers/pride-scp_v0.5.8-hpc.sif"
BUNDLE="$PWD/hpc_inputs/semantic_105"
MODEL_DIR="$PWD/models/ollama-qwen2.5-3b"

ssh "$CLUSTER_HOST" "mkdir -p \
  '$PERSIST_ROOT/containers' \
  '$PERSIST_ROOT/scripts' \
  '$PERSIST_ROOT/data/semantic_inputs' \
  '$PERSIST_ROOT/models/ollama' \
  '$PERSIST_ROOT/persistent_cache' \
  '$PERSIST_ROOT/results' \
  '$PERSIST_ROOT/logs'"

rsync -avP \
  "$SIF" \
  "$SIF.sha256" \
  "$SIF.meta.txt" \
  "$CLUSTER_HOST:$PERSIST_ROOT/containers/"

rsync -avP --delete \
  "$BUNDLE/" \
  "$CLUSTER_HOST:$PERSIST_ROOT/data/semantic_inputs/"

rsync -avP --delete \
  "$MODEL_DIR/" \
  "$CLUSTER_HOST:$PERSIST_ROOT/models/ollama/"

# Intentionally flatten scripts/slurm/ to remote scripts/.
rsync -avP \
  scripts/slurm/pride_scp_kg_base.sbatch \
  scripts/slurm/pride_scp_semantic_array.sbatch \
  scripts/slurm/pride_scp_kg_merge_resolve.sbatch \
  "$CLUSTER_HOST:$PERSIST_ROOT/scripts/"
```

Verify on the login host:

```bash
ssh "$CLUSTER_HOST" bash -s <<'EOS'
set -euo pipefail
PERSIST_ROOT=/nfs/research/juan/DIA/singj/pride-scp
cd "$PERSIST_ROOT/containers"
sha256sum -c pride-scp_v0.5.8-hpc.sif.sha256
cd "$PERSIST_ROOT/data/semantic_inputs"
sha256sum -c bundle.sha256
cd "$PERSIST_ROOT/models/ollama"
sha256sum -c model-store.sha256
EOS
```

## Slurm workflow

The workflow deliberately avoids concurrent SQLite writes:

```text
base KG job ───────────────┐
                           ├──> final merge/resolver job
semantic LLM array ────────┘
```

The base job and semantic array may run simultaneously. Each array task writes an independent
semantic-claim output. By default the base job only constructs/refreshes the base KG (`BASE_RESOLVE=0`);
resolution is deliberately deferred until after semantic claims have been merged. Set `BASE_RESOLVE=1`
only when a standalone pre-semantic resolution report is explicitly required. The final CPU job imports
all completed claim files into one copied base KG and runs the resolver once.

The resolver is rerun-safe on already-resolved seed graphs. Resolver-derived resolution rows are cleared
before their referenced resolver-created edges, preserving SQLite foreign-key integrity.

The launchers do not contain fixed resource `#SBATCH` directives. Resources are selected at submission
time so the same scripts can run on another cluster.

### Inspect available GPU partitions/resources

Before selecting the GPU profile:

```bash
sinfo -o '%P %G %c %m %l'
```

Set the actual GPU partition/resource syntax used by the target cluster rather than assuming a Codon
partition name.

### Example CPU base job

```bash
PERSIST_ROOT=/nfs/research/juan/DIA/singj/pride-scp
SIF="$PERSIST_ROOT/containers/pride-scp_v0.5.8-hpc.sif"
RUN_NAME=scp-kg-v058-qwen

BASE_JOB=$(sbatch --parsable \
  --partition=research \
  --cpus-per-task=4 \
  --mem=16G \
  --time=02:00:00 \
  --output="$PERSIST_ROOT/logs/pride-scp-base-%j.out" \
  --export=ALL,PERSIST_ROOT="$PERSIST_ROOT",SIF="$SIF",RUN_NAME="$RUN_NAME" \
  "$PERSIST_ROOT/scripts/pride_scp_kg_base.sbatch")

echo "BASE_JOB=$BASE_JOB"
```

### Example GPU semantic array

Choose `GPU_PARTITION` and GPU request after inspecting the cluster. Eight shards is a reasonable
starting point when eight GPUs can run concurrently; the number of accessions does not have to equal
the number of shards.

```bash
GPU_PARTITION=<cluster-gpu-partition>
PERSIST_ROOT=/nfs/research/juan/DIA/singj/pride-scp
SIF="$PERSIST_ROOT/containers/pride-scp_v0.5.8-hpc.sif"
RUN_NAME=scp-kg-v058-qwen
MODEL=qwen2.5:3b
N_SHARDS=8

ARRAY_JOB=$(sbatch --parsable \
  --partition="$GPU_PARTITION" \
  --cpus-per-task=8 \
  --mem=32G \
  --time=04:00:00 \
  --gres=gpu:1 \
  --array=0-$((N_SHARDS-1)) \
  --output="$PERSIST_ROOT/logs/pride-scp-semantic-%A_%a.out" \
  --export=ALL,PERSIST_ROOT="$PERSIST_ROOT",SIF="$SIF",RUN_NAME="$RUN_NAME",MODEL="$MODEL",N_SHARDS="$N_SHARDS",USE_GPU=1 \
  "$PERSIST_ROOT/scripts/pride_scp_semantic_array.sbatch")

echo "ARRAY_JOB=$ARRAY_JOB"
```

For CPU-only execution, submit the same launcher to a CPU partition without `--gres` and use:

```text
USE_GPU=0
```

### Final merge and resolver

```bash
MERGE_JOB=$(sbatch --parsable \
  --dependency="afterok:${BASE_JOB}:${ARRAY_JOB}" \
  --partition=research \
  --cpus-per-task=4 \
  --mem=16G \
  --time=02:00:00 \
  --output="$PERSIST_ROOT/logs/pride-scp-merge-%j.out" \
  --export=ALL,PERSIST_ROOT="$PERSIST_ROOT",SIF="$SIF",RUN_NAME="$RUN_NAME",EXPECTED_SHARDS="$N_SHARDS" \
  "$PERSIST_ROOT/scripts/pride_scp_kg_merge_resolve.sbatch")

echo "MERGE_JOB=$MERGE_JOB"
```

## Monitoring

```bash
squeue -j "$BASE_JOB,$ARRAY_JOB,$MERGE_JOB" \
  -o '%.18i %.12P %.24j %.2t %.10M %.6D %R'

sacct -j "$ARRAY_JOB" \
  --format=JobID,State,Elapsed,AllocCPUS,ReqMem,MaxRSS,ExitCode
```

For a completed task:

```bash
seff <jobid>
```

Inspect packet/model timing in:

```text
$PERSIST_ROOT/results/<run>/shards/task_XXXX/packet_results.tsv
$PERSIST_ROOT/results/<run>/shards/task_XXXX/semantic_claim_extraction_summary.json
$PERSIST_ROOT/results/<run>/shards/task_XXXX/ollama.log
```

Final resolved graph:

```text
$PERSIST_ROOT/results/<run>/final/scp_knowledge_graph.sqlite
$PERSIST_ROOT/results/<run>/final/resolution/kg_resolution_summary.json
$PERSIST_ROOT/results/<run>/final/exports/
```

## Node-local staging

All launchers stage the immutable SIF to `/tmp`. The semantic array can additionally stage:

- portable metadata/manuscript input bundle;
- Ollama model store;
- persistent semantic packet cache.

Controls:

```text
SCRATCH_ROOT
STAGE_INPUT
STAGE_MODEL
STAGE_CACHE
```

Completed results are rsynced back to persistent storage. Semantic packet caches are merged back only
after successful extraction. On failure, lightweight outputs/logs are copied to a persistent `failed/`
directory and the node-local scratch path is printed rather than silently discarded.

## Thread control

The Slurm semantic launcher sets:

```text
RAYON_NUM_THREADS = worker CPUs
OMP_NUM_THREADS = 1
MKL_NUM_THREADS = 1
OPENBLAS_NUM_THREADS = 1
NUMEXPR_NUM_THREADS = 1
```

`OLLAMA_NUM_THREAD` may be supplied explicitly; otherwise worker CPUs are used. This prevents nested
BLAS/OpenMP/Rayon oversubscription while keeping the Ollama request configurable.

## GPU compatibility

The SIF contains Ollama's CPU/CUDA runtime backends from the pinned official Docker image. NVIDIA jobs
execute the SIF with `--nv`, which injects the host NVIDIA driver stack. Therefore:

- the compute node must have a compatible NVIDIA driver;
- the Slurm job must actually allocate a GPU;
- `nvidia-smi`/`CUDA_VISIBLE_DEVICES` should be inspected in the job log;
- do one small packet smoke run before launching a large array.

The launchers never depend on internet access. A missing model is an explicit error rather than a
runtime `ollama pull`.

## v0.5.9 semantic reproducibility controls

The semantic Slurm launcher accepts the following environment controls in addition to the existing
model/context settings:

```text
LLM_SEED=42        fixed generation seed
LLM_FORCE=0        set to 1 to bypass packet cache for a controlled repeat
LLM_MAX_PACKETS=0  optionally cap a benchmark run
```

The extractor also fixes `temperature=0`, `top_k=1`, and `top_p=1.0`. These settings, the seed,
prompt/schema hashes, and response hash are recorded in packet results/cache provenance. After a
prompt/schema upgrade, legacy validated claims can still be imported, but legacy packets do not hide
the corresponding source passages from the new extractor by default.
