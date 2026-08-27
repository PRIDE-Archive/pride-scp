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
  03_resolve_publication_content.py \
  04_run_pride_scp_annotations.py \
  pride_scp_targeted_ollama.py; do
  if [[ ! -f "$STAGES/$required" ]]; then
    echo "ERROR: missing $STAGES/$required" >&2
    exit 1
  fi
done

"$PYTHON" "$STAGES/01_fetch_pride_publications.py" \
  --accessions-file "$BRIDGE/candidate_accessions.txt" \
  --workers "${PUBLICATION_WORKERS:-8}" \
  --contact-email "${CONTACT_EMAIL:-${NCBI_EMAIL:-}}" \
  --cache-dir "$WORK/pride_project_cache" \
  --output "$WORK/pride_candidate_publications.tsv"

reuse_args=()
if [[ -n "${LEGACY_PDF_DIRS:-}" ]]; then
  IFS=':' read -r -a legacy_dirs <<< "$LEGACY_PDF_DIRS"
  for dir in "${legacy_dirs[@]}"; do
    [[ -d "$dir" ]] && reuse_args+=(--reuse-pdf-dir "$dir")
  done
fi

for dir in \
  "$HOME/Documents/PRIDE_SCP/pride_scp_catalogue_pipeline/publication_pdfs" \
  "$HOME/Documents/PRIDE_SCP/pride_scp_catalogue_pipeline/pride_publication_pdfs"; do
  if [[ -d "$dir" ]]; then
    reuse_args+=(--reuse-pdf-dir "$dir")
  fi
done

manual_manifest_args=()
if [[ -f "$MANUAL_PDF_DIR/manual_pdf_manifest.tsv" ]]; then
  manual_manifest_args+=(--manual-pdf-manifest "$MANUAL_PDF_DIR/manual_pdf_manifest.tsv")
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

"$PYTHON" "$STAGES/03_resolve_publication_content.py" \
  "$WORK/pride_candidate_publications_with_pdfs.tsv" \
  --content-dir "$WORK/publication_content" \
  --workers "${CONTENT_WORKERS:-4}" \
  --contact-email "${CONTACT_EMAIL:-${NCBI_EMAIL:-}}" \
  --output "$WORK/pride_candidate_publications_with_content.tsv"

"$PYTHON" "$ROOT/python/recall/write_missing_manuscript_queue.py" \
  "$WORK/pride_candidate_publications_with_content.tsv" \
  --output-dir "$WORK/manual_manuscripts" \
  --manual-pdf-dir "$MANUAL_PDF_DIR"

"$PYTHON" "$ROOT/python/recall/partition_semantic_candidates.py" \
  "$BRIDGE/semantic_candidates.jsonl" \
  --content-manifest "$WORK/pride_candidate_publications_with_content.tsv" \
  --output-dir "$WORK/semantic_partition"

cat <<MSG
Publication enrichment and content resolution complete.

Every recall candidate remains publication-backed or repository-only.
Publication backing now accepts either:
  - a validated PDF, or
  - normalized Europe-PMC full-text XML.

Manual PDF directory:
  $MANUAL_PDF_DIR

Human manuscript queues:
  missing PDF accessions:
    $WORK/manual_manuscripts/missing_pdf_accessions.txt
  priority manual manuscript accessions after XML fallback:
    $WORK/manual_manuscripts/missing_manuscript_accessions.txt
  publication search/download queue:
    $WORK/manual_manuscripts/missing_manuscript_publications.tsv
  PXD-to-PDF mapping template:
    $WORK/manual_manuscripts/manual_pdf_manifest.template.tsv

To explicitly map downloaded PDFs, copy/edit the template as:
  $MANUAL_PDF_DIR/manual_pdf_manifest.tsv

Then rerun this script. Manual PDFs are validated and reused automatically.

Semantic partition:
  publication-backed: $WORK/semantic_partition/publication_backed_candidates.jsonl
  repository-only:    $WORK/semantic_partition/repository_only_candidates.jsonl

Next long Ollama step (wait until the regenerated partition is inspected):
  $PYTHON $STAGES/04_run_pride_scp_annotations.py \
    $WORK/pride_candidate_publications_with_content.tsv \
    --targeted-script $STAGES/pride_scp_targeted_ollama.py \
    --output-dir $WORK/pride_scp_annotations \
    --model qwen2.5:3b \
    --cpu-threads 4 \
    --workers 1 \
    --all-valid-content

Repository-only triage:
  $PYTHON $ROOT/python/recall/triage_repository_candidates.py \
    $WORK/semantic_partition/repository_only_candidates.jsonl \
    --output-dir $WORK/repository_triage \
    --model qwen2.5:3b \
    --cpu-threads 4
MSG
