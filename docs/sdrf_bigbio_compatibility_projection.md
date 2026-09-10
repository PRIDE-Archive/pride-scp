# PRIDE-SCP BigBio 1.1 compatibility projection (v0.5.14)

This stage converts an already source-closed PRIDE-SCP SDRF candidate into a separate BigBio-1.1
compatibility derivative. The source candidate is immutable and remains the scientific provenance
anchor.

Automatic normalization is restricted to representation and specification metadata:

- move all `characteristics[...]` columns before `assay name`;
- place `factor value[...]` columns last;
- write `comment[sdrf version] = v1.1.0`;
- replace legacy/internal template metadata with versioned BigBio template declarations;
- derive `human` only when every concrete organism row is human;
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
