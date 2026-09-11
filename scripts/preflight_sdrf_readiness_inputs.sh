#!/usr/bin/env bash
set -Eeuo pipefail

: "${PERSIST_ROOT:?set PERSIST_ROOT}"
: "${SIF:?set SIF}"
: "${ACCESSIONS_FILE:?set ACCESSIONS_FILE}"
: "${GRAPH_DB:?set GRAPH_DB}"
: "${SNAPSHOT:?set SNAPSHOT}"
: "${OUT:?set OUT}"
CANDIDATE_ROOTS="${CANDIDATE_ROOTS:-}"
BIGBIO_SKILLS_ROOT="${BIGBIO_SKILLS_ROOT:-}"
SKILLS_MODE="${SKILLS_MODE:-optional}"

fail() { echo "FAIL: $*" >&2; exit 1; }
ok() { echo "OK: $*"; }

command -v singularity >/dev/null 2>&1 || fail "singularity is not available"
ok "singularity=$(command -v singularity)"

[[ -f "$SIF" ]] || fail "SIF missing: $SIF"
[[ -s "${SIF}.sha256" ]] || fail "SIF checksum missing: ${SIF}.sha256"
expected="$(awk 'NF {print $1; exit}' "${SIF}.sha256")"
actual="$(sha256sum "$SIF" | awk '{print $1}')"
[[ "$expected" =~ ^[0-9a-fA-F]{64}$ ]] || fail "invalid checksum manifest: ${SIF}.sha256"
[[ "${expected,,}" == "${actual,,}" ]] || fail "SIF checksum mismatch expected=$expected actual=$actual"
ok "SIF checksum $actual"

[[ -f "$ACCESSIONS_FILE" ]] || fail "accessions file missing: $ACCESSIONS_FILE"
count="$(awk 'NF {n++} END {print n+0}' "$ACCESSIONS_FILE")"
[[ "$count" -gt 0 ]] || fail "accessions file is empty: $ACCESSIONS_FILE"
ok "accessions=$count ($ACCESSIONS_FILE)"

[[ -f "$GRAPH_DB" ]] || fail "graph DB missing: $GRAPH_DB"
ok "graph DB=$GRAPH_DB"

[[ -d "$SNAPSHOT" ]] || fail "snapshot missing: $SNAPSHOT"
ok "snapshot=$SNAPSHOT"

usable=0
IFS=':' read -r -a roots <<< "$CANDIDATE_ROOTS"
for root in "${roots[@]}"; do
  [[ -z "$root" ]] && continue
  if [[ -d "$root" ]]; then
    usable=$((usable + 1))
    ok "candidate root=$root"
  else
    echo "WARNING: candidate root missing: $root" >&2
  fi
done
[[ "$usable" -gt 0 ]] || fail "no usable candidate roots"

if [[ "$SKILLS_MODE" == "required" ]]; then
  [[ -n "$BIGBIO_SKILLS_ROOT" && -d "$BIGBIO_SKILLS_ROOT" ]] || fail "required sdrf-skills root missing: $BIGBIO_SKILLS_ROOT"
  ok "sdrf-skills=$BIGBIO_SKILLS_ROOT"
fi

mkdir -p "$OUT"

# Use only the minimum bindings needed to prove the container starts and includes the validator.
singularity exec \
  --bind "$ACCESSIONS_FILE:/data/readiness_accessions.txt:ro" \
  --bind "$GRAPH_DB:/data/readiness_graph.sqlite:ro" \
  --bind "$SNAPSHOT:/data/readiness_snapshot:ro" \
  --bind "$OUT:/results/readiness" \
  "$SIF" \
  python -c 'import importlib.metadata as m; print("container_preflight=OK sdrf-pipelines=" + m.version("sdrf-pipelines"))'

ok "readiness launcher inputs are usable"
