#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "sdrf_targeted_evidence_recovery.py"
spec = importlib.util.spec_from_file_location("sdrf_targeted_evidence_recovery", SCRIPT)
mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def check(column: str, message: str, expected_field: str, expected_category: str) -> None:
    assert mod.canonical_field(column, message) == expected_field
    assert mod.classify_issue(column, message) == expected_category


check(
    "characteristics[single cell isolation protocol]",
    "single-cell 1.0.0 template does not allow 'not available' for isolation method on a study/single-cell row",
    "single_cell_isolation_method",
    "evidence_search",
)
check(
    "characteristics[cell type]",
    "empty/zero-cell control carries concrete biological identity 'HeLa cell'",
    "cell_type",
    "deterministic_zero_cell",
)
check(
    "characteristics[organism]",
    "PRIDE project exposes multiple source values {Homo sapiens, Mus musculus}, but draft collapses them; row-level source mapping may be unresolved",
    "organism",
    "structured_mapping",
)

assert "FACS" in mod.TARGETED_FIELDS["single_cell_isolation_method"]
assert mod.make_queries("PXD025634", "single_cell_isolation_method", "10.1/x", "A paper")

ok, score, reason = mod.evidence_relevance(
    "single_cell_isolation_method",
    "Individual cells were isolated by FACS and deposited into wells for proteomic preparation.",
)
assert ok and score > 0 and reason == "explicit_isolation_method"

ok, _, reason = mod.evidence_relevance(
    "single_cell_isolation_method",
    "SNX12 | sorting nexin 12 | ENSG00000147164 | 19.3 | 22.4 | 7.1E-145 | 27.5 | 26.8",
)
assert not ok and reason in {"bioinformatics_false_context", "numeric_or_protein_table", "no_explicit_isolation_method"}

ok, _, reason = mod.evidence_relevance(
    "single_cell_isolation_method",
    "The degree sorted circle layout was used for the STRINGdb network with a protein query.",
)
assert not ok and reason == "bioinformatics_false_context"

print("targeted evidence recovery regression: PASS")
