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
: "${CANDIDATE_ROOTS:?set colon-separated CANDIDATE_ROOTS}"
: "${OUT:?set OUT}"

VALIDATOR_MODE="${VALIDATOR_MODE:-required}"
ONTOLOGY_MODE="${ONTOLOGY_MODE:-skip}"
SKILLS_MODE="${SKILLS_MODE:-optional}"
BIGBIO_SKILLS_ROOT="${BIGBIO_SKILLS_ROOT:-}"
REVIEW_APPROVED_MANIFEST="${REVIEW_APPROVED_MANIFEST:-}"

mkdir -p "$OUT"
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

IFS=':' read -r -a roots <<< "$CANDIDATE_ROOTS"
for root in "${roots[@]}"; do
  [[ -n "$root" ]] && cmd+=(--candidate-root "$root")
done
[[ -n "$BIGBIO_SKILLS_ROOT" ]] && cmd+=(--sdrf-skills-root "$BIGBIO_SKILLS_ROOT")
[[ -n "$REVIEW_APPROVED_MANIFEST" ]] && cmd+=(--review-approved-manifest "$REVIEW_APPROVED_MANIFEST")

singularity exec "${sing_args[@]}" "$SIF" "${cmd[@]}"

echo "completed $(date -u +%Y-%m-%dT%H:%M:%SZ)"
