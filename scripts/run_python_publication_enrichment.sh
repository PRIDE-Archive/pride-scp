#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python}"
STAGES="$ROOT/python/stages"
BRIDGE="${BRIDGE_DIR:-$ROOT/data/python_bridge}"
WORK="${PYTHON_WORK_DIR:-$ROOT/work/python}"
mkdir -p "$WORK"

for required in \
  pride_scp_pipeline_common.py \
  01_fetch_pride_publications.py \
  02_download_publication_pdfs.py \
  04_run_pride_scp_annotations.py \
  pride_scp_targeted_ollama.py; do
  if [[ ! -f "$STAGES/$required" ]]; then
    echo "ERROR: missing $STAGES/$required" >&2
    echo "Run scripts/import_current_python.sh first." >&2
    exit 1
  fi
done

"$PYTHON" "$STAGES/01_fetch_pride_publications.py" \
  --accessions-file "$BRIDGE/candidate_accessions.txt" \
  --workers "${PUBLICATION_WORKERS:-8}" \
  --contact-email "${CONTACT_EMAIL:-}" \
  --cache-dir "$WORK/pride_project_cache" \
  --output "$WORK/pride_candidate_publications.tsv"

"$PYTHON" "$STAGES/02_download_publication_pdfs.py" \
  "$WORK/pride_candidate_publications.tsv" \
  --pdf-dir "$WORK/publication_pdfs" \
  --workers "${PDF_WORKERS:-4}" \
  --output "$WORK/pride_candidate_publications_with_pdfs.tsv"

cat <<MSG
Publication enrichment complete.

The old deterministic publication screen is intentionally NOT a hard gate.
You may run it separately for diagnostics, but Stage 04 should consume the
unfiltered PDF manifest using --all-valid-pdfs.

Next (long Ollama step):

  $PYTHON $STAGES/04_run_pride_scp_annotations.py \\
    $WORK/pride_candidate_publications_with_pdfs.tsv \\
    --targeted-script $STAGES/pride_scp_targeted_ollama.py \\
    --output-dir $WORK/pride_scp_annotations \\
    --model qwen2.5:3b \\
    --cpu-threads 4 \\
    --workers 1 \\
    --all-valid-pdfs
MSG
