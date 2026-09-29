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
