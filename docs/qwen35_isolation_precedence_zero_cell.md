# Qwen3.5 structured extraction + isolation precedence + zero-cell controls

This patch makes three generic changes to PRIDE-SCP SDRF reconstruction:

1. Structured Ollama requests explicitly send `think=false` in both Rust and Python.
2. Deterministic single-cell isolation inference no longer uses a fixed FACS-first scan across broad evidence. Candidate methods are ranked by evidence-source specificity and explicit method/action language. Close conflicting methods fail closed instead of locking one dataset-wide value. Explicit eyebrow-hair microdissection and manual hydrodynamic single-cell loading map to the template-compatible `manual picking` value.
3. Empty/zero-cell controls have concrete biological identity fields sanitized to `not applicable` during projection, preventing neighboring study-row identity leakage.

The patch is intentionally generic and does not contain accession-specific IDs or GT truth.

Validation performed before packaging: `git apply --check` against the supplied source-context snapshot. Full Rust formatting/check/test must be run in the user's local repository after applying because the artifact runtime used for packaging does not include a Rust toolchain.
