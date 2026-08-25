#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 -m json.tool config/discovery_terms.json >/dev/null

echo "Discovery configuration JSON: OK"

if compgen -G 'python/stages/*.py' >/dev/null; then
  python3 -m py_compile python/stages/*.py python/recall/*.py
  echo "Python syntax: OK"
else
  python3 -m py_compile python/recall/*.py
  echo "Python recall helper syntax: OK"
  echo "Current pipeline scripts have not yet been imported."
fi

if command -v cargo >/dev/null 2>&1; then
  cargo fmt --all -- --check
  cargo check --workspace --locked 2>/dev/null || cargo check --workspace
  cargo test --workspace --locked 2>/dev/null || cargo test --workspace
  echo "Rust workspace: OK"
else
  echo "WARNING: cargo is not installed; Rust validation skipped." >&2
fi
