#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-$PWD}"
OUT="${OUT:-$ROOT/data/scp_global_knowledge_graph_v056}"
SNAPSHOT="${SNAPSHOT:-$ROOT/data/snapshot}"
PUBLICATION_MANIFEST="${PUBLICATION_MANIFEST:-$ROOT/data/sdrf_residual_external_publication_recovery_gt105_pride_v050/publication_recovery/combined_publication_manifest.tsv}"
V053_AUDIT="${V053_AUDIT:-$ROOT/data/sdrf_generalized_evidence_graph_v053/audit}"
ACCESSIONS_FILE="${ACCESSIONS_FILE:-}"
CRAWL_SLAVOV="${CRAWL_SLAVOV:-1}"
SLAVOV_ROOT="${SLAVOV_ROOT:-https://scp.slavovlab.net/}"
SLAVOV_MAX_PAGES="${SLAVOV_MAX_PAGES:-80}"
SLAVOV_MAX_DEPTH="${SLAVOV_MAX_DEPTH:-2}"
SLAVOV_TIMEOUT="${SLAVOV_TIMEOUT:-30}"
SLAVOV_GITHUB_FALLBACK="${SLAVOV_GITHUB_FALLBACK:-1}"
USE_SMALL_LLM="${USE_SMALL_LLM:-1}"
MODEL="${MODEL:-qwen2.5:3b}"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434/api/generate}"
OLLAMA_TIMEOUT="${OLLAMA_TIMEOUT:-1200}"
OLLAMA_NUM_CTX="${OLLAMA_NUM_CTX:-32768}"
LLM_RETRIES="${LLM_RETRIES:-1}"
PUBLICATION_MODE="${PUBLICATION_MODE:-full}"
MAX_PUBLICATION_CHUNKS="${MAX_PUBLICATION_CHUNKS:-24}"
LLM_FORCE="${LLM_FORCE:-0}"
LLM_REQUIRE_ALL_PACKETS="${LLM_REQUIRE_ALL_PACKETS:-1}"
LLM_DRY_RUN="${LLM_DRY_RUN:-0}"

mkdir -p "$OUT"
[[ -d "$SNAPSHOT" ]] || { echo "ERROR: missing snapshot: $SNAPSHOT" >&2; exit 2; }
[[ -f "$PUBLICATION_MANIFEST" ]] || { echo "ERROR: missing publication manifest: $PUBLICATION_MANIFEST" >&2; exit 2; }

# Cohort selection is data only.  Any future source-grounded SCP accession list can be supplied.
if [[ -z "$ACCESSIONS_FILE" ]]; then
  [[ -f "$V053_AUDIT/accession_summary.tsv" ]] || { echo "ERROR: set ACCESSIONS_FILE to a newline-delimited accession list" >&2; exit 2; }
  ACCESSIONS_FILE="$OUT/accessions.txt"
  python - "$V053_AUDIT/accession_summary.tsv" "$ACCESSIONS_FILE" <<'PY'
import csv,sys
rows=list(csv.DictReader(open(sys.argv[1], errors='replace'), delimiter='\t'))
vals=sorted({(r.get('accession') or '').strip().upper() for r in rows if (r.get('accession') or '').strip()})
open(sys.argv[2],'w').write('\n'.join(vals)+'\n')
print(f'derived architecture benchmark cohort from v0.5.3: {len(vals)} accessions')
PY
fi
[[ -f "$ACCESSIONS_FILE" ]] || { echo "ERROR: ACCESSIONS_FILE missing: $ACCESSIONS_FILE" >&2; exit 2; }

python "$ROOT/scripts/check_sdrf_generalization_guard.py"

cat > "$OUT/run_manifest.json" <<JSON
{
  "phase": "SCP_global_knowledge_graph_v056_small_llm_semantic_claims",
  "graph_version": "pride-scp-global-knowledge-graph-v0.2",
  "resolver_version": "pride-scp-kg-resolver-v0.2",
  "semantic_extractor_version": "pride-scp-kg-small-llm-semantic-extractor-v0.1",
  "architectural_changes": [
    "wire the existing small local LLM into the global graph as a constrained semantic source reader rather than an SDRF generator",
    "read accession-associated PRIDE project metadata plus locally resolved manuscript text using provenance-labelled packets",
    "permit only a fixed semantic ontology and forbid model-generated RAW/file/sample/cell/run mappings at both extraction and graph-import boundaries",
    "construct graph node types, source lineages and evidence text deterministically from model-cited passage IDs",
    "cap citation-only claims and reject reporter-channel roles whose cited source does not contain the channel token",
    "treat publication-model and repository-model interpretations as distinct lower-authority evidence families that require independent corroboration before canonical semantic promotion",
    "rerun the conservative v0.5.5 canonical resolver after semantic-claim import"
  ],
  "accessions_file": "$ACCESSIONS_FILE",
  "model": "$MODEL",
  "publication_mode": "$PUBLICATION_MODE",
  "max_publication_chunks": $MAX_PUBLICATION_CHUNKS,
  "crawl_slavov": $([[ "$CRAWL_SLAVOV" == "1" ]] && echo true || echo false),
  "small_llm_enabled": $([[ "$USE_SMALL_LLM" == "1" ]] && echo true || echo false),
  "gt_use": "none in runtime graph construction, semantic extraction, or resolution; benchmark/accession files only choose records to exercise",
  "runtime_accession_specific_rules": false,
  "non_generative": true
}
JSON
cat "$OUT/run_manifest.json"

