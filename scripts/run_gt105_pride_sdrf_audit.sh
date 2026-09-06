#!/usr/bin/env bash
set -euo pipefail

# Deterministically audit the usable SDRFs resolved for the source-resolved
# primary-PRIDE subset. This stage never invokes Ollama. v0.2.6 separates
# SDRF/template validity from local PRIDE repository-file linkage.

ROOT="${ROOT:-$(pwd)}"
SNAPSHOT="${SNAPSHOT:-$ROOT/data/snapshot}"
BINARY="${BINARY:-$ROOT/target/release/pride-scp}"
SOURCE_ROOT="${SOURCE_ROOT:-$ROOT/data/sdrf_source_resolution_gt105_pride_v024}"
OUT="${OUT:-$ROOT/data/sdrf_audit_gt105_pride_v026}"

[[ -x "$BINARY" ]] || { echo "missing executable: $BINARY" >&2; exit 2; }
[[ -d "$SNAPSHOT" ]] || { echo "missing snapshot: $SNAPSHOT" >&2; exit 2; }
[[ -s "$SOURCE_ROOT/sdrf_source_resolution.tsv" ]] || {
  echo "missing source-resolution table: $SOURCE_ROOT/sdrf_source_resolution.tsv" >&2
  echo "run ./scripts/run_gt106_pride_sdrf_source_resolution.sh first" >&2
  exit 2
}
[[ -d "$SOURCE_ROOT/resolved" ]] || { echo "missing resolved SDRF directory" >&2; exit 2; }

mkdir -p "$OUT"
python - "$SOURCE_ROOT/sdrf_source_resolution.tsv" "$OUT/resolved_accessions.txt" "$OUT/unresolved_accessions.txt" <<'PY'
import csv, sys
src, resolved_out, unresolved_out = sys.argv[1:]
resolved=[]; unresolved=[]
with open(src, newline='') as fh:
    for row in csv.DictReader(fh, delimiter='\t'):
        acc=row['accession'].strip()
        if row.get('selected_source_kind','').strip() and row.get('resolved_path','').strip():
            resolved.append(acc)
        else:
            unresolved.append(acc)
for path, vals in [(resolved_out,resolved),(unresolved_out,unresolved)]:
    with open(path,'w') as out:
        for v in vals: out.write(v+'\n')
print(f"resolved={len(resolved)} unresolved={len(unresolved)}")
PY

RESOLVED="$OUT/resolved_accessions.txt"
[[ -s "$RESOLVED" ]] || { echo "no resolved SDRFs to audit" >&2; exit 3; }

"$BINARY" sdrf-audit \
  --accessions-file "$RESOLVED" \
  --snapshot "$SNAPSHOT" \
  --resolved-sdrf-dir "$SOURCE_ROOT/resolved" \
  --output "$OUT"

echo "Resolved PRIDE SDRF deterministic audit complete"
echo "  summary:    $OUT/sdrf_audit_summary.json"
echo "  results:    $OUT/sdrf_audit_results.tsv"
echo "  resolved:   $OUT/resolved_accessions.txt"
echo "  unresolved: $OUT/unresolved_accessions.txt"
