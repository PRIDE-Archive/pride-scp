#!/usr/bin/env bash
set -euo pipefail

# Historical compatibility entry point.  The original v0.5.2 accession-specific implementation has
# been retired.  Delegate to the generalized source-evidence graph while deriving only the cohort
# from the prior v0.5.1 graph.  Cohort choice does not alter runtime scientific behavior.
ROOT="${ROOT:-$PWD}"
OUT="${OUT:-$ROOT/data/sdrf_multibranch_evidence_graph_gt105_pride_v052_compat_generalized}"
V051_GRAPH="${V051_GRAPH:-$ROOT/data/sdrf_multiplex_evidence_graph_gt105_pride_v051/audit/multiplex_evidence_graph.tsv}"
ACCESSIONS_FILE="${ACCESSIONS_FILE:-}"

if [[ -z "$ACCESSIONS_FILE" ]]; then
  [[ -f "$V051_GRAPH" ]] || { echo "ERROR: missing prior evidence graph: $V051_GRAPH" >&2; exit 2; }
  mkdir -p "$OUT"
  ACCESSIONS_FILE="$OUT/accessions.txt"
  python - "$V051_GRAPH" "$ACCESSIONS_FILE" <<'PY'
import csv,sys
with open(sys.argv[1], errors='replace') as fh:
    vals=sorted({(r.get('accession') or '').strip().upper() for r in csv.DictReader(fh, delimiter='\t') if (r.get('accession') or '').strip()})
open(sys.argv[2],'w').write('\n'.join(vals)+'\n')
print(f'historical v0.5.2 wrapper: delegated generalized cohort={len(vals)}')
PY
fi

export ROOT OUT ACCESSIONS_FILE
exec "$ROOT/scripts/run_pride_sdrf_generalized_evidence_graph_v053.sh"
