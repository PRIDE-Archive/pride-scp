# Residual85 mapping11 v0.5.14.6 — bounded sdrf-skills compatibility

Policy version: `pride-scp-bigbio-readiness-v0.5.14.6`

This iteration does not mutate SDRF values. It adds a fail-closed compatibility layer around
`sdrf-skills tools check` for three upstream checker defects demonstrated by exact issue objects in the
frozen mapping11 cohort.

The readiness gate reruns a compact `detect_hallucinations()` diagnostic only when `tools check` fails.
An override is allowed only when every reported issue matches one of these generic signatures:

1. `comment[label]` is a verified structured `PRIDE:*` term, while the checker reports only
   `expected MS / actual PRIDE`.
2. `characteristics[cell type]` or `factor value[cell type]` is a verified structured `CLO:*` term,
   while the checker reports only `expected CL,BTO / actual CLO`.
3. The checker reports its known static-name defect for `UNIMOD:199` exactly as
   `Dimethyl:2H(4) -> Label:13C(6)15N(2)` while keeping the accession `UNIMOD:199` on both sides.

The override is refused if any hallucination, true label mismatch, unknown ontology finding, or unknown
UNIMOD swap is present. Raw checker output and the compact diagnostic report are retained in the
readiness receipt, and the source/projected SDRF bytes are unchanged by this compatibility logic.

This policy is accession-independent and must not be extended to make a study-specific annotation pass.
