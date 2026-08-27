#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON="${PYTHON:-python}"
WORK="${PYTHON_WORK_DIR:-$ROOT/work/python}"
MANUAL_PDF_DIR="${MANUAL_PDF_DIR:-$ROOT/manual_pdfs}"
MANIFEST="${1:-$WORK/pride_candidate_publications_with_content.tsv}"
OUTPUT="${2:-$WORK/manual_manuscripts}"

if [[ ! -f "$MANIFEST" ]]; then
  echo "ERROR: publication-content manifest not found: $MANIFEST" >&2
  echo "Run scripts/run_python_publication_enrichment.sh first." >&2
  exit 1
fi

"$PYTHON" "$ROOT/python/recall/write_missing_manuscript_queue.py" \
  "$MANIFEST" \
  --output-dir "$OUTPUT" \
  --manual-pdf-dir "$MANUAL_PDF_DIR"

echo
echo "Missing-PDF accession list:"
echo "  $OUTPUT/missing_pdf_accessions.txt"
echo "Priority manual-manuscript accession list after XML fallback:"
echo "  $OUTPUT/missing_manuscript_accessions.txt"
echo "Human search/download queue:"
echo "  $OUTPUT/missing_manuscript_publications.tsv"
echo "PXD-to-PDF mapping template:"
echo "  $OUTPUT/manual_pdf_manifest.template.tsv"
echo "Manual PDF directory:"
echo "  $MANUAL_PDF_DIR"
