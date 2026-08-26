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

# v0.1.6 owns these resolver/enrichment files locally.  Preserve them on
# subsequent imports so an older source tree cannot silently undo the new PDF
# resolver.  They are still imported during initial bootstrap if absent.
protected=(
  pride_scp_pipeline_common.py
  01_fetch_pride_publications.py
  02_download_publication_pdfs.py
)

copy_if_unprotected_or_missing() {
  local name="$1"
  local is_protected=0
  for protected_name in "${protected[@]}"; do
    if [[ "$name" == "$protected_name" ]]; then
      is_protected=1
      break
    fi
  done
  if [[ $is_protected -eq 1 && -f "$DEST/$name" ]]; then
    echo "Preserving repository-owned resolver file: python/stages/$name"
    return
  fi
  cp -p "$SOURCE_DIR/$name" "$DEST/$name"
}

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
  copy_if_unprotected_or_missing "$name"
done

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

for extra in STAGE06_README.md STAGE06_V32_README.md; do
  if [[ -f "$SOURCE_DIR/$extra" ]]; then
    cp -p "$SOURCE_DIR/$extra" "$ROOT/python/$extra"
  fi
done

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

if ! grep -q 'pride-scp-v0.1.6' "$DEST/02_download_publication_pdfs.py"; then
  echo "WARNING: Stage 02 does not advertise the v0.1.6 resolver patch." >&2
fi

echo "Imported current Python pipeline from: $SOURCE_DIR"
echo "Destination: $DEST"
echo "Source hashes: $ROOT/python/STAGE_SOURCES.sha256"
