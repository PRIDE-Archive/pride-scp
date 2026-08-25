#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_DIR="${1:-$HOME/Documents/PRIDE_SCP/pride_scp_catalogue_pipeline}"
DEST="$ROOT/python/stages"

if [[ ! -d "$SOURCE_DIR" ]]; then
  echo "ERROR: source pipeline directory not found: $SOURCE_DIR" >&2
  exit 1
fi

mkdir -p "$DEST"

required=(
  pride_scp_pipeline_common.py
  01_fetch_pride_publications.py
  02_download_publication_pdfs.py
  04_run_pride_scp_annotations.py
  05_merge_pride_scp_catalogue.py
  06_review_pride_scp_catalogue.py
  pride_scp_targeted_ollama.py
)

for name in "${required[@]}"; do
  if [[ ! -f "$SOURCE_DIR/$name" ]]; then
    echo "ERROR: required current script is missing: $SOURCE_DIR/$name" >&2
    exit 1
  fi
  cp -p "$SOURCE_DIR/$name" "$DEST/$name"
done

# Stage 03 is retained for diagnostics only. Support both historical names.
if [[ -f "$SOURCE_DIR/03_screen_pride_scp_publications.py" ]]; then
  cp -p "$SOURCE_DIR/03_screen_pride_scp_publications.py" \
    "$DEST/03_screen_pride_scp_publications.py"
elif [[ -f "$SOURCE_DIR/03_screen_scp_candidates.py" ]]; then
  cp -p "$SOURCE_DIR/03_screen_scp_candidates.py" \
    "$DEST/03_screen_pride_scp_publications.py"
else
  echo "WARNING: no Stage-03 screen script found; continuing because it is diagnostic only." >&2
fi

if [[ -f "$SOURCE_DIR/requirements.txt" ]]; then
  cp -p "$SOURCE_DIR/requirements.txt" "$ROOT/python/requirements.txt"
fi

# Preserve any current Stage-06 README/config notes without making them required.
for extra in STAGE06_README.md STAGE06_V32_README.md; do
  if [[ -f "$SOURCE_DIR/$extra" ]]; then
    cp -p "$SOURCE_DIR/$extra" "$ROOT/python/$extra"
  fi
done

# If the earlier manual catalogue is present locally, make it immediately usable
# as a positive-control recall benchmark without modifying it.
for candidate in \
  "$SOURCE_DIR/pride_single_cell_proteomics_master_catalogue_2026-08-20.csv" \
  "$SOURCE_DIR/../pride_single_cell_proteomics_master_catalogue_2026-08-20.csv"; do
  if [[ -f "$candidate" ]]; then
    cp -p "$candidate" "$ROOT/benchmarks/known_positives_2026-08-20.csv"
    break
  fi
done

(
  cd "$ROOT"
  find python/stages -maxdepth 1 -type f -name '*.py' -print0 \
    | sort -z \
    | xargs -0 sha256sum > python/STAGE_SOURCES.sha256
)

if ! grep -q 'stage6-qc-v3.2' "$DEST/06_review_pride_scp_catalogue.py"; then
  echo "WARNING: imported Stage 06 does not advertise stage6-qc-v3.2; verify that the source tree is your latest one." >&2
fi

echo "Imported current Python pipeline from: $SOURCE_DIR"
echo "Destination: $DEST"
echo "Source hashes: $ROOT/python/STAGE_SOURCES.sha256"
