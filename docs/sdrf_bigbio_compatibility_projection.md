# PRIDE-SCP BigBio 1.1 compatibility projection (v0.5.14.4)

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

## v0.5.14.3 specification-first contract and validator-drift handling

The authoritative contract for this stage is the published SDRF-Proteomics v1.1.0 specification:

- https://sdrf.quantms.org/specification.html
- https://sdrf.quantms.org/specification.html#_single_cell
- DIA template definition: section 14.11
- single-cell template definition: section 14.12

The readiness gate now encodes the stable representation rules that caused repeated integration
failures instead of learning them accession by accession:

1. `comment[sdrf version]` is `v1.1.0`.
2. file-level metadata uses lowercase SDRF column names and canonical lowercase reserved words.
3. `comment[sdrf annotation tool]` uses `name vX.Y.Z` (or another format explicitly allowed by the
   specification), not an internal generator token.
4. `comment[sdrf template]` declares selected **leaf templates only**; inherited parent templates are
   implied by the specification. Parent and leaf validators are still run as separate subprocesses.
5. single-cell projections require `characteristics[single cell isolation protocol]` and
   `characteristics[cell identifier]`; missing scientific values remain blockers and are never filled.
6. source-candidate acquisition values preserve the specification's preferred ontology encoding. The
   v0.5.14.4 publication derivative may use a validator-compatible serialization without modifying the
   immutable source candidate.
7. exact known HCD legacy encodings are canonicalized to the specification-allowed short label `HCD`;
   no fragmentation method is inferred. Current SDRF 1.1 documentation identifies MS:1000422 as the
   canonical HCD concept and explicitly allows the short label.

### sdrf-pipelines 0.1.6 DIA template drift

The project pins `sdrf-pipelines[ontology]==0.1.6`, currently the latest PyPI release available to the
portable container. Its vendored `dia-acquisition/1.1.0` template is known to have drifted from the
upstream SDRF 1.1 template while retaining the same version label:

https://github.com/bigbio/sdrf-pipelines/issues/345

That bug rejects the specification-recommended value:

```text
NT=Data-independent acquisition;AC=PRIDE:0000450
```

even though both the current specification and upstream DIA 1.1 template list it as valid.

In v0.5.14.3, PRIDE_SCP did **not** rewrite the correct SDRF to a bare value and instead used a narrow
compatibility override. Release20 upstream CI showed that this still left otherwise publishable PRs red.
As of v0.5.14.4 the immutable source candidate remains unchanged, but the publication derivative emits
the validator-compatible bare DIA literal. The old exact-signature override remains only as a fallback
for diagnostics when validating a non-normalized artifact. That fallback is accepted **only when all of
the following hold**:

- the failure contains exactly the documented stale-template error and no other validator error;
- the file independently satisfies the local SDRF 1.1 DIA value/cardinality contract;
- the structural and other template validators remain independently enforced.

The readiness JSON records `compatibility_override=true` and a reason containing issue 345. Any other
DIA failure remains blocking. This makes the exception narrow, explicit, auditable, and removable once
a released sdrf-pipelines version resynchronizes its bundled template.

### Regression policy

Every compatibility rule above has a self-test. A future change must fail tests if it would reintroduce:

- legacy PRIDE_SCP annotation-tool strings;
- the obsolete PRIDE:0000628 DIA accession;
- parent+child template declarations where the parent is inherited;
- non-lowercase SDRF reserved words;
- known noncanonical HCD encodings;
- mutating the immutable source candidate solely because of validator issue 345; or
- broad validator waivers that are not tied to an exact known upstream defect.



## v0.5.14.4 release20-derived publication hardening

Release20 upstream CI established that the pinned `sdrf-pipelines` DIA leaf validator accepts only the
bare literal `Data-independent acquisition` even when the immutable source candidate uses a valid
NT/AC representation. The publication projection therefore serializes already-explicit DIA methods as
`Data-independent acquisition` while preserving the source candidate/hash and recording the
normalization action. This includes the known generic DIA NT/AC values and explicit diaPASEF/SWATH DIA
spellings; non-DIA acquisition methods are never converted.

Additional generic hardening learned from independent review is applied before publication hashes are
frozen:

- exact zero-cell controls (`empty`/`blank`/`negative control`, `cell identifier=empty`,
  `cells per well=0`) clear only cell-specific identity fields to `not applicable`; contextual
  organism/organism-part/disease metadata is retained;
- the currently unsupported `study sample` validator literal is projected fail-closed as
  `not available` rather than discovered only in upstream CI;
- the scientific guard is duplicate-header safe and blocks repeated identical cleavage-agent or
  modification values, preventing repeated-column chemistry defects from reaching independent review;
- the Rust generator emits validator-compatible DIA for newly generated drafts, so future candidates
  do not recreate the release20 compatibility failure.

These are generic rules. They do not encode accession-specific values and they do not relax the
source-grounding or independent-review requirements. Missing chemistry, mapping, biological identity,
or other scientific evidence remains fail-closed.
