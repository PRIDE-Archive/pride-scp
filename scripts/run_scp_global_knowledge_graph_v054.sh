#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-$PWD}"
OUT="${OUT:-$ROOT/data/scp_global_knowledge_graph_v054}"
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
LLM_CLAIMS_JSONL="${LLM_CLAIMS_JSONL:-}"

mkdir -p "$OUT"
[[ -d "$SNAPSHOT" ]] || { echo "ERROR: missing snapshot: $SNAPSHOT" >&2; exit 2; }
[[ -f "$PUBLICATION_MANIFEST" ]] || { echo "ERROR: missing publication manifest: $PUBLICATION_MANIFEST" >&2; exit 2; }

# Cohort selection is input only; no accession changes graph inference behavior.  For the current
# architectural benchmark, derive the accessions from v0.5.3 when the caller does not provide a list.
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
  "phase": "SCP_global_knowledge_graph_v054",
  "graph_version": "pride-scp-global-knowledge-graph-v0.1",
  "architectural_changes": [
    "introduce one provenance-aware global single-cell-proteomics knowledge/evidence graph",
    "represent accessions as scoped graph views rather than independent duplicated knowledge stores",
    "separate raw source claims from canonical accepted facts/edges",
    "import v0.5.3 branch/file calls only as hypotheses, not graph truth",
    "collapse repeated PSM/spectrum join rows before graph ingestion",
    "add a constrained JSONL claim interface for the existing small-LLM semantic extractor",
    "ingest scp.slavovlab.net as an additional community knowledge/auditing source without auto-promoting its claims to SDRF truth",
    "retain repository metadata and RAW inventories as authoritative primary-source edges"
  ],
  "accessions_file": "$ACCESSIONS_FILE",
  "slavov_root": "$SLAVOV_ROOT",
  "crawl_slavov": $([[ "$CRAWL_SLAVOV" == "1" ]] && echo true || echo false),
  "small_llm_claim_import": $([[ -n "$LLM_CLAIMS_JSONL" ]] && echo true || echo false),
  "gt_use": "none in runtime graph construction; benchmark/accession files only choose records to exercise",
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

if [[ -n "$LLM_CLAIMS_JSONL" ]]; then
  [[ -f "$LLM_CLAIMS_JSONL" ]] || { echo "ERROR: LLM_CLAIMS_JSONL not found: $LLM_CLAIMS_JSONL" >&2; exit 2; }
  python "$ROOT/scripts/scp_knowledge_graph.py" --db "$DB" --import-claims "$LLM_CLAIMS_JSONL"
fi

python "$ROOT/scripts/scp_knowledge_graph.py" \
  --db "$DB" \
  --export "$OUT/exports" \
  --accessions-file "$ACCESSIONS_FILE"

printf '\nv0.5.4 global SCP knowledge graph complete (non-generative)\n'
printf '  database:         %s\n' "$DB"
printf '  graph exports:    %s\n' "$OUT/exports"
printf '  accession views:  %s\n' "$OUT/exports/accession_views"
printf '  build summary:    %s\n' "$OUT/scp_knowledge_graph_build_summary.json"
if [[ "$CRAWL_SLAVOV" == "1" ]]; then
  printf '  Slavov source:    %s\n' "$OUT/slavov_source_ingest_summary.json"
fi
