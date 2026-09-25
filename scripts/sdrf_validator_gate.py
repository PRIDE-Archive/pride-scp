#!/usr/bin/env python3
"""Exact-byte SDRF validator gate reusing the project's existing sdrf-pipelines path.

This wrapper intentionally does not reimplement SDRF validation. It dynamically loads
scripts/sdrf_bigbio_readiness.py and calls its read_sdrf(), derive_templates(),
validate_parse_sdrf(), and command_dict() helpers. The resulting receipt is bound to
the exact candidate SHA256 and is suitable for validator-gated closure boundaries.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def load_readiness_module(path: Path) -> ModuleType:
    if not path.is_file():
        raise FileNotFoundError(f"readiness script not found: {path}")
    spec = importlib.util.spec_from_file_location("pride_scp_readiness_gate", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load readiness module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    required = ("read_sdrf", "derive_templates", "validate_parse_sdrf", "command_dict")
    missing = [name for name in required if not hasattr(module, name)]
    if missing:
        raise RuntimeError(f"readiness module missing helpers: {missing}")
    return module


def validator_version() -> str:
    for package in ("sdrf-pipelines", "parse-sdrf", "parse_sdrf"):
        try:
            return importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            pass
    return "unknown"


def run_gate(
    *,
    accession: str,
    candidate: Path,
    boundary: str,
    readiness_script: Path,
    parse_sdrf: str,
    ontology_mode: str,
    timeout: int,
    runtime_sha256: str = "",
    validator_version_override: str = "",
) -> dict[str, Any]:
    if not candidate.is_file():
        raise FileNotFoundError(f"candidate not found: {candidate}")
    module = load_readiness_module(readiness_script)
    headers, rows = module.read_sdrf(candidate)
    templates = module.derive_templates(headers, rows, [])
    results = module.validate_parse_sdrf(candidate, templates, parse_sdrf, ontology_mode, timeout)
    encoded = [module.command_dict(result) for result in results]
    available = bool(encoded) and all(bool(r.get("available")) for r in encoded)
    passed = bool(encoded) and all(bool(r.get("passed")) for r in encoded)
    gate = "green" if available and passed else "red"
    return {
        "schema_version": "pride-scp-sdrf-validator-gate-v1",
        "accession": accession.upper(),
        "boundary": boundary,
        "candidate_path": str(candidate),
        "candidate_sha256": sha256_file(candidate),
        "validator_gate": gate,
        "validator_available": available,
        "validator_passed": passed,
        "validator_version": validator_version_override or validator_version(),
        "runtime_sha256": runtime_sha256 or sha256_file(readiness_script),
        "readiness_script": str(readiness_script),
        "parse_sdrf_command": parse_sdrf,
        "ontology_mode": ontology_mode,
        "templates": templates,
        "results": encoded,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }


def self_test() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        candidate = root / "PXD000001.sdrf.tsv"
        candidate.write_text("source name\tcomment[data file]\nS1\ta.raw\n", encoding="utf-8")
        fake = root / "readiness.py"
        fake.write_text(
            """
class R:
    available=True
    passed=True

def read_sdrf(path):
    return ['source name','comment[data file]'], [['S1','a.raw']]

def derive_templates(headers, rows, explicit):
    return ['single-cell','ms-proteomics']

def validate_parse_sdrf(path, templates, command, ontology_mode, timeout):
    return [R(), R()]

def command_dict(x):
    return {'label':'fake','argv':['fake'], 'available':x.available, 'returncode':0, 'passed':x.passed, 'compatibility_override':False, 'compatibility_reason':'', 'stdout':'', 'stderr':''}
""".lstrip(),
            encoding="utf-8",
        )
        receipt = run_gate(
            accession="PXD000001",
            candidate=candidate,
            boundary="self_test",
            readiness_script=fake,
            parse_sdrf="parse_sdrf",
            ontology_mode="skip",
            timeout=5,
            validator_version_override="0.1.6",
        )
        assert receipt["validator_gate"] == "green"
        assert receipt["candidate_sha256"] == sha256_file(candidate)
        assert receipt["templates"] == ["single-cell", "ms-proteomics"]
        assert receipt["validator_version"] == "0.1.6"
    print("sdrf_validator_gate self-test: PASS")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--accession")
    p.add_argument("--candidate", type=Path)
    p.add_argument("--boundary", default="validator_boundary")
    p.add_argument("--output", type=Path)
    p.add_argument("--readiness-script", type=Path, default=Path("scripts/sdrf_bigbio_readiness.py"))
    p.add_argument("--parse-sdrf", default="parse_sdrf")
    p.add_argument("--ontology-mode", choices=["skip", "online"], default="skip")
    p.add_argument("--timeout", type=int, default=180)
    p.add_argument("--runtime-sha256", default="")
    p.add_argument("--validator-version", default="", help="Version reported by the actual validator runtime; overrides host package metadata.")
    p.add_argument("--fail-on-red", action="store_true")
    return p


def main() -> int:
    args = parser().parse_args()
    if args.self_test:
        self_test()
        return 0
    if not args.accession or args.candidate is None or args.output is None:
        raise SystemExit("--accession, --candidate and --output are required")
    receipt = run_gate(
        accession=args.accession,
        candidate=args.candidate,
        boundary=args.boundary,
        readiness_script=args.readiness_script,
        parse_sdrf=args.parse_sdrf,
        ontology_mode=args.ontology_mode,
        timeout=args.timeout,
        runtime_sha256=args.runtime_sha256,
        validator_version_override=args.validator_version,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, indent=2))
    if args.fail_on_red and receipt["validator_gate"] != "green":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
