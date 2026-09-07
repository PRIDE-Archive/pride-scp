#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-$PWD}"
BIN="${BIN:-$ROOT/target/release/pride-scp}"
SNAPSHOT="${SNAPSHOT:-$ROOT/data/snapshot}"
ANNOTATIONS="${ANNOTATIONS:-$ROOT/work/python/pride_scp_annotations/annotations}"
RESOLVED_SDRF_DIR="${RESOLVED_SDRF_DIR:-$ROOT/data/sdrf_source_resolution_gt105_pride_v024/resolved}"
V041="${V041:-$ROOT/data/sdrf_mapping_relation_recheck_gt105_pride_v041}"
PUB_MANIFEST="${PUB_MANIFEST:-$V041/publication_manifest_with_text.tsv}"
V042C="${V042C:-$ROOT/data/sdrf_mapping_role_recheck_gt105_pride_v042c}"
REPORTER_AUDIT="${REPORTER_AUDIT:-$V042C/audit/sdrf_mapping_evidence_audit.tsv}"
V043A="${V043A:-$ROOT/data/sdrf_reporter_run_scope_audit_gt105_pride_v043a}"
SUPPORT_XLSX="${SUPPORT_XLSX:-$V043A/audit/support_files/Choi_2021_Patch_proteomics_experimental_design.xlsx}"
FILES_JSON="${FILES_JSON:-$SNAPSHOT/files/PXD028040.json}"
MANIFEST_OUT="${MANIFEST_OUT:-$ROOT/data/sdrf_explicit_mapping_manifest_gt105_pride_v044/audit}"
OUT="${OUT:-$ROOT/data/sdrf_explicit_mapping_reconstruction_gt105_pride_v044}"
MODEL="${MODEL:-qwen2.5:3b}"

for f in "$FILES_JSON" "$SUPPORT_XLSX" "$REPORTER_AUDIT" "$PUB_MANIFEST"; do
  if [[ ! -f "$f" ]]; then
    echo "ERROR: required v0.4.4 source input not found: $f" >&2
    exit 2
  fi
done
if [[ ! -x "$BIN" ]]; then
  echo "ERROR: pride-scp binary not executable: $BIN" >&2
  echo "Build the v0.4.4 overlay first with: cargo build --release --locked" >&2
  exit 2
fi

mkdir -p "$MANIFEST_OUT" "$OUT"

cat > "$OUT/run_manifest.json" <<JSON
{
  "phase": "GT105_PRIDE_SDRF_v044_source_grounded_explicit_mapping_reconstruction",
  "generator_version": "pride-scp-sdrf-v0.4.4",
  "accession": "PXD028040",
  "publication_manifest": "$PUB_MANIFEST",
  "support_workbook": "$SUPPORT_XLSX",
  "reporter_audit": "$REPORTER_AUDIT",
  "architectural_changes": [
    "derive the single-neuron acquisition cohort from the deposited workbook section rather than RAW filename branch words",
    "require one unique repository RAW per explicit workbook acquisition-date + SC-code key",
    "preserve the workbook's three DA-neuron biological identities and technical replicate measurements 1-3",
    "serialize the explicit TMT128 analytical label and TMT131 carrier channel for manifest-authorized rows only",
    "allow a multiplexed design to pass normal row validation only when a source-grounded explicit row manifest is supplied",
    "leave unrelated development/control RAWs outside the reconstructed single-neuron branch rather than fabricating mappings"
  ],
  "gt_use": "accession cohort seed/evaluation only",
  "runtime_sdrf_gt_metadata_used": false,
  "runtime_sdrf_gt_labels_used": false
}
JSON
cat "$OUT/run_manifest.json"

python "$ROOT/scripts/sdrf_reporter_design_manifest.py" \
  --accession PXD028040 \
  --files-json "$FILES_JSON" \
  --support-xlsx "$SUPPORT_XLSX" \
  --reporter-audit-tsv "$REPORTER_AUDIT" \
  --output "$MANIFEST_OUT"

ROW_MANIFEST="$MANIFEST_OUT/explicit_row_mapping_manifest.tsv"
if [[ ! -f "$ROW_MANIFEST" ]]; then
  echo "ERROR: explicit row-mapping manifest was not created: $ROW_MANIFEST" >&2
  exit 2
fi

python - "$ROW_MANIFEST" <<'PY'
import csv, sys
from collections import Counter, defaultdict
p = sys.argv[1]
with open(p) as fh:
    rows = list(csv.DictReader(fh, delimiter="\t"))
if len(rows) != 9:
    raise SystemExit(f"ERROR: expected 9 explicit PXD028040 mapping rows, found {len(rows)}")
if {r["accession"] for r in rows} != {"PXD028040"}:
    raise SystemExit("ERROR: manifest accession contract changed")
