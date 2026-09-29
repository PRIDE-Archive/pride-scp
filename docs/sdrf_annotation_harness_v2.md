# PRIDE-SCP SDRF annotation harness v2

## Purpose

Harness v2 moves the deterministic annotation control loop out of operator/chat interaction and into
machine-readable state.  It does **not** change the scientific reconstruction policy.  The existing
resolvers, validators, scientific guards, readiness policy and independent review remain the only
components authorized to modify or promote SDRFs.

Version 2.1 implements the orchestration substrate first:

- immutable run specification;
- content-addressed evidence and candidate identities;
- normalized blocker taxonomy;
- declarative resolver capabilities;
- exact no-progress cache keys;
- per-accession state/decision ledger;
- cohort-level generic implementation threshold;
- compact action/review/evidence-limited/ready queues.

Slurm DAG execution is deliberately deferred until the decision replay is stable.

## Run specification

The harness accepts a JSON run specification.  Relative paths are resolved relative to the run-spec
file.

```json
{
  "schema_version": "pride-scp-sdrf-annotation-run-spec-v1",
  "run_id": "residual64-replay-v1",
  "generic_implementation_threshold": 3,
  "provenance": {
    "git_sha": "<git sha>",
    "sif_path": "/path/to/pinned.sif",
    "sif_sha256": "<sha256>",
    "policy_version": "pride-scp-bigbio-readiness-v0.5.14.8"
  },
  "inputs": {
    "accessions_file": "accessions.txt",
    "candidate_manifest": "candidate_manifest.tsv",
    "blocker_manifest": "blockers.tsv",
    "evidence_registry": "evidence_registry.tsv",
    "attempt_ledger": "resolver_attempts.tsv",
    "resolver_catalog": "resources/sdrf_annotation_resolver_catalog_v1.json"
  }
}
```

`blocker_manifest.tsv` is the normalized adapter boundary.  Required useful columns are:

```text
accession
state or baseline_terminal_state or final_state
reason_code or baseline_reason_code
blocker_fields (semicolon separated)
```

The first v2.1 deployment should generate this adapter from the already-frozen readiness/closure
artifacts rather than changing those artifacts.

## Evidence registry

`sdrf_evidence_registry.py` converts source manifests into content-addressed records.  Presence in a
workspace does not establish source independence.  A source is independent only when its manifest
carries an explicit trusted source class plus an external/deposited locator, or an explicit
`is_independent=true` decision from a prior provenance gate.

Important fields:

```text
accession
artifact_sha256
blocker_field
source_kind
source_provider
source_locator
local_path
parent_artifact_sha256
derivation_operation
trust_class
is_independent
provenance_status
candidate_hash_equal
```

If an evidence SHA equals a candidate SHA without independently retained source provenance, it is
recorded as `circular_or_unproven_self_evidence`.

## No-progress cache

A resolver attempt key is:

```text
SHA256(
  accession
  + candidate_sha256
  + evidence_set_sha256
  + resolver_id
  + resolver_version
  + policy_version
  + blocker_key
)
```

Statuses such as `no_change`, `no_applicable_evidence` and `same_validation_errors` are cached.  The
same stage is never scheduled again until at least one content/version component changes.

This is the principal mechanism for eliminating repeated zero-information closure loops.

## Terminal/action decisions

The v2.1 planner emits exactly these operator-relevant states:

```text
RUN_RESOLVER
SUBMISSION_READY
HUMAN_REVIEW_ACTIONABLE
IMPLEMENTATION_CANDIDATE
PROVENANCE_CONFLICT
EVIDENCE_LIMITED
```

The planner does not make an SDRF submission-ready.  `SUBMISSION_READY` is only a reflection of an
already-frozen readiness result.

`IMPLEMENTATION_CANDIDATE` requires the same exact unsupported blocker field to have independent
source evidence in at least the configured cohort threshold (default 3).  This prevents accession-
specific code from being proposed merely to increase yield.

## Outputs

Each planning run writes:

```text
run_manifest.json
RUN_SUMMARY.md
state_ledger.tsv
action_queue.tsv
resolver_plan.tsv
resolver_attempts.tsv
human_review_queue.tsv
implementation_candidates.tsv
evidence_limited.tsv
submission_ready.tsv
```

The normal operator surfaces are only:

```text
RUN_SUMMARY.md
action_queue.tsv
resolver_plan.tsv
resolver_attempts.tsv
human_review_queue.tsv
implementation_candidates.tsv
submission_ready.tsv
```

## v2.1 validation target

Replay the completed Residual64 campaign from frozen inputs.  The expected scientific result must be
identical to the manually adjudicated result, while the harness must skip already-proven no-progress
resolver/evidence combinations and reach terminal states without conversational decisions.

