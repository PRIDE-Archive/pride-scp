# PRIDE-SCP scientific annotation agent v0.1

This command is a new harness line introduced after the v0.4-v0.7 post-serialization repair loop plateaued on the frozen mixed8 cohort.

## Command

```text
pride-scp sdrf-agent
```

The legacy `sdrf-annotate` command remains unchanged and reproducible.

## Core design

The scientific agent maintains a persistent workspace before SDRF rows are serialized:

```text
repository/publication/supplement/KG evidence
                    |
                    v
          persistent study-design state
          - branches
          - scoped assertions
          - open questions
          - conflicts
          - action history
                    |
          model chooses next operation
          /          |           \
       search      compile       stop
         |            |            |
         +------------+------------+
                    |
                    v
      deterministic provenance/scope guards
                    |
                    v
           deterministic SDRF compiler
                    |
                    v
                validators
                    |
          feedback to agent state
```

The LLM is allowed to explore hypotheses in its workspace, but only `supported` assertions with valid field-specific evidence can reach the compiler.

## Safety boundary

Rust, not the model, owns:

- accepted acquisition-cardinality scaffold;
- validity of E#### evidence references;
- exact RAW-file linkage;
- project vs branch broadcast safety;
- isolation/acquisition controlled-vocabulary canonicalization;
- the deterministic SDRF row compiler;
- validation and stop budgets.

Filename tokens remain search hints and contradiction detectors only. They never establish biological identity.

## Persistent files

For accession `PXD...` the command writes:

```text
workspaces/PXD.../state.turnNN.json
workspaces/PXD.../state.json
workspaces/PXD.../action_history.json
workspaces/PXD.../validation_history.json
workspaces/PXD.../proposal.json
workspaces/PXD.../trace.json

evidence/PXD....evidence.json
sdrf/PXD....sdrf.tsv
review/PXD....sdrf.review.tsv
audit/PXD....scientific_agent.json
```

## Default bounded loop

```text
max agent turns       12
max evidence actions  20
max validator cycles   3
```

The budgets are global per accession and configurable from the CLI.

## First architecture benchmark

Use four sentinels before returning to mixed8:

```text
PXD035339  isolation/canonicalization
PXD046467  heterogeneous branches/linkage/acquisition
PXD041388  known-good non-regression control
PXD025634  multiorganism fail-closed sentinel
```

Success is architectural, not simply 4/4 validator-green. The run should show that branches/scopes are represented before rows exist, unresolved linkage remains explicit, supported hydrodynamic/capillary isolation can canonicalize before serialization, and the known-good control remains valid.
