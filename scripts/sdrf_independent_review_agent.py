#!/usr/bin/env python3
"""Non-editing exact-hash review agent for PRIDE-SCP SDRF closure.

The reviewer can return only approved, held, or insufficient_evidence. Deterministic
prerequisites are checked before any model call. The reviewer cannot write SDRF bytes.
The Ollama request follows the same structured, non-thinking JSON contract used by the
production PRIDE-SCP semantic readers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import urllib.request
from pathlib import Path
from typing import Any

STATUSES = {"approved", "held", "insufficient_evidence"}
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": sorted(STATUSES)},
        "reason": {"type": "string"},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "reason", "evidence_refs"],
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise ValueError(f"expected JSON object: {path}")
    return obj


def evidence_refs(obj: Any) -> set[str]:
    refs: set[str] = set()

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            for k, v in x.items():
                if k in {"evidence_ref", "evidence_refs", "source_ref", "source_refs", "ref", "refs"}:
                    if isinstance(v, str) and v.strip():
                        refs.add(v.strip())
                    elif isinstance(v, list):
                        refs.update(str(y).strip() for y in v if str(y).strip())
                walk(v)
        elif isinstance(x, list):
            for y in x:
                walk(y)

    walk(obj)
    return refs


def prerequisites(
    projected: Path,
    readiness: dict[str, Any],
    validator: dict[str, Any],
    evidence: dict[str, Any],
) -> tuple[bool, list[str]]:
    failures = []
    digest = sha256_file(projected)
    if readiness.get("state") != "needs_independent_review":
        failures.append("readiness_state_not_needs_independent_review")
    if readiness.get("blockers"):
        failures.append("readiness_has_blockers")
    rsha = str(readiness.get("projected_sha256") or "").lower()
    if not rsha or rsha != digest.lower():
        failures.append("projected_sha_mismatch")
    if str(validator.get("candidate_sha256") or "").lower() != digest.lower():
        failures.append("validator_sha_mismatch")
    if validator.get("validator_gate") != "green":
        failures.append("validator_not_green")
    parse = readiness.get("parse_sdrf") or []
    if not parse or not all(isinstance(x, dict) and x.get("passed") is True for x in parse):
        failures.append("readiness_parse_sdrf_not_green")
    skills = readiness.get("skills_check") or {}
    if not isinstance(skills, dict) or skills.get("passed") is not True:
        failures.append("skills_check_not_green")
    if not evidence_refs(evidence):
        failures.append("no_evidence_refs")
    return not failures, failures


def truncate_json_value(value: Any, limit: int = 5000) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "...<truncated>"
    try:
        rendered = json.dumps(value, sort_keys=True, ensure_ascii=False)
    except Exception:
        rendered = str(value)
    if len(rendered) <= limit:
        return value
    return rendered[:limit] + "...<truncated>"


def compact_review_packet(
    readiness: dict[str, Any],
    validator: dict[str, Any],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    parse = []
    for item in readiness.get("parse_sdrf") or []:
        if not isinstance(item, dict):
            continue
        parse.append({
            "label": item.get("label"),
            "passed": item.get("passed"),
            "compatibility_override": item.get("compatibility_override"),
            "compatibility_reason": item.get("compatibility_reason"),
        })
    skills = readiness.get("skills_check") or {}
    normalization = readiness.get("normalization") or {}
    compact_readiness = {
        "state": readiness.get("state"),
        "blockers": readiness.get("blockers") or [],
        "warnings": readiness.get("warnings") or [],
        "projected_sha256": readiness.get("projected_sha256"),
        "rows": readiness.get("rows"),
        "templates": readiness.get("templates") or readiness.get("derived_templates") or [],
        "normalization": {
            "actions": normalization.get("actions") or [],
            "warnings": normalization.get("warnings") or [],
            "blockers": normalization.get("blockers") or [],
        },
        "parse_sdrf": parse,
        "skills_check": {
            "passed": skills.get("passed"),
            "compatibility_override": skills.get("compatibility_override"),
            "compatibility_reason": skills.get("compatibility_reason"),
        },
    }
    compact_validator = {
        "candidate_sha256": validator.get("candidate_sha256"),
        "validator_gate": validator.get("validator_gate"),
        "validator_version": validator.get("validator_version"),
        "runtime_sha256": validator.get("runtime_sha256"),
    }
    compact_items = []
    for item in evidence.get("items") or []:
        if not isinstance(item, dict):
            continue
        ref = str(item.get("evidence_ref") or "").strip()
        if not ref:
            continue
        compact_items.append({
            "evidence_ref": ref,
            "kind": item.get("kind"),
            "path": item.get("path") or item.get("content_path"),
            "sha256": item.get("sha256") or item.get("content_sha256"),
            "content": truncate_json_value(
                item.get("content_excerpt", item.get("content", "")),
                4500,
            ),
        })
        if len(compact_items) >= 18:
            break
    return {
        "readiness": compact_readiness,
        "validator": compact_validator,
        "evidence": compact_items,
    }


def parse_model_json(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("empty reviewer response")
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        obj = json.loads(text[start:end + 1])
    if not isinstance(obj, dict):
        raise ValueError("reviewer response is not a JSON object")
    return obj


def call_model(url: str, model: str, prompt: str, timeout: int) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, 3):
        suffix = "" if attempt == 1 else "\nReturn only the required JSON object. No prose or markdown."
        payload = json.dumps({
            "model": model,
            "prompt": prompt + suffix,
            "stream": False,
            "format": REVIEW_SCHEMA,
            "think": False,
            "keep_alive": "30m",
            "options": {
                "temperature": 0,
                "seed": 42,
                "num_ctx": 32768,
                "num_predict": 384,
            },
        }).encode()
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                outer = json.load(resp)
            if isinstance(outer, dict) and outer.get("error"):
                raise RuntimeError(str(outer.get("error")))
            if isinstance(outer, dict) and "response" in outer:
                raw = outer.get("response")
            else:
                raw = outer
            if isinstance(raw, dict):
                return raw
            if isinstance(raw, str):
                return parse_model_json(raw)
            raise ValueError("unexpected reviewer response envelope")
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"reviewer model returned no valid structured JSON after 2 attempts: {last_error}")


def review(
    accession: str,
    projected: Path,
    readiness: dict[str, Any],
    validator: dict[str, Any],
    evidence: dict[str, Any],
    model: str,
    url: str,
    timeout: int,
    fixture: Path | None = None,
) -> dict[str, Any]:
    digest = sha256_file(projected)
    ok, failures = prerequisites(projected, readiness, validator, evidence)
    if not ok:
        return {
            "schema_version": "pride-scp-exact-hash-review-v2",
            "accession": accession,
            "status": "held",
            "sha256": digest,
            "reason": "deterministic_prerequisite_failed",
            "failures": failures,
            "evidence_refs": sorted(evidence_refs(evidence)),
        }
    if fixture and fixture.is_file():
        decision = read_json(fixture)
    else:
        allowed_refs = sorted(evidence_refs(evidence))
        packet = compact_review_packet(readiness, validator, evidence)
        prompt = (
            "You are a non-editing independent SDRF reviewer. Return one JSON object only. "
            "Allowed status values: approved, held, insufficient_evidence. Do not propose edits. "
            "The JSON must contain status, reason, and evidence_refs. evidence_refs must be a non-empty "
            "list chosen only from the allowed evidence reference IDs supplied below whenever status is approved. "
            "Approve only if the supplied exact projected bytes, readiness diagnostics, validator receipt, and "
            "source evidence support the projection without unresolved scientific contradiction. If the evidence "
            "does not establish that, return held or insufficient_evidence rather than guessing.\n\n"
            f"accession={accession}\nsha256={digest}\n"
            f"allowed_evidence_refs={json.dumps(allowed_refs)}\n"
            f"review_packet={json.dumps(packet, sort_keys=True, ensure_ascii=False)}\n"
        )
        decision = call_model(url, model, prompt, timeout)
    status = str(decision.get("status") or "").strip().lower()
    if status not in STATUSES:
        status = "insufficient_evidence"
    cited = decision.get("evidence_refs") or decision.get("refs") or []
    if isinstance(cited, str):
        cited = [cited]
    allowed = evidence_refs(evidence)
    cited = [str(x) for x in cited if str(x) in allowed]
    if status == "approved" and not cited:
        status = "insufficient_evidence"
    return {
        "schema_version": "pride-scp-exact-hash-review-v2",
        "accession": accession,
        "status": status,
        "sha256": digest,
        "reason": str(decision.get("reason") or ""),
        "evidence_refs": cited,
        "model": model,
        "non_editing": True,
        "structured_json": True,
        "think": False,
    }


def self_test() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        p = root / "x.tsv"
        p.write_text("a\tb\n1\t2\n")
        d = sha256_file(p)
        readiness = {
            "state": "needs_independent_review",
            "blockers": [],
            "projected_sha256": d,
            "parse_sdrf": [{"passed": True}],
            "skills_check": {"passed": True},
        }
        validator = {"candidate_sha256": d, "validator_gate": "green"}
        evidence = {"items": [{"evidence_ref": "E1", "text": "source"}]}
        fixture = root / "decision.json"
        fixture.write_text(json.dumps({"status": "approved", "evidence_refs": ["E1"], "reason": "supported"}))
        out = review("PXD000001", p, readiness, validator, evidence, "fake", "http://invalid", 5, fixture)
        assert out["status"] == "approved" and out["sha256"] == d
        assert parse_model_json('```json\n{"status":"held","reason":"x","evidence_refs":[]}\n```')["status"] == "held"
        packet = compact_review_packet(readiness, validator, evidence)
        assert packet["readiness"]["state"] == "needs_independent_review"

        # Exercise the actual Ollama envelope contract without a model.
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        captured: dict[str, Any] = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode())
                captured.update(payload)
                body = json.dumps({
                    "response": json.dumps({
                        "status": "approved",
                        "reason": "supported",
                        "evidence_refs": ["E1"],
                    })
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        try:
            decision = call_model(
                f"http://127.0.0.1:{server.server_port}/api/generate",
                "fake",
                "review",
                5,
            )
        finally:
            thread.join(timeout=5)
            server.server_close()
        assert decision["status"] == "approved"
        assert captured["think"] is False
        assert isinstance(captured["format"], dict)
    print("sdrf_independent_review_agent self-test: PASS")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--accession")
    p.add_argument("--projected", type=Path)
    p.add_argument("--readiness", type=Path)
    p.add_argument("--validator-receipt", type=Path)
    p.add_argument("--evidence", type=Path)
    p.add_argument("--output", type=Path)
    p.add_argument("--model", default="qwen3.6:27b")
    p.add_argument("--ollama-url", default="http://127.0.0.1:11434/api/generate")
    p.add_argument("--timeout", type=int, default=1200)
    p.add_argument("--fixture", type=Path)
    return p


def main() -> int:
    args = parser().parse_args()
    if args.self_test:
        self_test()
        return 0
    for name in ("accession", "projected", "readiness", "validator_receipt", "evidence", "output"):
        if getattr(args, name, None) in (None, ""):
            raise SystemExit(f"--{name.replace('_', '-')} is required")
    result = review(
        args.accession,
        args.projected,
        read_json(args.readiness),
        read_json(args.validator_receipt),
        read_json(args.evidence),
        args.model,
        args.ollama_url,
        args.timeout,
        args.fixture,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
