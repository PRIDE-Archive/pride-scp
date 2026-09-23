# Trusted full-SDRF Stage1 fallback

## Purpose

The row-role-hardened deterministic path normally requires an accepted v2 FactorGraph Stage1 artifact before compilation. A small residual class already has a trusted deposited SDRF with complete repository coverage and strong structured single-cell proteomics metadata, but Stage1 ends in `human_review` / minimum-safe-graph failure.

This fallback handles only that class. It does **not** weaken Stage1 for de-novo reconstruction and does not change the global M+R policy.

## Activation

The ordinary accepted-FactorGraph route is unchanged.

The fallback is considered only when:

1. the Stage1 decision artifact exists, parses under the frozen factor-Stage1 harness, matches the accession, and has `status != accepted`; and
2. `PRIDE_SCP_TRUSTED_FULL_SDRF_ELIGIBILITY_MANIFEST` points to an explicit hash-bound TSV authorization manifest.

Missing, malformed, wrong-version, or accession-mismatched Stage1 artifacts remain hard failures rather than activating the fallback.

## Eligibility manifest

Required columns:

```text
accession
candidate_sha256
candidate_type
coverage_class
trusted_source
structured_scp_evidence
non_proteomic_conflict
eligibility
```

Required values for an eligible row:

```text
candidate_type=sdrf
coverage_class=full_repository_coverage
trusted_source=trusted_local_deposited_sdrf
structured_scp_evidence=strong_structured_scp_evidence
non_proteomic_conflict=none
eligibility=eligible
```

There must be exactly one row for the accession.

The manifest is an authorization/provenance record only. Runtime independently rechecks the candidate.

## Runtime fail-closed checks

The candidate must be the exact `<resolved-sdrf-dir>/<PXD>.sdrf.tsv` selected by `build_evidence()` as the preservation-first existing SDRF.

Runtime then requires:

- the existing bidirectional repository-coverage gate still passes;
- every deposited `comment[data file]` row resolves to exactly one current repository RAW after the existing archive-wrapper alias normalization;
- every repository RAW is represented at least once;
- the runtime SHA-256 exactly equals the manifest SHA-256;
- explicit structured biological evidence is present;
- explicit structured SCP/sample-role evidence is present;
- explicit proteomics / mass-spectrometry branch evidence is present;
- explicit non-proteomic `technology type` branches fail closed.

Ambiguous RAW aliases fail. The code never uses filename semantics to infer biology, isolation, acquisition, or branch membership.

## Compilation contract

The fallback creates no synthetic FactorGraph and performs no LLM/tool call.

The trusted full deposited SDRF enters the existing preservation-first compiler with:

```text
0 model calls
0 tool actions
1 validator cycle
```

The ordinary validators and scientific guards remain required.

## Preservation guard

After deterministic compilation but before a candidate is accepted as output, Rust compares the deposited input with the compiled table.

Required invariants:

- row count must not change;
- the original header prefix must remain in the same order;
- every concrete deposited value in an original column must remain byte-identical, except the ordinary SDRF version / annotation-tool stamps;
- missing or `not available` deposited isolation/acquisition cells must not become concrete values;
- explicit deposited isolation/acquisition values, including `not applicable`, must remain unchanged.

Therefore the path cannot manufacture isolation or acquisition metadata merely to satisfy validators.

If the preservation guard fails, the accession fails closed before downstream readiness.

## Non-goals

This fallback does not:

- make partial SDRFs eligible;
- use XLSX/CSV/vendor designs;
- bypass an absent/corrupt/mismatched Stage1 artifact;
- create an accepted FactorGraph;
- infer RAW-to-biological mappings from filenames;
- overwrite deposited scientific values;
- weaken BigBio validation/readiness;
- weaken scientific guards;
- change frozen Tier1 architecture for other accessions.

## First bounded cohort

The first experiment is limited to the five previously audited full-coverage trusted SDRFs:

```text
PXD003121
PXD043473
PXD045844
PXD051942
PXD067623
```

Eligibility remains data-driven and hash-bound; no accession identifiers are embedded in runtime source logic.
