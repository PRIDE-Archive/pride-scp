# Release20 Repair4 v4 — final two-accession cleavage-agent completeness fix

Date: 2026-09-13

Repair4-v3 deterministic readiness succeeded for PXD019515, PXD019958 and PXD054066, but the fresh
exact-hash independent review approved only PXD054066. The remaining defect in PXD019515 and PXD019958
is narrow and shared: both source workflows used **Lys-C and Trypsin**, while the v3 candidates encoded
Trypsin alone (PXD019515) or Trypsin plus `not applicable` (PXD019958).

This v4 lane is therefore intentionally restricted to:

```text
PXD019515
PXD019958
```

It starts from the exact Repair4-v3 candidate hashes:

```text
PXD019515  37e14990aa7efb213a0ffb716fdd20c9ebbf0baae209bd73bf2e77f36460ac17
PXD019958  d1931cedb30491fa7710b689b2856ca262cefe7b4b4a0b304fb15073b8d35e6d
```

## Scientific correction

Both candidates must encode the two source-supported digestion enzymes:

```text
NT=Trypsin;AC=MS:1001251
NT=Lys-C;AC=MS:1001309
```

Repeated cleavage-agent columns enumerate the enzymes used; their column order is not used as a
workflow-time model. To minimize mutation, the already-correct Trypsin value remains in the first slot.

### PXD019515

The v3 candidate has one cleavage-agent column. v4 inserts one adjacent repeated
`comment[cleavage agent details]` column and populates it with Lys-C. Precursor tolerance,
modifications, biological metadata and mapping are frozen.

### PXD019958

The v3 candidate already has two cleavage-agent columns containing Trypsin and `not applicable`.
v4 replaces only the second value with Lys-C. Modifications and biological/control metadata are frozen.

## Safety invariants

The v4 helper:

- refuses any input whose SHA-256 differs from the exact reviewed v3 candidate;
- fails if any pre-existing non-cleavage field changes;
- requires the only changed field to be `comment[cleavage agent details]`;
- does not touch approved PXD054066 or PXD062702 artifacts;
- does not change v0.5.14.3 compatibility/readiness architecture;
- requires a new projected hash and a new exact-hash independent review before publication.

After local validation, rerun v0.5.14.3 readiness for only these two accessions. PXD054066 and
PXD062702 remain approved and held for coordinated publication.
