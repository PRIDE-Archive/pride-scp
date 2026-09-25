#!/usr/bin/env python3
"""Canonicalize SDRF bytes using the frozen PRIDE-SCP/BigBio readiness projection.

This helper deliberately does not invent sample, channel, donor, or biological identity.
It loads the exact readiness implementation supplied at runtime and reuses its
``derive_templates`` + ``normalize_bigbio_projection`` functions so deterministic
resolver/agent output is serialized with the same BigBio 1.1 compatibility contract
that will later gate submission.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "pride-scp-canonical-sdrf-serializer-v1"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def load_readiness(path: Path):
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    parent = str(path.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    spec = importlib.util.spec_from_file_location("_pride_scp_frozen_readiness", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load readiness module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    for name in ("read_sdrf", "derive_templates", "normalize_bigbio_projection"):
        if not hasattr(module, name):
            raise RuntimeError(f"readiness module missing {name}: {path}")
    return module


def canonicalize(candidate: Path, output: Path, report: Path, readiness_script: Path) -> dict[str, Any]:
    module = load_readiness(readiness_script)
    headers, rows = module.read_sdrf(candidate)
    templates = module.derive_templates(headers, rows, [])
    output.parent.mkdir(parents=True, exist_ok=True)
    normalized_headers, normalized_rows, info = module.normalize_bigbio_projection(
        candidate,
        output,
        templates,
    )
    input_sha = sha256_file(candidate)
    output_sha = sha256_file(output)
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "changed" if input_sha != output_sha else "unchanged",
        "changed": input_sha != output_sha,
        "input": str(candidate),
        "output": str(output),
        "input_sha256": input_sha,
        "output_sha256": output_sha,
        "templates": templates,
        "rows": len(normalized_rows),
        "columns": len(normalized_headers),
        "actions": list(getattr(info, "actions", []) or []),
        "warnings": list(getattr(info, "warnings", []) or []),
        "blockers": list(getattr(info, "blockers", []) or []),
        "readiness_policy_version": str(getattr(module, "POLICY_VERSION", "")),
        "readiness_version": str(getattr(module, "VERSION", "")),
        "sdrf_pipelines_pin": str(getattr(module, "SDRF_PIPELINES_PIN", "")),
        "safety": {
            "scientific_identity_inference": False,
            "sample_file_channel_mapping_inference": False,
            "uses_frozen_readiness_projection_contract": True,
        },
    }
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def self_test(readiness_script: Path) -> None:
    module = load_readiness(readiness_script)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / "x.sdrf.tsv"
        out = root / "y.sdrf.tsv"
        rep = root / "report.json"
        src.write_text(
            "source name\tassay name\tcomment[proteomics data acquisition method]\t"
            "characteristics[organism]\tcomment[data file]\tfactor value[condition]\n"
            "s1\ta1\tNT=Data-independent acquisition;AC=PRIDE:0000628\t"
            "Homo sapiens\ta.raw\tcontrol\n",
            encoding="utf-8",
        )
        result = canonicalize(src, out, rep, readiness_script)
        headers, rows = module.read_sdrf(out)
        assert headers.index("characteristics[organism]") < headers.index("assay name")
        assert headers[-1] == "factor value[condition]"
        acq = headers.index("comment[proteomics data acquisition method]")
        assert rows[0][acq] == "Data-independent acquisition"
        assert result["sdrf_pipelines_pin"] == "0.1.6"
    print("sdrf_canonical_serializer self-test: PASS")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--candidate", type=Path)
    p.add_argument("--output", type=Path)
    p.add_argument("--report", type=Path)
    p.add_argument(
        "--readiness-script",
        type=Path,
        default=Path(__file__).resolve().parent / "sdrf_bigbio_readiness.py",
    )
    return p


def main() -> int:
    args = parser().parse_args()
    if args.self_test:
        self_test(args.readiness_script)
        return 0
    for name in ("candidate", "output", "report", "readiness_script"):
        if getattr(args, name, None) is None:
            raise SystemExit(f"--{name.replace('_', '-')} is required")
    result = canonicalize(args.candidate, args.output, args.report, args.readiness_script)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
