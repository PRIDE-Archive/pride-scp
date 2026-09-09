#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-$PWD}"
OUT="${OUT:-$ROOT/data/scp_global_knowledge_graph_v059}"
SNAPSHOT="${SNAPSHOT:-$ROOT/data/snapshot}"
PUBLICATION_MANIFEST="${PUBLICATION_MANIFEST:-$ROOT/data/sdrf_residual_external_publication_recovery_gt105_pride_v050/publication_recovery/combined_publication_manifest.tsv}"
V053_AUDIT="${V053_AUDIT:-$ROOT/data/sdrf_generalized_evidence_graph_v053/audit}"
ACCESSIONS_FILE="${ACCESSIONS_FILE:-}"
LLM_ACCESSIONS_FILE="${LLM_ACCESSIONS_FILE:-}"
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
OLLAMA_NUM_CTX="${OLLAMA_NUM_CTX:-8192}"
OLLAMA_NUM_PREDICT="${OLLAMA_NUM_PREDICT:-3072}"
OLLAMA_NUM_THREAD="${OLLAMA_NUM_THREAD:-0}"
OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:-30m}"
LLM_RETRIES="${LLM_RETRIES:-1}"
PUBLICATION_MODE="${PUBLICATION_MODE:-semantic}"
MAX_PUBLICATION_CHUNKS="${MAX_PUBLICATION_CHUNKS:-6}"
SEMANTIC_EXCERPT_CHARS="${SEMANTIC_EXCERPT_CHARS:-2800}"
LLM_MAX_PACKET_CHARS="${LLM_MAX_PACKET_CHARS:-8000}"
LLM_MAX_PASSAGES_PER_PACKET="${LLM_MAX_PASSAGES_PER_PACKET:-3}"
LLM_MAX_CLAIMS="${LLM_MAX_CLAIMS:-20}"
LLM_MAX_PACKETS="${LLM_MAX_PACKETS:-0}"
LLM_FORCE="${LLM_FORCE:-0}"
LLM_REUSE_LEGACY_CACHE="${LLM_REUSE_LEGACY_CACHE:-1}"
LLM_LEGACY_SUPPRESS_REEXTRACT="${LLM_LEGACY_SUPPRESS_REEXTRACT:-0}"
LLM_SEED="${LLM_SEED:-42}"
LLM_REQUIRE_ALL_PACKETS="${LLM_REQUIRE_ALL_PACKETS:-1}"
LLM_DRY_RUN="${LLM_DRY_RUN:-0}"
RESUME="${RESUME:-0}"
BASE_GRAPH_FROM="${BASE_GRAPH_FROM:-}"

mkdir -p "$OUT"
[[ -d "$SNAPSHOT" ]] || { echo "ERROR: missing snapshot: $SNAPSHOT" >&2; exit 2; }
[[ -f "$PUBLICATION_MANIFEST" ]] || { echo "ERROR: missing publication manifest: $PUBLICATION_MANIFEST" >&2; exit 2; }

# Cohort selection is data only. Any future source-grounded SCP accession list can be supplied.
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
if [[ -z "$LLM_ACCESSIONS_FILE" ]]; then
  LLM_ACCESSIONS_FILE="$ACCESSIONS_FILE"
fi
[[ -f "$LLM_ACCESSIONS_FILE" ]] || { echo "ERROR: LLM_ACCESSIONS_FILE missing: $LLM_ACCESSIONS_FILE" >&2; exit 2; }

python "$ROOT/scripts/check_sdrf_generalization_guard.py"

cat > "$OUT/run_manifest.json" <<JSON
{
  "phase": "SCP_global_knowledge_graph_v059_reproducible_grounded_small_llm_semantic_claims",
  "graph_version": "pride-scp-global-knowledge-graph-v0.2",
  "resolver_version": "pride-scp-kg-resolver-v0.2",
  "semantic_extractor_version": "pride-scp-kg-small-llm-semantic-extractor-v0.3",
  "architectural_changes": [
    "replace full-manuscript brute-force model reading with high-recall semantic evidence retrieval and explicit retrieval coverage auditing",
    "shrink CPU-LLM packets and context while bounding structured claim output",
    "constrain evidence_refs in the JSON schema to exact packet-local passage IDs",
    "use fixed seeded greedy generation settings and include them in packet-cache identity/provenance",
    "normalize legacy decorated evidence refs only when literal packet IDs are present",
    "split explicit reporter-channel lists deterministically while requiring every token in cited source text",
    "import validated legacy claims without suppressing re-extraction after prompt/schema upgrades by default",
    "checkpoint claims, rejections and packet timing after every completed packet",
    "print per-packet progress, input size, cache state and model wall time",
    "allow the global graph accession cohort and expensive LLM accession cohort to be selected independently",
    "allow direct resume from an existing base graph without rebuilding/crawling community sources"
  ],
  "accessions_file": "$ACCESSIONS_FILE",
  "llm_accessions_file": "$LLM_ACCESSIONS_FILE",
  "model": "$MODEL",
  "publication_mode": "$PUBLICATION_MODE",
  "max_publication_chunks": $MAX_PUBLICATION_CHUNKS,
  "semantic_excerpt_chars": $SEMANTIC_EXCERPT_CHARS,
  "llm_max_packet_chars": $LLM_MAX_PACKET_CHARS,
  "llm_max_claims": $LLM_MAX_CLAIMS,
  "ollama_num_ctx": $OLLAMA_NUM_CTX,
  "ollama_num_predict": $OLLAMA_NUM_PREDICT,
  "llm_seed": $LLM_SEED,
  "resume": $([[ "$RESUME" == "1" ]] && echo true || echo false),
  "crawl_slavov": $([[ "$CRAWL_SLAVOV" == "1" ]] && echo true || echo false),
  "small_llm_enabled": $([[ "$USE_SMALL_LLM" == "1" ]] && echo true || echo false),
  "gt_use": "none in runtime graph construction, semantic extraction, or resolution; benchmark/accession files only choose records to exercise",
  "runtime_accession_specific_rules": false,
  "non_generative": true
}
JSON
cat "$OUT/run_manifest.json"

