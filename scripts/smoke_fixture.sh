#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -x target/debug/pride-scp ]]; then
  cargo build -p pride-scp-cli
fi

OUT="${TMPDIR:-/tmp}/pride-scp-smoke-$$"
trap 'rm -rf "$OUT"' EXIT

target/debug/pride-scp discover \
  --snapshot tests/fixtures/snapshot \
  --config config/discovery_terms.json \
  --output "$OUT/discovery" \
  --min-score 1 \
  --expected-positive-count 0

target/debug/pride-scp recall-audit \
  --candidates "$OUT/discovery/candidates.tsv" \
  --known-positives tests/fixtures/known_positives.csv \
  --output "$OUT/recall"

python3 - "$OUT/recall/recall_summary.json" <<'PY'
import json, sys
p=sys.argv[1]
d=json.load(open(p))
assert d["expected_positives"] == 2, d
assert d["recovered_positives"] == 2, d
assert abs(d["recall"] - 1.0) < 1e-12, d
print("Fixture recall smoke test: PASS")
PY
