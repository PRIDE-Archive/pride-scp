#!/usr/bin/env bash
# CI smoke: installed executables, imports, offline validator CLI, JCode CLI.
# No model is downloaded and no production research data is used.
IMAGE="${1:-pride-scp-jcode:ci}"
FAIL=0
check() {
    LABEL="$1"
    shift
    echo "===== ${LABEL} ====="
    if docker run --rm --entrypoint /bin/bash "$IMAGE" -c "$*"; then
        echo "PASS ${LABEL}"
    else
        echo "FAIL ${LABEL}" >&2
        FAIL=1
    fi
}
check jcode 'command -v jcode && jcode --version'
check ollama 'command -v ollama && ollama --version'
check rust_science 'command -v pride-scp && pride-scp --version'
check validator 'command -v parse_sdrf && parse_sdrf validate-sdrf --help | grep -F -- "--sdrf_file" && parse_sdrf validate-sdrf --help | grep -F -- "--use_ols_cache_only"'
check python_package 'python -c "import importlib.metadata as m; assert m.version(\"sdrf-pipelines\") == \"0.1.6\"; print(\"sdrf-pipelines=0.1.6\")"'
check jcode_help 'jcode run --help'
echo "IMAGE_SMOKE_FAIL_COUNT=${FAIL}"
if [ "$FAIL" -ne 0 ]; then
    false
fi
