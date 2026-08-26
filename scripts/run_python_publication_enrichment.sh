#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python}"
STAGES="$ROOT/python/stages"
BRIDGE="${BRIDGE_DIR:-$ROOT/data/python_bridge}"
WORK="${PYTHON_WORK_DIR:-$ROOT/work/python}"
MANUAL_PDF_DIR="${MANUAL_PDF_DIR:-$ROOT/manual_pdfs}"
mkdir -p "$WORK" "$MANUAL_PDF_DIR"

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

reuse_args=()

# User-supplied legacy PDF directories are colon-separated, analogous to PATH.
if [[ -n "${LEGACY_PDF_DIRS:-}" ]]; then
  IFS=':' read -r -a legacy_dirs <<< "$LEGACY_PDF_DIRS"
  for dir in "${legacy_dirs[@]}"; do
    [[ -d "$dir" ]] && reuse_args+=(--reuse-pdf-dir "$dir")
  done
fi

# Automatically reuse the conventional PDF directory from the previous
# PRIDE_SCP working tree when it exists. This is only a local optimization;
# nothing is copied into Git.
for dir in \
  "$HOME/Documents/PRIDE_SCP/pride_scp_catalogue_pipeline/publication_pdfs" \
  "$HOME/Documents/PRIDE_SCP/pride_scp_catalogue_pipeline/pride_publication_pdfs"; do
  if [[ -d "$dir" ]]; then
    reuse_args+=(--reuse-pdf-dir "$dir")
  fi
done

manual_manifest_args=()
if [[ -f "$MANUAL_PDF_DIR/manual_pdf_manifest.tsv" ]]; then
  manual_manifest_args+=(
    --manual-pdf-manifest "$MANUAL_PDF_DIR/manual_pdf_manifest.tsv"
  )
fi

"$PYTHON" "$STAGES/02_download_publication_pdfs.py" \
  "$WORK/pride_candidate_publications.tsv" \
  --pdf-dir "$WORK/publication_pdfs" \
  --workers "${PDF_WORKERS:-4}" \
  --manual-pdf-dir "$MANUAL_PDF_DIR" \
  --manual-queue "$WORK/manual_pdf_queue.tsv" \
  --reuse-mode "${PDF_REUSE_MODE:-symlink}" \
  "${reuse_args[@]}" \
  "${manual_manifest_args[@]}" \
  --output "$WORK/pride_candidate_publications_with_pdfs.tsv"

"$PYTHON" "$ROOT/python/recall/partition_semantic_candidates.py" \
  "$BRIDGE/semantic_candidates.jsonl" \
  --pdf-manifest "$WORK/pride_candidate_publications_with_pdfs.tsv" \
  --output-dir "$WORK/semantic_partition"

manual_count=0
if [[ -f "$WORK/manual_pdf_queue.tsv" ]]; then
  manual_count="$(( $(wc -l < "$WORK/manual_pdf_queue.tsv") - 1 ))"
  (( manual_count < 0 )) && manual_count=0
fi

cat <<MSG
Publication enrichment complete.

The old deterministic publication screen is intentionally NOT a hard gate.
Every candidate remains publication-backed or repository-only.

Manual PDF fallback:
  directory: $MANUAL_PDF_DIR
  unresolved publication queue: $WORK/manual_pdf_queue.tsv
  unresolved unique publications: $manual_count

To reuse additional old PDFs without copying them first:
  LEGACY_PDF_DIRS=/path/one:/path/two scripts/run_python_publication_enrichment.sh

Semantic partition:
  publication-backed: $WORK/semantic_partition/publication_backed_candidates.jsonl
  repository-only:    $WORK/semantic_partition/repository_only_candidates.jsonl

Next (long Ollama steps; do not start until the partition is inspected):

Publication-backed:
  $PYTHON $STAGES/04_run_pride_scp_annotations.py \
    $WORK/pride_candidate_publications_with_pdfs.tsv \
    --targeted-script $STAGES/pride_scp_targeted_ollama.py \
    --output-dir $WORK/pride_scp_annotations \
    --model qwen2.5:3b \
    --cpu-threads 4 \
    --workers 1 \
    --all-valid-pdfs

Repository-only (non-destructive triage):
  $PYTHON $ROOT/python/recall/triage_repository_candidates.py \
    $WORK/semantic_partition/repository_only_candidates.jsonl \
    --output-dir $WORK/repository_triage \
    --model qwen2.5:3b \
    --cpu-threads 4
MSG
