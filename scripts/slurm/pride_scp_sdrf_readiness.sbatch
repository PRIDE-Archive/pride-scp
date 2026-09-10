#!/usr/bin/env bash
#SBATCH --job-name=pride-scp-sdrf-ready
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=01:00:00
set -euo pipefail

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

# Candidate absence is a scientific blocker, not an operational launcher failure.
# Filter missing roots. If none remain, pass an explicit empty directory to the existing
# v0.5.13 readiness engine so it emits per-accession blockers normally.
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
if ((${#valid_candidate_roots[@]})); then
  CANDIDATE_ROOTS="$(IFS=:; echo "${valid_candidate_roots[*]}")"
else
  EMPTY_CANDIDATE_ROOT="$OUT/_empty_candidate_root"
  mkdir -p "$EMPTY_CANDIDATE_ROOT"
  valid_candidate_roots=("$EMPTY_CANDIDATE_ROOT")
  CANDIDATE_ROOTS="$EMPTY_CANDIDATE_ROOT"
  echo "WARNING: no usable candidate roots; readiness will report scientific blockers rather than abort" >&2
fi

# Optional skills must not fail Singularity before the readiness process starts.
if [[ -n "$BIGBIO_SKILLS_ROOT" && ! -d "$BIGBIO_SKILLS_ROOT" ]]; then
  if [[ "$SKILLS_MODE" == "required" ]]; then
    echo "ERROR: required sdrf-skills root does not exist: $BIGBIO_SKILLS_ROOT" >&2
    exit 2
  fi
  echo "WARNING: optional sdrf-skills root missing; disabling skills checks: $BIGBIO_SKILLS_ROOT" >&2
  BIGBIO_SKILLS_ROOT=""
fi

sha256sum -c "${SIF}.sha256"

echo "============================================================"
echo "PRIDE-SCP v0.5.13 SDRF readiness / BigBio acceptance gate"
echo "job_id=${SLURM_JOB_ID:-na} node=${SLURMD_NODENAME:-$(hostname)}"
echo "accessions=$ACCESSIONS_FILE"
echo "graph=$GRAPH_DB"
echo "snapshot=$SNAPSHOT"
echo "candidates=$CANDIDATE_ROOTS"
echo "validator_mode=$VALIDATOR_MODE ontology_mode=$ONTOLOGY_MODE skills_mode=$SKILLS_MODE"
echo "skills_root=${BIGBIO_SKILLS_ROOT:-none}"
echo "output=$OUT"
echo "============================================================"

sing_args=()
[[ -n "$BIGBIO_SKILLS_ROOT" ]] && sing_args+=(--bind "$BIGBIO_SKILLS_ROOT:$BIGBIO_SKILLS_ROOT:ro")

cmd=(
  python /opt/pride-scp/scripts/sdrf_bigbio_readiness.py
  --accessions-file "$ACCESSIONS_FILE"
  --graph-db "$GRAPH_DB"
  --snapshot "$SNAPSHOT"
  --output "$OUT"
  --validator-mode "$VALIDATOR_MODE"
  --ontology-mode "$ONTOLOGY_MODE"
  --skills-mode "$SKILLS_MODE"
)

for root in "${valid_candidate_roots[@]}"; do
  cmd+=(--candidate-root "$root")
done
[[ -n "$BIGBIO_SKILLS_ROOT" ]] && cmd+=(--sdrf-skills-root "$BIGBIO_SKILLS_ROOT")
[[ -n "$REVIEW_APPROVED_MANIFEST" ]] && cmd+=(--review-approved-manifest "$REVIEW_APPROVED_MANIFEST")

singularity exec "${sing_args[@]}" "$SIF" "${cmd[@]}"

echo "completed $(date -u +%Y-%m-%dT%H:%M:%SZ)"
