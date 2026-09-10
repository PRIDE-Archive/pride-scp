# PRIDE-SCP BigBio 1.1 compatibility projection (v0.5.14.2)

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

## v0.5.14.2 annotation-tool and DIA compatibility hardening

The full-105 readiness audit showed that all 37 internally projectable historical candidates failed
BigBio structural validation on the same legacy `comment[sdrf annotation tool]` value, for example:

```text
pride-scp-sdrf pride-scp-sdrf-v0.3.1
```

BigBio 1.1 expects annotation-tool metadata in `name vX.Y.Z`, `NT=name;VV=vX.Y.Z`, or
`manual curation` form. The readiness normalizer now rewrites only recognized historical PRIDE_SCP
annotation-tool encodings to the equivalent standards-valid representation, for example:

```text
pride-scp-sdrf v0.3.1
```

Unknown/non-PRIDE_SCP annotation-tool values are preserved. The Rust SDRF writer now emits the same
valid form for newly generated PRIDE_SCP drafts so future candidates do not recreate the legacy defect.

The same population audit also exposed a reusable DIA compatibility issue. The exact historical value:

```text
NT=Data-independent acquisition;AC=PRIDE:0000628
```

is normalized to the BigBio DIA 1.1 representation for the same explicitly named method:

```text
NT=Data-independent acquisition;AC=PRIDE:0000450
```

No DDA value is converted to DIA and no acquisition method is inferred from unrelated metadata.

Whole-file `dia-acquisition` template selection is now conservative: all concrete acquisition values
must be DIA-compatible unless the caller explicitly forces that template. A mixed DDA/DIA candidate
therefore does not receive the DIA leaf template merely because one row or stale historical template
declaration mentions DIA.

Scientific blockers remain unchanged and are never synthesized.

