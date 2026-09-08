#!/usr/bin/env python3
"""Fail when production SDRF inference code contains accession-specific runtime rules.

Real accession identifiers are allowed only inside self-tests/regression fixtures.  Runtime code may
read/access accession values dynamically, but may not branch on a literal PXD identifier or embed a
literal PXD as scientific configuration.
"""
from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path

PXD_RE = re.compile(r"\bPXD\d{6,}\b", re.I)
DEFAULT_FILES = [
    "scripts/sdrf_generalized_evidence_graph.py",
    "scripts/sdrf_multibranch_evidence_graph.py",
    "scripts/sdrf_multiplex_evidence_graph.py",
    "scripts/sdrf_mapping_evidence_audit.py",
    "scripts/sdrf_reporter_run_scope_audit.py",
    "scripts/sdrf_reporter_design_semantic_audit.py",
    "scripts/sdrf_local_publication_corpus_reconcile.py",
    "scripts/sdrf_publication_accession_recovery.py",
    "scripts/sdrf_residual_external_publication_recovery.py",
    "scripts/sdrf_multiplex_support_asset_audit.py",
]


class Guard(ast.NodeVisitor):
    def __init__(self, path: Path):
        self.path = path
        self.scope: list[str] = []
        self.errors: list[str] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Constant(self, node: ast.Constant) -> None:
        if not isinstance(node.value, str) or not PXD_RE.search(node.value):
            return
        # Synthetic accession literals in self-tests are deliberately permitted.
        if any(name.startswith("self_test") or name.startswith("test_") for name in self.scope):
            return
        self.errors.append(f"{self.path}:{getattr(node, 'lineno', '?')}: runtime PXD literal {node.value!r}")


def check(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    guard = Guard(path); guard.visit(tree)
    return guard.errors


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("files", nargs="*")
    args = p.parse_args()
    root = Path.cwd()
    files = [Path(x) for x in args.files] if args.files else [root / x for x in DEFAULT_FILES]
    errors: list[str] = []
    for path in files:
        errors.extend(check(path))
    if errors:
        print("SDRF generalization guard: FAIL")
        print("\n".join(errors))
        return 1
    print("SDRF generalization guard: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
