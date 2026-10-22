#!/usr/bin/env bash
# Codon compute-node container smoke. Submit with sbatch, not bash on login.
# No model weights, data mutation, or JCode agent action in this smoke test.
# Example: sbatch --cpus-per-task=2 --mem=8G --time=00:20:00 \
#          --output=/nfs/research/juan/DIA/singj/pride-scp/logs/jcode-smoke-%j.log \
#          --export=ALL,SIF=/nfs/research/juan/DIA/singj/pride-scp/containers/pride-scp-jcode.sif \
#          scripts/slurm/pride_scp_jcode_sif_smoke.sbatch

if [ -z "${SIF:-}" ] || [ ! -s "$SIF" ]; then
    echo 'FAIL SIF is unset or missing' >&2
    false
else
    echo "SIF=$SIF"
    sha256sum "$SIF"
    FAIL=0
    for COMMAND in 'jcode --version' 'ollama --version' 'pride-scp --version' \
      'parse_sdrf validate-sdrf --help'; do
        echo "COMMAND=$COMMAND"
        if singularity exec --cleanenv "$SIF" /bin/bash -c "$COMMAND"; then
            echo 'PASS'
        else
            echo 'FAIL' >&2
            FAIL=1
        fi
    done
    echo "SIF_SMOKE_FAIL_COUNT=$FAIL"
    if [ "$FAIL" -ne 0 ]; then
        false
    fi
fi
