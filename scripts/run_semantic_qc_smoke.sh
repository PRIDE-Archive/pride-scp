#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PACKETS="$ROOT/work/python/semantic_unification/qc_evidence_packets.jsonl"
SMOKE_OUT="$ROOT/work/python/semantic_qc_smoke_v019"
CRITIC_MODEL="${SEMANTIC_QC_CRITIC_MODEL:-phi4-mini:3.8b}"
JURY_MODEL="${SEMANTIC_QC_JURY_MODEL:-gemma3:4b}"
CPU_THREADS="${SEMANTIC_QC_CPU_THREADS:-4}"

python "$ROOT/python/recall/build_semantic_qc_packets.py" \
  --unified-manifest "$ROOT/work/python/semantic_unification/unified_semantic_manifest.jsonl" \
  --annotations-dir "$ROOT/work/python/pride_scp_annotations/annotations" \
  --output "$PACKETS" \
  --summary "$ROOT/work/python/semantic_unification/qc_evidence_packet_summary.json"

rm -rf "$SMOKE_OUT"
python "$ROOT/python/recall/adjudicate_semantic_qc.py" \
  "$PACKETS" \
  --output-dir "$SMOKE_OUT" \
  --critic-model "$CRITIC_MODEL" \
  --jury-model "$JURY_MODEL" \
  --cpu-threads "$CPU_THREADS" \
  --accession PXD000902 \
  --accession PXD028991 \
  --accession PXD000441 \
  --accession PXD049412

printf '\nExpected qualitative smoke behavior:\n'
printf '  PXD000902  include  (individual Xenopus egg proteomics)\n'
printf '  PXD028991  exclude  (many FACS-sorted root-hair cells per proteomic sample)\n'
printf '  PXD000441  exclude  (AML cell-line population proteomics)\n'
printf '  PXD049412  include  (modern genuine SCP)\n'
printf '\nResults:\n  %s\n' "$SMOKE_OUT/semantic_qc_results.tsv"
