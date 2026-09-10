#!/usr/bin/env bash
#SBATCH --job-name=pride-scp-sdrf-ready
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=01:00:00
set -Eeuo pipefail

: "${PERSIST_ROOT:?set PERSIST_ROOT}"
: "${SIF:?set SIF}"
: "${ACCESSIONS_FILE:?set ACCESSIONS_FILE}"
: "${GRAPH_DB:?set GRAPH_DB}"
: "${SNAPSHOT:?set SNAPSHOT}"
CANDIDATE_ROOTS="${CANDIDATE_ROOTS:-}"
: "${OUT:?set OUT}"

VALIDATOR_MODE="${VALIDATOR_MODE:-required}"
ONTOLOGY_MODE="${ONTOLOGY_MODE:-skip}"
SKILLS_MODE="${SKILLS_MODE:-optional}"
BIGBIO_SKILLS_ROOT="${BIGBIO_SKILLS_ROOT:-}"
REVIEW_APPROVED_MANIFEST="${REVIEW_APPROVED_MANIFEST:-}"

mkdir -p "$OUT"
FAILURE_RECEIPT="$OUT/launcher_failure.txt"
rm -f "$FAILURE_RECEIPT"

on_err() {
  local rc=$?
  local line="${BASH_LINENO[0]:-unknown}"
  local cmd="${BASH_COMMAND:-unknown}"
  {
    echo "status=launcher_failed"
    echo "exit_code=$rc"
    echo "line=$line"
    echo "command=$cmd"
    echo "job_id=${SLURM_JOB_ID:-na}"
    echo "node=${SLURMD_NODENAME:-$(hostname)}"
    echo "time=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } > "$FAILURE_RECEIPT"
  echo "ERROR: readiness launcher failed rc=$rc line=$line command=$cmd" >&2
  echo "failure_receipt=$FAILURE_RECEIPT" >&2
  exit "$rc"
}
trap on_err ERR

die() {
  echo "ERROR: $*" >&2
  false
}

command -v singularity >/dev/null 2>&1 || die "singularity is not available"
[[ -f "$SIF" ]] || die "SIF does not exist: $SIF"
[[ -f "$ACCESSIONS_FILE" ]] || die "accessions file does not exist: $ACCESSIONS_FILE"
[[ -f "$GRAPH_DB" ]] || die "graph DB does not exist: $GRAPH_DB"
[[ -d "$SNAPSHOT" ]] || die "snapshot does not exist: $SNAPSHOT"

# Verify the SIF without relying on the filename recorded in the checksum manifest or the Slurm
# submission working directory. build_singularity.sh manifests contain the hash as field 1.
CHECKSUM_FILE="${SIF}.sha256"
[[ -s "$CHECKSUM_FILE" ]] || die "SIF checksum file missing: $CHECKSUM_FILE"
EXPECTED_SHA="$(awk 'NF {print $1; exit}' "$CHECKSUM_FILE")"
ACTUAL_SHA="$(sha256sum "$SIF" | awk '{print $1}')"
[[ "$EXPECTED_SHA" =~ ^[0-9a-fA-F]{64}$ ]] || die "invalid expected SIF SHA in $CHECKSUM_FILE: $EXPECTED_SHA"
[[ "${EXPECTED_SHA,,}" == "${ACTUAL_SHA,,}" ]] || die "SIF SHA mismatch expected=$EXPECTED_SHA actual=$ACTUAL_SHA"
echo "$(basename "$SIF"): OK ($ACTUAL_SHA)"

# Candidate absence is a scientific blocker, not a launcher failure. Ignore missing requested roots.
valid_candidate_roots=()
if [[ -n "$CANDIDATE_ROOTS" ]]; then
  IFS=':' read -r -a requested_candidate_roots <<< "$CANDIDATE_ROOTS"
  for root in "${requested_candidate_roots[@]}"; do
    [[ -z "$root" ]] && continue
    if [[ -d "$root" ]]; then
      valid_candidate_roots+=("$root")
    else
      echo "WARNING: candidate root does not exist; ignoring: $root" >&2
    fi
  done
fi
if ((${#valid_candidate_roots[@]} == 0)); then
  EMPTY_CANDIDATE_ROOT="$OUT/_empty_candidate_root"
  mkdir -p "$EMPTY_CANDIDATE_ROOT"
  valid_candidate_roots=("$EMPTY_CANDIDATE_ROOT")
  echo "WARNING: no usable candidate roots; readiness will emit fail-closed per-accession blockers" >&2
fi

if [[ -n "$BIGBIO_SKILLS_ROOT" && ! -d "$BIGBIO_SKILLS_ROOT" ]]; then
  if [[ "$SKILLS_MODE" == "required" ]]; then
    die "required sdrf-skills root does not exist: $BIGBIO_SKILLS_ROOT"
  fi
  echo "WARNING: optional sdrf-skills root missing; disabling skills checks: $BIGBIO_SKILLS_ROOT" >&2
  BIGBIO_SKILLS_ROOT=""
fi

if [[ -n "$REVIEW_APPROVED_MANIFEST" && ! -f "$REVIEW_APPROVED_MANIFEST" ]]; then
  die "review-approved manifest does not exist: $REVIEW_APPROVED_MANIFEST"
fi

echo "============================================================"
echo "PRIDE-SCP v0.5.14.2 SDRF readiness launcher"
echo "job_id=${SLURM_JOB_ID:-na} node=${SLURMD_NODENAME:-$(hostname)}"
echo "sif=$SIF"
echo "accessions=$ACCESSIONS_FILE"
echo "graph=$GRAPH_DB"
echo "snapshot=$SNAPSHOT"
printf 'candidate_root=%s\n' "${valid_candidate_roots[@]}"
echo "validator_mode=$VALIDATOR_MODE ontology_mode=$ONTOLOGY_MODE skills_mode=$SKILLS_MODE"
echo "skills_root=${BIGBIO_SKILLS_ROOT:-none}"
echo "output=$OUT"
echo "============================================================"

# Bind all runtime inputs to stable in-container paths. Do not rely on site-specific Singularity
# auto-bind rules for /nfs paths.
bind_args=(
  --bind "$ACCESSIONS_FILE:/data/readiness_accessions.txt:ro"
  --bind "$GRAPH_DB:/data/readiness_graph.sqlite:ro"
  --bind "$SNAPSHOT:/data/readiness_snapshot:ro"
  --bind "$OUT:/results/readiness"
)

cmd=(
  python /opt/pride-scp/scripts/sdrf_bigbio_readiness.py
  --accessions-file /data/readiness_accessions.txt
  --graph-db /data/readiness_graph.sqlite
  --snapshot /data/readiness_snapshot
  --output /results/readiness
  --validator-mode "$VALIDATOR_MODE"
  --ontology-mode "$ONTOLOGY_MODE"
  --skills-mode "$SKILLS_MODE"
)

idx=0
for root in "${valid_candidate_roots[@]}"; do
  inside="/data/readiness_candidates_${idx}"
  bind_args+=(--bind "$root:$inside:ro")
  cmd+=(--candidate-root "$inside")
  idx=$((idx + 1))
done

if [[ -n "$BIGBIO_SKILLS_ROOT" ]]; then
  bind_args+=(--bind "$BIGBIO_SKILLS_ROOT:/models/sdrf-skills:ro")
  cmd+=(--sdrf-skills-root /models/sdrf-skills)
fi
if [[ -n "$REVIEW_APPROVED_MANIFEST" ]]; then
  bind_args+=(--bind "$REVIEW_APPROVED_MANIFEST:/data/review_approved_manifest.tsv:ro")
  cmd+=(--review-approved-manifest /data/review_approved_manifest.tsv)
fi

# Tiny explicit container preflight; if this fails the ERR trap writes a durable receipt.
singularity exec "${bind_args[@]}" "$SIF" \
  python -c 'import importlib.metadata as m; print("container_preflight=OK sdrf-pipelines=" + m.version("sdrf-pipelines"))'

singularity exec "${bind_args[@]}" "$SIF" "${cmd[@]}"

[[ -s "$OUT/sdrf_readiness_summary.json" ]] || die "readiness completed without summary: $OUT/sdrf_readiness_summary.json"
[[ -s "$OUT/sdrf_readiness.tsv" ]] || die "readiness completed without TSV: $OUT/sdrf_readiness.tsv"

rm -f "$FAILURE_RECEIPT"
echo "completed $(date -u +%Y-%m-%dT%H:%M:%SZ)"
