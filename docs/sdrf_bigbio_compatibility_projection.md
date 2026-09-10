# PRIDE-SCP BigBio 1.1 compatibility projection (v0.5.14.1)

This stage converts an already source-closed PRIDE-SCP SDRF candidate into a separate BigBio-1.1
compatibility derivative. The source candidate is immutable and remains the scientific provenance
anchor.

Automatic normalization is restricted to representation and specification metadata:

- move all `characteristics[...]` columns before `assay name`;
- place `factor value[...]` columns last;
- write `comment[sdrf version] = v1.1.0`;
- replace legacy/internal template metadata with versioned BigBio template declarations;
- derive `human` only when every concrete organism value across every `characteristics[organism]` column is human;
- add `not available` for missing human age/sex/disease fields because those sentinels are permitted
  by the BigBio human template.

The normalizer never invents biological replicate, sample identity, file mapping, channel mapping,
label chemistry, cleavage agent, instrument, or acquisition method. BigBio-required scientific values
that remain missing or placeholders are reported as `blocked_metadata_incomplete` before external
validation.

Outputs include the immutable source SHA-256, normalized/projected SHA-256, normalization actions and
blockers. Independent-review approval must match the normalized projected SHA-256.

External `parse_sdrf` or `sdrf-skills` validation findings are scientific readiness states and do not
make the overall batch operationally fail. A non-zero process status is reserved for a required tool
being unavailable.

## v0.5.14.1 whole-file organism-template hardening

Whole-file organism template derivation now consumes every duplicate `characteristics[organism]`
column, not only the first occurrence. This closes a generic legacy-layout failure mode in which a
mixed/non-human candidate could be misclassified as all-human when an earlier duplicate organism
column contained only human values. The source candidate remains read-only; only the separate
hash-tracked compatibility derivative is written.

Readiness telemetry now states `candidate_rewritten_by_gate=false`,
`source_candidate_mutated_by_gate=false`, and `normalized_derivative_created_by_gate=true` so the
immutability contract is explicit.