No change to row mapping, evidence acceptance, projection, validator, readiness or independent-review
semantics is permitted as part of the v2.1 implementation.

## Resolver execution hand-off

`resolver_plan.tsv` is the v2.1/v2.3 boundary.  Every planned row contains the exact immutable
`stage_key`, candidate SHA, evidence-set SHA, resolver/version, blocker key and policy version.  A
future Slurm executor can submit those rows directly and append the terminal attempt status to
`resolver_attempts.tsv`.  A subsequent planner invocation will then suppress exact no-progress
replays automatically.

The repository ships `resources/sdrf_annotation_resolver_catalog_v1.json` so the resolver
capability contract can be reviewed and versioned independently of the planner implementation.  The
built-in catalog is an equivalent fallback used by self-tests and minimal deployments.

## v2.2 — provenance hardening and blocker-directed evidence acquisition

Version 2.2 adds the first submission-yield mechanism on top of the v2.1 control plane.
It still does not fetch data itself and it does not weaken scientific acceptance rules. Instead,
it produces a content-addressed acquisition queue for the existing/future provider adapters.

### Provenance lineage

Evidence registry rows now preserve:

```text
artifact_sha256
accession
source_kind
source_provider
source_locator
retrieved_at
retrieval_method
original_filename
media_type
byte_size
parent_artifact_sha256
derivation_operation
trust_class
independence_class
```

`independence_class` is deterministic and may be:

```text
independent_external
deposited_repository
publication_supplement
derived_from_trusted_source
candidate_derived
provenance_unknown
```

A local workspace path is never sufficient to establish independence. Candidate ancestry fails
closed even after copying/parsing. A parser/materialization derivative may retain independence only
when its parent artifact is registered as independently sourced and the derivation operation is an
approved non-generative transformation.

The evidence-set content hash now includes source locator/provider, independence class, provenance
status, parent SHA and derivation operation. Changing provenance therefore invalidates old resolver
or acquisition stage keys instead of silently reusing stale no-progress state.

### Evidence acquisition section in the run spec

Acquisition is opt-in so historical deterministic replays remain byte-stable:

```json
{
  "evidence_acquisition": {
    "enabled": true,
    "source_catalog": "resources/sdrf_evidence_source_catalog_v1.json",
    "attempt_ledger": "evidence_acquisition_attempts.tsv",
    "max_source_classes_per_accession": 4
  }
}
```

When acquisition is disabled, a case with no applicable independent evidence remains
`EVIDENCE_LIMITED`, exactly as in v2.1.

When enabled, an evidence-limited case is compared against the blocker-directed source catalog. If
an unexhausted strategy exists, its state becomes:

```text
next_action=ACQUIRE_EVIDENCE
terminal_state=<blank>
```

and the exact work is written to:

```text
evidence_acquisition_plan.tsv
```

### Source-class priority

The shipped catalog prefers structured/high-trust sources before text extraction:

```text
10  deposited_sdrf
15  repository_structured_sidecars
20  sample_annotation_table
30  experimental_design_table
40  publication_supplement
50  publication_full_text
```

Strategies are blocker-specific. For example, cell identifiers and row mapping can use design
spreadsheets, while cell-line/Cellosaurus blockers prefer deposited/sample annotation evidence.
Candidate-missing cases first search for deposited SDRFs and repository structured sidecars.

Publication full text is an evidence-discovery source, not directly projectable row evidence. Any
claim extracted from it must still enter the provenance registry and pass normal deterministic
scope/mapping rules before it can affect a candidate.

### Acquisition no-progress cache

Each source-class attempt has its own content-addressed key:

```text
SHA256(
  accession
  + source_class
  + evidence_set_sha256
  + policy_version
  + blocker_key
  + source_catalog_version
)
```

No-progress statuses are:

```text
source_not_found
no_new_artifact
source_exhausted
unsupported_source
provenance_invalid
```

An exhausted source class is skipped for identical inputs. A changed evidence set, policy, blocker
or source strategy version creates a new key and may legally reopen acquisition.

### v2.2 boundaries

v2.2 plans acquisition only. Provider-specific network fetchers, structured parsing execution,
candidate generation and readiness iteration remain existing components or follow-on v2.2c work.
This separation keeps source discovery from becoming an implicit scientific mutation path.

The source-class budget is sequential, not parallel. One planning cycle schedules only the
highest-priority unexhausted source class for an accession. After that acquisition attempt, the
harness is rerun with the updated evidence/attempt ledger. This allows it to stop immediately when a
cheap trusted source closes the blocker instead of downloading lower-priority sources unnecessarily.
`max_source_classes_per_accession` is the total distinct source-class budget for the blocker/catalog
version, not the number of concurrent fetches.