DB="$OUT/scp_knowledge_graph.sqlite"
BASE_READY=0
if [[ "$RESUME" == "1" && -f "$DB" ]]; then
  echo "resume mode: reusing existing v0.5.9 base graph: $DB"
  BASE_READY=1
elif [[ -n "$BASE_GRAPH_FROM" ]]; then
  [[ -f "$BASE_GRAPH_FROM" ]] || { echo "ERROR: BASE_GRAPH_FROM missing: $BASE_GRAPH_FROM" >&2; exit 2; }
  cp -f "$BASE_GRAPH_FROM" "$DB"
  echo "reused base graph from: $BASE_GRAPH_FROM"
  BASE_READY=1
fi

if [[ "$BASE_READY" != "1" ]]; then
  python "$ROOT/scripts/scp_kg_build.py" \
    --accessions-file "$ACCESSIONS_FILE" \
    --snapshot "$SNAPSHOT" \
    --publication-manifest "$PUBLICATION_MANIFEST" \
    --v053-audit "$V053_AUDIT" \
    --output "$OUT" \
    --rebuild

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
else
  echo "base graph ready; skipping KG rebuild and Slavov crawl"
fi

LLM_OUT="$OUT/small_llm_semantic_claims"
if [[ "$USE_SMALL_LLM" == "1" ]]; then
  args=(
    --accessions-file "$LLM_ACCESSIONS_FILE"
    --snapshot "$SNAPSHOT"
    --publication-manifest "$PUBLICATION_MANIFEST"
    --output "$LLM_OUT"
    --cache-dir "$ROOT/data/scp_semantic_claim_cache"
    --model "$MODEL"
    --ollama-url "$OLLAMA_URL"
    --timeout "$OLLAMA_TIMEOUT"
    --num-ctx "$OLLAMA_NUM_CTX"
    --num-predict "$OLLAMA_NUM_PREDICT"
    --num-thread "$OLLAMA_NUM_THREAD"
    --seed "$LLM_SEED"
    --keep-alive "$OLLAMA_KEEP_ALIVE"
    --retries "$LLM_RETRIES"
    --publication-mode "$PUBLICATION_MODE"
    --max-publication-chunks "$MAX_PUBLICATION_CHUNKS"
    --semantic-excerpt-chars "$SEMANTIC_EXCERPT_CHARS"
    --max-packet-chars "$LLM_MAX_PACKET_CHARS"
    --max-passages-per-packet "$LLM_MAX_PASSAGES_PER_PACKET"
    --max-claims "$LLM_MAX_CLAIMS"
    --max-packets "$LLM_MAX_PACKETS"
  )
  [[ "$LLM_FORCE" == "1" ]] && args+=(--force)
  [[ "$LLM_REUSE_LEGACY_CACHE" != "1" ]] && args+=(--no-reuse-legacy-cache)
  [[ "$LLM_LEGACY_SUPPRESS_REEXTRACT" == "1" ]] && args+=(--legacy-cache-suppresses-reextract)
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

printf '\nv0.5.9 reproducible grounded global SCP knowledge graph + small-LLM semantic extraction complete (non-generative)\n'
printf '  database:           %s\n' "$DB"
printf '  graph exports:      %s\n' "$OUT/exports"
printf '  accession views:    %s\n' "$OUT/exports/accession_views"
printf '  resolution:         %s\n' "$OUT/resolution/kg_resolution_summary.json"
if [[ "$USE_SMALL_LLM" == "1" ]]; then
  printf '  packet plan:        %s\n' "$LLM_OUT/packet_plan.tsv"
  printf '  semantic claims:    %s\n' "$LLM_OUT/semantic_claims.jsonl"
  printf '  semantic summary:   %s\n' "$LLM_OUT/semantic_claim_extraction_summary.json"
  printf '  rejected claims:    %s\n' "$LLM_OUT/rejected_claims.jsonl"
fi
if [[ "$CRAWL_SLAVOV" == "1" && "$BASE_READY" != "1" ]]; then
  printf '  Slavov source:      %s\n' "$OUT/slavov_source_ingest_summary.json"
fi