python "$ROOT/scripts/scp_kg_build.py" \
  --accessions-file "$ACCESSIONS_FILE" \
  --snapshot "$SNAPSHOT" \
  --publication-manifest "$PUBLICATION_MANIFEST" \
  --v053-audit "$V053_AUDIT" \
  --output "$OUT" \
  --rebuild

DB="$OUT/scp_knowledge_graph.sqlite"

if [[ "$CRAWL_SLAVOV" == "1" ]]; then
  args=(
    --db "$DB"
    --output "$OUT"
    --root-url "$SLAVOV_ROOT"
    --max-pages "$SLAVOV_MAX_PAGES"
    --max-depth "$SLAVOV_MAX_DEPTH"
    --timeout "$SLAVOV_TIMEOUT"
  )
  [[ "$SLAVOV_GITHUB_FALLBACK" == "1" ]] && args+=(--github-fallback)
  python "$ROOT/scripts/scp_kg_ingest_slavov.py" "${args[@]}"
fi

LLM_OUT="$OUT/small_llm_semantic_claims"
if [[ "$USE_SMALL_LLM" == "1" ]]; then
  args=(
    --accessions-file "$ACCESSIONS_FILE"
    --snapshot "$SNAPSHOT"
    --publication-manifest "$PUBLICATION_MANIFEST"
    --output "$LLM_OUT"
    --cache-dir "$ROOT/data/scp_semantic_claim_cache"
    --model "$MODEL"
    --ollama-url "$OLLAMA_URL"
    --timeout "$OLLAMA_TIMEOUT"
    --num-ctx "$OLLAMA_NUM_CTX"
    --retries "$LLM_RETRIES"
    --publication-mode "$PUBLICATION_MODE"
    --max-publication-chunks "$MAX_PUBLICATION_CHUNKS"
  )
  [[ "$LLM_FORCE" == "1" ]] && args+=(--force)
  [[ "$LLM_REQUIRE_ALL_PACKETS" == "1" ]] && args+=(--require-all-packets)
  [[ "$LLM_DRY_RUN" == "1" ]] && args+=(--dry-run)
  python "$ROOT/scripts/scp_kg_extract_semantic_claims.py" "${args[@]}"
  if [[ "$LLM_DRY_RUN" != "1" ]]; then
    [[ -f "$LLM_OUT/semantic_claims.jsonl" ]] || { echo "ERROR: semantic claim output missing" >&2; exit 2; }
    python "$ROOT/scripts/scp_knowledge_graph.py" --db "$DB" --import-claims "$LLM_OUT/semantic_claims.jsonl"
  fi
fi

python "$ROOT/scripts/scp_kg_resolve.py" \
  --db "$DB" \
  --output "$OUT/resolution"

python "$ROOT/scripts/scp_knowledge_graph.py" \
  --db "$DB" \
  --export "$OUT/exports" \
  --accessions-file "$ACCESSIONS_FILE"

printf '\nv0.5.6 global SCP knowledge graph + constrained small-LLM semantic extraction complete (non-generative)\n'
printf '  database:           %s\n' "$DB"
printf '  graph exports:      %s\n' "$OUT/exports"
printf '  accession views:    %s\n' "$OUT/exports/accession_views"
printf '  resolution:         %s\n' "$OUT/resolution/kg_resolution_summary.json"
if [[ "$USE_SMALL_LLM" == "1" ]]; then
  printf '  semantic claims:    %s\n' "$LLM_OUT/semantic_claims.jsonl"
  printf '  semantic summary:   %s\n' "$LLM_OUT/semantic_claim_extraction_summary.json"
  printf '  rejected claims:    %s\n' "$LLM_OUT/rejected_claims.jsonl"
fi
if [[ "$CRAWL_SLAVOV" == "1" ]]; then
  printf '  Slavov source:      %s\n' "$OUT/slavov_source_ingest_summary.json"
fi
