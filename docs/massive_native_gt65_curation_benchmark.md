# MassIVE M2-C1: frozen-v19 GT65 curation benchmark

This stage benchmarks the frozen `v19-shadow-2.7.1` curator across all 65 frozen
MassIVE accession-level positives after the accepted M2-B.1 identity repair and
M2-C0 native evidence bridge.

## Separation of concerns

GT is used only in the evaluation layer to select the 65 already-recovered
identities. The JSONL supplied to v19 contains no GT labels or expected decisions.
Identity and evidence come from the accepted source-derived discovery result and
M2-C0 native bridge.

Representative selection is deterministic:

- `native_massive` and `native_and_pxd`: prefer an emitted MSV representation with
  an M2-C0 native evidence sidecar;
- `pxd_alias`: use the emitted PXD candidate;
- any fallback uses the highest-scoring accepted source-derived representation.

The benchmark does **not** change discovery vocabulary, curation prompts, decision
rules, GT196, Stage05, or historical Phi/Gemma QC quarantine.

## Outputs

- `benchmark/massive_gt65_benchmark_candidates.jsonl` — GT-free input to v19.
- `benchmark/massive_gt65_benchmark_manifest.tsv` — evaluation-only mapping of 65
  GT rows to candidate representations.
- `curation/` — fresh frozen-v19 results.
- `evaluation/massive_gt65_curation_decision_audit.tsv` — per-GT-row decisions.
- `evaluation/massive_gt65_reviews.tsv` — review cases.
- `evaluation/massive_gt65_hard_false_negatives.tsv` — automated excludes.
- `m2c1_gt65_curation_benchmark_summary.json` — headline benchmark summary.

Automated `exclude` on a GT-positive row is a hard false negative. `review` is not.
This stage is diagnostic: do not tune v19 until failure modes have been separated
into missing evidence, alias/routing, sample-unit normalization, or model reasoning.
