# Changelog

## 0.1.0 — recall-first repository bootstrap

- Added Rust workspace with `pride-scp` CLI.
- Added resumable PRIDE project/file/SDRF snapshotting with bounded Tokio concurrency.
- Added Rayon-backed multi-lane deterministic SCP candidate discovery.
- Added configurable recall-oriented discovery vocabulary.
- Added full `project_discovery_audit.tsv` including score-zero projects.
- Added candidate JSONL evidence bundles with source excerpts.
- Added known-positive recall audit and missed-positive report.
- Added Python bridge export for the existing publication/PDF/Ollama pipeline.
- Added non-destructive repository-evidence Qwen triage for no-PDF candidates.
- Added migration helper to copy the exact current Stage 01–06 Python scripts from the old working tree and hash them.
- Changed architecture so the old Stage 03 publication screen is diagnostic, not a hard gate.
- Added synthetic fixtures testing repository-text and file-manifest discovery recall.
