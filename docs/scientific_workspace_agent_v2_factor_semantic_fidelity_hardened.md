# Scientific Workspace Agent v2: factor semantic-fidelity hardening

This iteration preserves the frozen FactorGraph Stage-1 architecture and the deterministic factor-to-observation bridge. It adds a second observation-local semantic-fidelity gate for acquisition mode while retaining the existing isolation-method hardening.

Mode:

```text
study_factor_graph_v2_semantic_fidelity_hardened
```

Harness:

```text
pride-scp-scientific-workspace-agent-v2-factor-semantic-fidelity-hardened
```

## Motivation

The broad accepted-factor run exposed a false canonicalization in a heterogeneous acquisition study: an accepted `DDA-PASEF` acquisition factor was canonicalized as `Data-independent acquisition` because evidence-wide acquisition inference did not recognize the hyphenated DDA-PASEF form and other trusted evidence in the same evidence item mentioned DIA-PASEF.

The fix is deliberately local. It does not rerun or reinterpret the factor graph with an LLM.

## Acquisition invariant

For accepted factor observations:

- explicit DDA observations may canonicalize only to DDA;
- explicit DIA observations may canonicalize only to DIA;
- DDA must never become DIA;
- DIA must never become DDA;
- ambiguous or non-acquisition uses of DIA/DDA terminology remain conflict/unresolved;
- `DIA-NN` processing text alone is not treated as proof of DIA acquisition.

If evidence-wide canonicalization conflicts with an unambiguous accepted factor observation, the semantic-fidelity mode uses the accepted factor-local mode and the claim's field-relevant evidence refs. This is allowed only for an explicit DDA/DIA observation. The old canonicalization-hardened mode is unchanged for reproducibility.

## Compiler-boundary protection

Before scientific claims are projected, a concrete project-wide acquisition baseline is masked when accepted factor-scoped acquisition observations do not semantically support it. This prevents an evidence-wide bootstrap value from leaking across heterogeneous DDA/DIA branches.

Isolation hardening remains unchanged and applies in both the old canonicalization-hardened mode and this new semantic-fidelity mode.

## Stop rule

Validate this change first on the frozen four-accession regression set:

- mixed DIA/DDA acquisition bug reproducer;
- explicit DDA control;
- explicit DIA control;
- isolation-hardening non-regression control.

No model calls or conversational tools are permitted. Each accession receives one validator pass. Only after the semantic-fidelity regression gate passes should the deterministic accepted-factor cohort be rerun.
