# Scientific Workspace Agent v2 — factor canonicalization hardening

## Purpose

This experiment keeps the accepted v2 factor graph and deterministic factor-to-observation bridge unchanged while hardening the final scientific canonicalization boundary.

The previous deterministic bridge recovered the intended source-faithful factor observations, but exposed an unsafe evidence-wide isolation mapping: both `Spray voltage injection` and `Manual hydrodynamic pressure loading` were canonicalized to `manual picking`. The bridge itself was correct; the semantic substitution occurred during Rust adjudication.

## Mode

```text
PRIDE_SCP_SCIENTIFIC_AGENT_MODE=study_factor_graph_v2_canonicalization_hardened
```

Harness:

```text
pride-scp-scientific-workspace-agent-v2-factor-canonicalization-hardened
```

## Architecture

```text
accepted factor graph
-> deterministic factor observations
-> observation-local canonicalization fidelity gate
-> Rust adjudication
-> deterministic SDRF projection
-> one validator pass
-> terminal result
```

There are no model calls, search tools, conversational retries, or validator-to-model repair turns.

## Canonicalization fidelity rule

For `isolation_method`, a canonical value is accepted only when the source-faithful observation itself directly supports that semantic mapping. Evidence elsewhere in the accession cannot supply a different isolation operation merely because it is an allowed controlled-vocabulary value.

Hardened mode therefore rejects mappings such as:

```text
Spray voltage injection -> manual picking
Manual hydrodynamic pressure loading -> manual picking
```

and keeps them fail-closed as `template_gap` or `unresolved`, retaining the source-faithful observation/evidence rather than substituting an unrelated allowed value.

An explicit observation such as `manual picking` may still canonicalize to `manual picking`.

The alias surface is deliberately tiny. If a meaning-preserving mapping is not directly established, the system prefers `template_gap` or `unresolved` over an incorrect allowed value.

Acquisition-mode canonicalization is unchanged.

## Scope

The hardening is activated only when the workspace harness is the canonicalization-hardened experimental mode. Historical v1/v2 modes retain their original behavior for reproducibility.

## Frozen arch4 safety gate

The audit requires all previous deterministic-bridge safety properties plus semantic fidelity of every canonical isolation adjudication.

In particular:

- PXD035339 must retain R001 spray-voltage and R002 hydrodynamic observations and neither may canonicalize to `manual picking`.
- PXD046467 capillary microsampling must remain a faithful template-gap/unresolved outcome rather than a surrogate isolation term.
- PXD041388 tDISCO/evDISCO must not be replaced by an unrelated canonical isolation value.
- PXD025634 microwell processing must retain its template gap and must not be forced to `manual picking`.

Broader-cohort expansion remains blocked if any isolation canonicalization is semantically unfaithful.

## Generic Rust tests

Seven tests cover:

- spray-voltage observations cannot canonicalize to `manual picking`;
- hydrodynamic-pressure observations cannot canonicalize to `manual picking`;
- explicit `manual picking` remains canonicalizable;
- capillary microsampling remains fail-closed;
- tDISCO/evDISCO is never promoted to `manual picking`;
- acquisition-mode canonicalization remains unchanged;
- an unfaithful bootstrap isolation value is masked before SDRF projection.

The audit is also tested against a synthetic positive fixture and against a synthetic reproduction of the PXD035339 false-canonicalization bug; the latter must force the safety/progress gates false and `stop_before_broader_cohort=true`.