if {r["label"] for r in rows} != {"TMT128"} or {r["carrier_channel"] for r in rows} != {"TMT131"}:
    raise SystemExit("ERROR: reporter layout contract changed")
by_sample = defaultdict(list)
for row in rows:
    by_sample[row["source_name"]].append(int(row["technical_replicate"]))
expected = {"DA_neuron_1": [1,2,3], "DA_neuron_2": [1,2,3], "DA_neuron_3": [1,2,3]}
actual = {k: sorted(v) for k, v in by_sample.items()}
if actual != expected:
    raise SystemExit(f"ERROR: biological/technical replicate contract changed: {actual}")
print("\nv0.4.4 explicit mapping manifest acceptance")
print("rows=9 samples=3 technical_replicates_per_sample=3 analytical=TMT128 carrier=TMT131")
for row in rows:
    print(f"{row['raw_file']} -> {row['source_name']} techrep={row['technical_replicate']} ref={row['design_ref']}")
PY

"$BIN" sdrf-annotate \
  --accession PXD028040 \
  --snapshot "$SNAPSHOT" \
  --resolved-sdrf-dir "$RESOLVED_SDRF_DIR" \
  --annotations-dir "$ANNOTATIONS" \
  --publication-manifest "$PUB_MANIFEST" \
  --explicit-row-mapping-manifest "$ROW_MANIFEST" \
  --output "$OUT" \
  --model "$MODEL" \
  --max-evidence-items 128 \
  --max-evidence-chars 60000 \
  --max-files-in-prompt 48 \
  --force

python - "$OUT" <<'PY'
import csv, json, sys
from collections import Counter, defaultdict
from pathlib import Path
root = Path(sys.argv[1])
result_path = root / "sdrf_annotation_results.tsv"
with result_path.open() as fh:
    results = list(csv.DictReader(fh, delimiter="\t"))
if len(results) != 1:
    raise SystemExit(f"ERROR: expected one result row, found {len(results)}")
r = results[0]
print("\nv0.4.4 PXD028040 deterministic reconstruction result")
print(json.dumps(r, indent=2))

sdrf_path = root / "sdrf" / "PXD028040.sdrf.tsv"
with sdrf_path.open() as fh:
    rows = list(csv.DictReader(fh, delimiter="\t"))
print(f"sdrf_rows={len(rows)}")
if len(rows) != 9:
    raise SystemExit(f"ERROR: expected 9 generated biological rows, found {len(rows)}")
by_source = defaultdict(list)
for row in rows:
    by_source[row["source name"]].append(row["comment[technical replicate]"])
    print(
        f"{row['comment[data file]']} source={row['source name']} bio={row.get('characteristics[biological replicate]','-')} "
        f"tech={row['comment[technical replicate]']} label={row['comment[label]']} "
        f"carrier={row.get('comment[carrier channel]','-')} cell={row.get('characteristics[cell identifier]','-')}"
    )
print("sample_replicate_structure=", {k: sorted(v) for k, v in by_source.items()})

review_path = root / "review" / "PXD028040.sdrf.review.tsv"
errors = []
warnings = []
if review_path.is_file():
    with review_path.open() as fh:
        for issue in csv.DictReader(fh, delimiter="\t"):
            (errors if issue["level"] == "error" else warnings).append(issue)
print("error_codes=", Counter(x["code"] for x in errors))
print("warning_codes=", Counter(x["code"] for x in warnings))
if any(x["code"] == "sample_to_channel_mapping_unresolved" for x in errors):
    raise SystemExit("ERROR: explicit mapping reconstruction still reports unresolved sample/channel mapping")
if any(x["code"] == "data_file_not_in_pride_raw_inventory" for x in errors):
    raise SystemExit("ERROR: explicit mapping reconstruction created an ungrounded RAW mapping")

audit_path = root / "audit" / "PXD028040.sdrf.audit.json"
if audit_path.is_file():
    audit = json.loads(audit_path.read_text())
    print(
        "audit_explicit_mapping_rows=", audit.get("explicit_row_mapping_rows"),
        "generation_mode=", audit.get("generation_mode"),
        "completeness=", audit.get("completeness_status"),
        "locally_valid=", audit.get("locally_valid")
    )
PY

printf '\nv0.4.4 explicit mapping reconstruction complete\n'
printf '  manifest: %s\n' "$ROW_MANIFEST"
printf '  results:  %s\n' "$OUT/sdrf_annotation_results.tsv"
printf '  draft:    %s\n' "$OUT/sdrf/PXD028040.sdrf.tsv"
printf '  review:   %s\n' "$OUT/review/PXD028040.sdrf.review.tsv"
printf '  audit:    %s\n' "$OUT/audit/PXD028040.sdrf.audit.json"
