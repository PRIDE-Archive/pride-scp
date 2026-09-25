#!/usr/bin/env python3
"""Validator-gated cohort closure orchestrator for PRIDE-SCP SDRFs.

This controller treats bridge-v2 states as dispatch queues rather than terminal labels.
It keeps the frozen readiness policy as the only submission authority and runs the
existing sdrf-pipelines validator after every candidate mutation.

Generic closure lanes:
  bridge-v2
    -> exact Cellosaurus resolution for ontology blockers
    -> bounded public evidence enrichment + explicit structured row/channel mapping
    -> exact projected-byte independent review
    -> SHA-bound approval manifest
    -> frozen approved readiness
    -> final exact-byte validator

No resolver may waive a red validator, overwrite a conflicting concrete value, infer
sample identity from row order/filenames, or bypass frozen readiness.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

VERSION = "pride-scp-validator-gated-closure-v2"


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


def safe_json(path: Path) -> dict[str, Any]:
    try:
        return read_json(path)
    except Exception:
        return {}


def write_accessions(path: Path, vals: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{x}\n" for x in vals), encoding="utf-8")


def parse_accessions(path: Path) -> list[str]:
    vals = []
    for line in path.read_text(errors="replace").splitlines():
        m = re.search(r"\bPXD\d{6}\b", line, re.I)
        if m:
            vals.append(m.group(0).upper())
    return sorted(set(vals))


def run(cmd: list[str], log: Path | None = None, env: dict[str, str] | None = None) -> int:
    cp = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    if log:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(cp.stdout or "", encoding="utf-8")
    else:
        sys.stdout.write(cp.stdout or "")
    return cp.returncode


def find_candidate(root: Path, acc: str) -> Path | None:
    direct = root / f"{acc}.sdrf.tsv"
    if direct.is_file():
        return direct
    found = sorted(root.rglob(f"{acc}.sdrf.tsv")) if root.is_dir() else []
    return found[0] if found else None


def read_tsv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
        return [dict(x) for x in csv.DictReader(fh, delimiter="\t")]


def detect_validator_runtime(args: argparse.Namespace) -> tuple[str, str]:
    version = (args.validator_version or "").strip()
    runtime_sha = (args.validator_runtime_sha256 or "").strip()
    if args.readiness_sif and args.readiness_sif.is_file():
        if not runtime_sha:
            runtime_sha = sha256_file(args.readiness_sif)
        if not version:
            cmd = [
                args.singularity,
                "exec",
                str(args.readiness_sif),
                args.readiness_python,
                "-c",
                "import importlib.metadata as m; print(m.version('sdrf-pipelines'))",
            ]
            cp = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if cp.returncode == 0:
                version = (cp.stdout or "").strip().splitlines()[-1].strip()
    return version or "unknown", runtime_sha


def validator_gate(args: argparse.Namespace, acc: str, candidate: Path, boundary: str, out: Path) -> dict[str, Any]:
    receipt = out / "validator_receipts" / acc / f"{boundary}.json"
    cmd = [
        args.python,
        str(args.validator_gate_script),
        "--accession", acc,
        "--candidate", str(candidate),
        "--boundary", boundary,
        "--output", str(receipt),
        "--readiness-script", str(args.readiness_script),
        "--parse-sdrf", args.parse_sdrf,
        "--ontology-mode", "skip",
        "--validator-version", args._validator_version,
    ]
    if args._validator_runtime_sha256:
        cmd += ["--runtime-sha256", args._validator_runtime_sha256]
    rc = run(cmd, log=out / "logs" / acc / f"validator_{boundary}.log")
    if rc not in (0, 2) or not receipt.is_file():
        return {
            "accession": acc,
            "boundary": boundary,
            "candidate_sha256": sha256_file(candidate),
            "validator_gate": "red",
            "validator_version": args._validator_version,
            "runtime_sha256": args._validator_runtime_sha256,
            "infrastructure_error": f"validator_rc={rc}",
        }
    return read_json(receipt)


def append_validation(rows: list[dict[str, Any]], receipt: dict[str, Any], stage: str) -> None:
    rows.append({
        "accession": receipt.get("accession", ""),
        "stage": stage,
        "boundary": receipt.get("boundary", ""),
        "candidate_sha256": receipt.get("candidate_sha256", ""),
        "validator_gate": receipt.get("validator_gate", "red"),
        "validator_version": receipt.get("validator_version", ""),
        "runtime_sha256": receipt.get("runtime_sha256", ""),
    })


def run_bridge(args: argparse.Namespace, accs: list[str], candidate_root: Path, seed: Path | None, stage: Path) -> int:
    af = stage / "accessions.txt"
    write_accessions(af, accs)
    cmd = [
        args.python, str(args.autorepair_script),
        "--accessions-file", str(af),
        "--snapshot", str(args.snapshot),
        "--annotations-dir", str(args.annotations_dir),
        "--candidate-root", str(candidate_root),
        "--stage1-root", str(args.stage1_root),
        "--skills-root", str(args.skills_root),
        "--output", str(stage / "autorepair"),
        "--model", args.model,
        "--ollama-url", args.ollama_url,
        "--timeout", str(args.timeout),
        "--max-repair-rounds", str(args.bridge_repair_rounds),
        "--max-agent-turns", str(args.max_agent_turns),
        "--max-tool-actions", str(args.max_tool_actions),
        "--max-validator-cycles", str(args.max_validator_cycles),
        "--singularity", args.singularity,
        "--pride-scp", args.pride_scp,
        "--python", args.python,
        "--readiness-python", args.readiness_python,
    ]
    if seed is not None and seed.is_dir():
        cmd += ["--seed-readiness-dir", str(seed)]
    if args.publication_manifest and args.publication_manifest.is_file():
        cmd += ["--publication-manifest", str(args.publication_manifest)]
    if args.agent_sif:
        cmd += ["--agent-sif", str(args.agent_sif)]
    if args.readiness_sif:
        cmd += ["--readiness-sif", str(args.readiness_sif)]
    return run(cmd, log=stage / "bridge.log")


def terminal_from_bridge(row: dict[str, str]) -> str:
    return row.get("final_state") or "unsupported_or_exhausted"


def copy_candidate(src: Path, dest_root: Path, acc: str) -> Path:
    dest_root.mkdir(parents=True, exist_ok=True)
    dest = dest_root / f"{acc}.sdrf.tsv"
    shutil.copy2(src, dest)
    return dest


def readiness_json_for_bridge(row: dict[str, str]) -> Path | None:
    value = (row.get("readiness_path") or "").strip()
    p = Path(value) if value else None
    return p if p and p.is_file() else None


def locate_projected(stage: Path, acc: str, digest: str) -> Path | None:
    digest = (digest or "").lower()
    preferred = [
        stage / "autorepair" / "preflight" / "readiness" / "projected" / acc / f"{acc}.sdrf.tsv",
    ]
    for p in preferred:
        if p.is_file() and (not digest or sha256_file(p).lower() == digest):
            return p
    for p in sorted((stage / "autorepair").rglob(f"{acc}.sdrf.tsv")):
        if "projected" not in p.parts:
            continue
        if not digest or sha256_file(p).lower() == digest:
            return p
    return None


def text_snippets(path: Path, limit: int = 12000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    if len(text) <= limit:
        return text
    keywords = re.compile(r"single[- ]cell|proteom|isolation|FACS|cellenONE|TMT|reporter|carrier|DIA|DDA|cell line|replicate", re.I)
    pieces = []
    used: set[tuple[int, int]] = set()
    for m in keywords.finditer(text):
        a = max(0, m.start() - 450)
        b = min(len(text), m.end() + 850)
        key = (a, b)
        if key in used:
            continue
        used.add(key)
        pieces.append(text[a:b])
        if sum(len(x) for x in pieces) >= limit:
            break
    return "\n---\n".join(pieces)[:limit] if pieces else text[:limit]


def projection_diff(candidate: Path, projected: Path) -> dict[str, Any]:
    try:
        with candidate.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
            a = list(csv.reader(fh, delimiter="\t"))
        with projected.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
            b = list(csv.reader(fh, delimiter="\t"))
    except Exception as exc:
        return {"error": repr(exc)}
    if not a or not b:
        return {"error": "empty_candidate_or_projection"}
    ah, bh = a[0], b[0]
    changed_columns: Counter[str] = Counter()
    if len(a) == len(b):
        ai = {x: i for i, x in enumerate(ah)}
        bi = {x: i for i, x in enumerate(bh)}
        for name in sorted(set(ai) & set(bi)):
            count = 0
            for ra, rb in zip(a[1:], b[1:]):
                av = ra[ai[name]] if ai[name] < len(ra) else ""
                bv = rb[bi[name]] if bi[name] < len(rb) else ""
                if av != bv:
                    count += 1
            if count:
                changed_columns[name] = count
    return {
        "candidate_sha256": sha256_file(candidate),
        "projected_sha256": sha256_file(projected),
        "candidate_rows": max(0, len(a) - 1),
        "projected_rows": max(0, len(b) - 1),
        "candidate_columns": ah,
        "projected_columns": bh,
        "changed_column_counts": dict(changed_columns),
    }


def build_review_evidence(args: argparse.Namespace, acc: str, candidate: Path, projected: Path, readiness_path: Path, output: Path, extra_evidence_root: Path | None = None) -> Path:
    items: list[dict[str, Any]] = []

    def add_json(ref: str, path: Path) -> None:
        if path.is_file():
            obj = safe_json(path)
            if obj:
                items.append({"evidence_ref": ref, "kind": "json", "path": str(path), "sha256": sha256_file(path), "content": obj})

    add_json("readiness", readiness_path)
    audit = candidate.with_name(f"{acc}.sdrf_audit.json")
    add_json("candidate_audit", audit)
    add_json("snapshot_project", args.snapshot / "projects" / f"{acc}.json")

    files_path = args.snapshot / "files" / f"{acc}.json"
    if files_path.is_file():
        obj = safe_json(files_path)
        raw = json.dumps(obj, sort_keys=True)
        items.append({
            "evidence_ref": "snapshot_files",
            "kind": "json_summary",
            "path": str(files_path),
            "sha256": sha256_file(files_path),
            "content": raw[:20000],
        })

    items.append({"evidence_ref": "candidate_projection_diff", "kind": "deterministic_diff", "content": projection_diff(candidate, projected)})

    publication_manifests: list[Path] = []
    if args.publication_manifest and args.publication_manifest.is_file():
        publication_manifests.append(args.publication_manifest)
    if extra_evidence_root:
        summary_path = extra_evidence_root / "evidence_escalation_summary.json"
        summary = safe_json(summary_path) if summary_path.is_file() else {}
        recovered = Path(str((summary.get("outputs") or {}).get("publication_manifest") or ""))
        if recovered.is_file() and recovered not in publication_manifests:
            publication_manifests.append(recovered)
    pub_rows: list[dict[str,str]] = []
    seen_pub: set[tuple[str,...]] = set()
    for manifest in publication_manifests:
        for row in read_tsv(manifest):
            if (row.get("accession") or "").strip().upper() != acc:
                continue
            sig = tuple(str(row.get(k) or "") for k in ("publication_doi","publication_pmid","publication_pmcid","publication_title","publication_content_path","publication_content_text_path"))
            if sig in seen_pub:
                continue
            seen_pub.add(sig); pub_rows.append(row)
    if pub_rows:
        for idx, row in enumerate(pub_rows[:6], start=1):
            compact = {k: v for k, v in row.items() if v and k in {
                "accession", "dataset_title", "dataset_description", "publication_doi", "publication_pmid",
                "publication_pmcid", "publication_title", "publication_citation", "organisms", "instruments",
                "experiment_types", "quantification_methods", "publication_content_status",
            }}
            ref = f"publication_manifest_{idx}"
            item: dict[str, Any] = {"evidence_ref": ref, "kind": "publication_manifest", "content": compact}
            for key in ("publication_content_text_path", "publication_content_path"):
                value = (row.get(key) or "").strip()
                p = Path(value) if value else None
                if p and p.is_file() and p.suffix.lower() != ".pdf":
                    item["content_excerpt"] = text_snippets(p)
                    item["content_path"] = str(p)
                    item["content_sha256"] = sha256_file(p)
                    break
            items.append(item)

    # Publication annotation outputs are source-grounded semantic evidence.  Limit the
    # bundle to files explicitly carrying the accession in their path/name.
    annotation_roots: list[Path] = []
    if args.annotations_dir and args.annotations_dir.is_dir():
        annotation_roots.append(args.annotations_dir)
    if extra_evidence_root:
        enriched_annotations = extra_evidence_root / "publication_annotations" / "annotations"
        if enriched_annotations.is_dir():
            annotation_roots.append(enriched_annotations)
    found: list[Path] = []
    for annotation_root in annotation_roots:
        found.extend(p for p in annotation_root.rglob("*") if p.is_file() and acc.lower() in str(p).lower())
    for idx, p in enumerate(sorted(set(found))[:12], start=1):
            ref = f"publication_annotation_{idx}"
            if p.suffix.lower() == ".json":
                content: Any = safe_json(p)
            else:
                content = text_snippets(p, 8000)
            items.append({"evidence_ref": ref, "kind": "annotation", "path": str(p), "sha256": sha256_file(p), "content": content})

    if extra_evidence_root:
        graph_root = extra_evidence_root / "generalized_evidence_graph"
        if graph_root.is_dir():
            graph_files = [p for p in graph_root.rglob("*") if p.is_file() and acc.lower() in str(p).lower()]
            for idx, p in enumerate(sorted(graph_files)[:8], start=1):
                content: Any = safe_json(p) if p.suffix.lower() == ".json" else text_snippets(p, 8000)
                items.append({"evidence_ref": f"generalized_evidence_{idx}", "kind": "structured_evidence", "path": str(p), "sha256": sha256_file(p), "content": content})

    evidence = {
        "schema_version": "pride-scp-exact-hash-review-evidence-v1",
        "accession": acc,
        "candidate_sha256": sha256_file(candidate),
        "projected_sha256": sha256_file(projected),
        "items": items,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    return output


def stage_readiness_candidate(candidate: Path, acc: str, root: Path, copy_audit: bool = True) -> Path:
    d = root / acc
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f"{acc}.sdrf.tsv"
    shutil.copy2(candidate, dest)
    audit = candidate.with_name(f"{acc}.sdrf_audit.json")
    if copy_audit and audit.is_file():
        shutil.copy2(audit, d / f"{acc}.sdrf_audit.json")
    elif not copy_audit:
        # Deterministic resolver bytes have not been re-audited by the Rust producer.
        # Preserve provenance without falsely inheriting a locally_valid assertion for
        # different bytes; frozen readiness reruns all deterministic/BigBio checks.
        (d / f"{acc}.sdrf_audit.json").write_text(json.dumps({
            "accession": acc,
            "controller_deterministic_resolver": True,
            "source_candidate_path": str(candidate),
            "candidate_sha256": sha256_file(candidate),
        }, indent=2) + "\n", encoding="utf-8")
    return root


def run_frozen_readiness(
    args: argparse.Namespace,
    acc: str,
    candidate: Path,
    output: Path,
    review_manifest: Path | None = None,
    copy_audit: bool = True,
) -> tuple[int, Path | None]:
    af = output / "accessions.txt"
    write_accessions(af, [acc])
    candidate_root = output / "candidates"
    stage_readiness_candidate(candidate, acc, candidate_root, copy_audit=copy_audit)
    cmd: list[str] = []
    if args.readiness_sif:
        cmd += [args.singularity, "exec", str(args.readiness_sif), args.readiness_python]
    else:
        cmd += [args.python]
    cmd += [
        str(args.readiness_script),
        "--accessions-file", str(af),
        "--snapshot", str(args.snapshot),
        "--candidate-root", str(candidate_root),
        "--output", str(output / "readiness"),
        "--validator-mode", "required",
        "--ontology-mode", "skip",
        "--sdrf-skills-root", str(args.skills_root),
        "--skills-mode", "required",
    ]
    if review_manifest is not None:
        cmd += ["--review-approved-manifest", str(review_manifest)]
    rc = run(cmd, log=output / "readiness.log")
    rp = output / "readiness" / "accessions" / f"{acc}.readiness.json"
    return rc, rp if rp.is_file() else None


def review_and_promote(
    args: argparse.Namespace,
    acc: str,
    candidate: Path,
    projected: Path,
    readiness_path: Path,
    out: Path,
    validation_rows: list[dict[str, Any]],
    tag: str,
    extra_evidence_root: Path | None = None,
) -> dict[str, Any]:
    review_root = out / "reviews" / acc / tag
    projected_receipt = validator_gate(args, acc, projected, f"{tag}_projected", out)
    append_validation(validation_rows, projected_receipt, "projected_review_candidate")
    if projected_receipt.get("validator_gate") != "green":
        return {"final_state": "needs_independent_review", "reason_code": "projected_validator_red", "projected_sha256": sha256_file(projected), "review_status": "held"}

    evidence_path = build_review_evidence(args, acc, candidate, projected, readiness_path, review_root / "evidence.json", extra_evidence_root)
    review_out = review_root / "review.json"
    model = args.review_model or args.model
    cmd = [
        args.python, str(args.review_script),
        "--accession", acc,
        "--projected", str(projected),
        "--readiness", str(readiness_path),
        "--validator-receipt", str(out / "validator_receipts" / acc / f"{tag}_projected.json"),
        "--evidence", str(evidence_path),
        "--output", str(review_out),
        "--model", model,
        "--ollama-url", args.ollama_url,
        "--timeout", str(args.timeout),
    ]
    rc = run(cmd, log=review_root / "review.log")
    if rc != 0 or not review_out.is_file():
        return {"final_state": "needs_independent_review", "reason_code": f"review_infrastructure_failure:{rc}", "projected_sha256": sha256_file(projected), "review_status": "held"}
    review = read_json(review_out)
    status = str(review.get("status") or "held")
    if status != "approved":
        return {"final_state": "needs_independent_review", "reason_code": f"review_{status}", "projected_sha256": sha256_file(projected), "review_status": status}

    digest = sha256_file(projected)
    if str(review.get("sha256") or "").lower() != digest.lower():
        return {"final_state": "needs_independent_review", "reason_code": "review_approved_sha_mismatch", "projected_sha256": digest, "review_status": "held"}

    approval = review_root / "review_approved_manifest.tsv"
    with approval.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["accession", "sha256", "status"], delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerow({"accession": acc, "sha256": digest, "status": "approved"})

    approved_root = review_root / "approved_readiness"
    readiness_rc, final_readiness_path = run_frozen_readiness(args, acc, candidate, approved_root, approval)
    if final_readiness_path is None:
        return {"final_state": "needs_independent_review", "reason_code": f"approved_readiness_infrastructure_failure:{readiness_rc}", "projected_sha256": digest, "review_status": "approved"}
    final_readiness = read_json(final_readiness_path)
    final_state = str(final_readiness.get("state") or "needs_independent_review")
    if final_state != "submission_ready":
        blockers = ";".join(str(x) for x in (final_readiness.get("blockers") or []))
        return {"final_state": final_state, "reason_code": "approved_readiness_not_submission_ready" + (":" + blockers if blockers else ""), "projected_sha256": digest, "review_status": "approved"}

    submission_path = Path(str(final_readiness.get("submission_path") or ""))
    if not submission_path.is_file():
        return {"final_state": "needs_independent_review", "reason_code": "submission_ready_missing_submission_bytes", "projected_sha256": digest, "review_status": "approved"}
    final_receipt = validator_gate(args, acc, submission_path, f"{tag}_final_submission", out)
    append_validation(validation_rows, final_receipt, "final_submission")
    if final_receipt.get("validator_gate") != "green":
        return {"final_state": "needs_independent_review", "reason_code": "final_submission_validator_red", "projected_sha256": digest, "review_status": "approved"}
    return {
        "final_state": "submission_ready",
        "reason_code": "exact_hash_review_approved_and_frozen_readiness_green",
        "projected_sha256": digest,
        "final_sha256": sha256_file(submission_path),
        "final_path": str(submission_path),
        "review_status": "approved",
        "all_submission_boundaries_green": True,
    }


def recheck_deterministic_candidate(
    args: argparse.Namespace,
    acc: str,
    candidate: Path,
    out: Path,
    validation_rows: list[dict[str, Any]],
    tag: str,
    extra_evidence_root: Path | None = None,
) -> dict[str, Any]:
    root = out / "deterministic_rechecks" / acc / tag
    rc, readiness_path = run_frozen_readiness(args, acc, candidate, root, copy_audit=False)
    if readiness_path is None:
        return {"final_state": "unsupported_or_exhausted", "reason_code": f"deterministic_readiness_infrastructure_failure:{rc}"}
    readiness = read_json(readiness_path)
    state = str(readiness.get("state") or "unsupported_or_exhausted")
    projected_sha = str(readiness.get("projected_sha256") or "")
    if state == "needs_independent_review":
        projected = root / "readiness" / "projected" / acc / f"{acc}.sdrf.tsv"
        if projected.is_file() and (not projected_sha or sha256_file(projected).lower() == projected_sha.lower()):
            return review_and_promote(
                args, acc, candidate, projected, readiness_path, out, validation_rows,
                tag + "_deterministic", extra_evidence_root,
            )
        return {"final_state": state, "reason_code": "deterministic_recheck_projection_missing", "projected_sha256": projected_sha}
    if state == "submission_ready":
        submission = Path(str(readiness.get("submission_path") or ""))
        if not submission.is_file():
            return {"final_state": "needs_independent_review", "reason_code": "deterministic_recheck_submission_bytes_missing", "projected_sha256": projected_sha}
        receipt = validator_gate(args, acc, submission, tag + "_deterministic_final_submission", out)
        append_validation(validation_rows, receipt, "final_submission")
        if receipt.get("validator_gate") == "green":
            return {
                "final_state": "submission_ready",
                "reason_code": "deterministic_resolver_frozen_readiness_and_validator_green",
                "projected_sha256": projected_sha,
                "final_sha256": sha256_file(submission),
                "final_path": str(submission),
                "all_submission_boundaries_green": True,
            }
        return {"final_state": "needs_independent_review", "reason_code": "deterministic_recheck_final_validator_red", "projected_sha256": projected_sha}
    blockers = ";".join(str(x) for x in (readiness.get("blockers") or []))
    return {
        "final_state": state,
        "reason_code": "deterministic_resolver_readiness:" + (blockers or "not_ready"),
        "projected_sha256": projected_sha,
    }


def run_cellosaurus(args: argparse.Namespace, acc: str, candidate: Path, stage: Path, out: Path, validation_rows: list[dict[str, Any]]) -> tuple[Path, bool]:
    root = stage / "cellosaurus" / acc
    resolved = root / f"{acc}.sdrf.tsv"
    report = root / "resolver.json"
    cmd = [
        args.python, str(args.cellosaurus_script),
        "--candidate", str(candidate),
        "--output", str(resolved),
        "--report", str(report),
        "--timeout", str(min(args.timeout, 60)),
    ]
    rc = run(cmd, log=root / "resolver.log")
    if rc != 0 or not resolved.is_file() or not report.is_file():
        return candidate, False
    result = read_json(report)
    if not bool(result.get("changed")):
        return candidate, False
    rec = validator_gate(args, acc, resolved, f"{stage.name}_cellosaurus", out)
    append_validation(validation_rows, rec, "cellosaurus_resolver")
    return resolved, True


def synthetic_readiness_root(stage: Path, states: dict[str, tuple[str, str]]) -> Path:
    root = stage / "evidence_input" / "readiness"
    accessions = root / "accessions"
    accessions.mkdir(parents=True, exist_ok=True)
    for acc, (state, reason) in states.items():
        (accessions / f"{acc}.readiness.json").write_text(json.dumps({
            "accession": acc,
            "state": state,
            "blockers": [reason] if reason else [],
            "warnings": [],
        }, indent=2) + "\n", encoding="utf-8")
    return root


def run_evidence_enrichment(
    args: argparse.Namespace,
    stage: Path,
    states: dict[str, tuple[str, str]],
    candidates: dict[str, Path],
) -> tuple[int, Path]:
    root = stage / "evidence_enrichment"
    af = root / "accessions.txt"
    write_accessions(af, sorted(states))
    candidate_root = root / "input_candidates"
    candidate_root.mkdir(parents=True, exist_ok=True)
    for acc, cand in candidates.items():
        copy_candidate(cand, candidate_root, acc)
    rr = synthetic_readiness_root(stage, states)
    cmd = [
        args.python, str(args.evidence_script),
        "--accessions-file", str(af),
        "--readiness-root", str(rr),
        "--snapshot", str(args.snapshot),
        "--publication-manifest", str(args.publication_manifest),
        "--resolved-sdrf-dir", str(candidate_root),
        "--output", str(root),
        "--repo-root", str(args.repo_root),
        "--pride-scp-bin", args.pride_scp,
        "--python", args.python,
        "--model", args.model,
        "--ollama-url", args.ollama_url,
        "--online-mode", "auto",
        "--execute",
    ]
    rc = run(cmd, log=root / "closure_evidence.log")
    return rc, root


def run_structured_mapping(
    args: argparse.Namespace,
    acc: str,
    candidate: Path,
    evidence_root: Path,
    stage: Path,
    out: Path,
    validation_rows: list[dict[str, Any]],
) -> tuple[Path, bool]:
    root = stage / "structured_mapping" / acc
    resolved = root / f"{acc}.sdrf.tsv"
    report = root / "resolver.json"
    roots = [
        evidence_root / "generalized_evidence_graph",
        evidence_root / "publication_external_recovery",
        evidence_root / "publication_refresh",
        args.snapshot / "projects" / f"{acc}.json",
        args.snapshot / "files" / f"{acc}.json",
    ]
    # Include only accession-scoped annotation/stage1 artifacts.  Never scan an entire
    # multi-accession root because common RAW basenames across projects could otherwise
    # create a false cross-project mapping edge.
    for base in (args.annotations_dir, args.stage1_root):
        if not base or not base.exists():
            continue
        narrow = base / acc
        if narrow.exists():
            roots.append(narrow)
            continue
        matches = sorted({p for p in base.rglob("*") if p.exists() and acc.lower() in str(p).lower()})
        roots.extend(matches[:64])
    cmd = [
        args.python, str(args.mapping_script),
        "--candidate", str(candidate),
        "--output", str(resolved),
        "--report", str(report),
        "--accession", acc,
    ]
    for root_path in roots:
        cmd += ["--evidence-root", str(root_path)]
    rc = run(cmd, log=root / "resolver.log")
    if rc != 0 or not resolved.is_file() or not report.is_file():
        return candidate, False
    result = read_json(report)
    if not bool(result.get("changed")):
        return candidate, False
    rec = validator_gate(args, acc, resolved, f"{stage.name}_structured_mapping", out)
    append_validation(validation_rows, rec, "structured_mapping_resolver")
    return resolved, True


def self_test() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        af = root / "a.txt"
        af.write_text("PXD000002\nPXD000001\nPXD000001\n")
        assert parse_accessions(af) == ["PXD000001", "PXD000002"]
        p = root / "x"
        p.mkdir()
        f = p / "PXD000001.sdrf.tsv"
        f.write_text("a\tb\n1\t2\n")
        assert find_candidate(p, "PXD000001") == f
        manifest = root / "approval.tsv"
        with manifest.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["accession", "sha256", "status"], delimiter="\t")
            w.writeheader(); w.writerow({"accession": "PXD000001", "sha256": "abc", "status": "approved"})
        row = read_tsv(manifest)[0]
        assert row == {"accession": "PXD000001", "sha256": "abc", "status": "approved"}
    print("sdrf_batch_closure self-test: PASS")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--accessions-file", type=Path)
    p.add_argument("--candidate-root", type=Path)
    p.add_argument("--snapshot", type=Path)
    p.add_argument("--annotations-dir", type=Path)
    p.add_argument("--stage1-root", type=Path)
    p.add_argument("--skills-root", type=Path)
    p.add_argument("--output", type=Path)
    p.add_argument("--seed-readiness-dir", type=Path)
    p.add_argument("--publication-manifest", type=Path)
    p.add_argument("--repo-root", type=Path, default=Path.cwd())
    p.add_argument("--model", default="qwen3.6:27b")
    p.add_argument("--review-model", default="")
    p.add_argument("--ollama-url", default="http://127.0.0.1:11434/api/generate")
    p.add_argument("--timeout", type=int, default=1200)
    p.add_argument("--max-closure-passes", type=int, default=2)
    p.add_argument("--bridge-repair-rounds", type=int, default=2)
    p.add_argument("--max-agent-turns", type=int, default=4)
    p.add_argument("--max-tool-actions", type=int, default=6)
    p.add_argument("--max-validator-cycles", type=int, default=2)
    p.add_argument("--agent-sif", type=Path)
    p.add_argument("--readiness-sif", type=Path)
    p.add_argument("--singularity", default="singularity")
    p.add_argument("--pride-scp", default="pride-scp")
    p.add_argument("--python", default="python")
    p.add_argument("--readiness-python", default="python")
    p.add_argument("--parse-sdrf", default="parse_sdrf")
    p.add_argument("--validator-version", default="")
    p.add_argument("--validator-runtime-sha256", default="")
    p.add_argument("--readiness-script", type=Path, default=Path("/opt/pride-scp/scripts/sdrf_bigbio_readiness.py"))
    p.add_argument("--validator-gate-script", type=Path, default=Path("/opt/pride-scp/scripts/sdrf_validator_gate.py"))
    p.add_argument("--autorepair-script", type=Path, default=Path("/opt/pride-scp/scripts/sdrf_batch_autorepair.py"))
    p.add_argument("--cellosaurus-script", type=Path, default=Path("/opt/pride-scp/scripts/sdrf_cellosaurus_resolver.py"))
    p.add_argument("--mapping-script", type=Path, default=Path(__file__).resolve().parent / "sdrf_structured_mapping_resolver.py")
    p.add_argument("--review-script", type=Path, default=Path("/opt/pride-scp/scripts/sdrf_independent_review_agent.py"))
    p.add_argument("--evidence-script", type=Path, default=Path("/opt/pride-scp/scripts/sdrf_evidence_escalation_harness.py"))
    return p


def main() -> int:
    args = parser().parse_args()
    if args.self_test:
        self_test()
        return 0
    required = ("accessions_file", "candidate_root", "snapshot", "annotations_dir", "stage1_root", "skills_root", "output", "publication_manifest")
    for key in required:
        if getattr(args, key, None) is None:
            raise SystemExit(f"--{key.replace('_', '-')} is required")

    args._validator_version, args._validator_runtime_sha256 = detect_validator_runtime(args)
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    accs = parse_accessions(args.accessions_file)
    if not accs:
        raise SystemExit("accession list is empty")

    validation_rows: list[dict[str, Any]] = []
    original_sha: dict[str, str] = {}
    current_root = out / "input_candidates"
    current_root.mkdir(parents=True, exist_ok=True)
    active: list[str] = []
    terminal: dict[str, dict[str, Any]] = {}
    evidence_attempted: set[str] = set()
    evidence_roots_by_acc: dict[str, Path] = {}

    for acc in accs:
        src = find_candidate(args.candidate_root, acc)
        if src is None:
            terminal[acc] = {"final_state": "unsupported_or_exhausted", "reason_code": "candidate_missing"}
            continue
        dest = current_root / f"{acc}.sdrf.tsv"
        shutil.copy2(src, dest)
        original_sha[acc] = sha256_file(dest)
        rec = validator_gate(args, acc, dest, "input", out)
        append_validation(validation_rows, rec, "input")
        active.append(acc)

    for pass_no in range(1, args.max_closure_passes + 1):
        if not active:
            break
        stage = out / f"closure_pass{pass_no:02d}"
        stage.mkdir(parents=True, exist_ok=True)
        rc = run_bridge(args, active, current_root, args.seed_readiness_dir if pass_no == 1 else None, stage)
        if rc != 0:
            for acc in active:
                terminal.setdefault(acc, {"final_state": "unsupported_or_exhausted", "reason_code": f"bridge_infrastructure_failure:{rc}"})
            break
        ledger = stage / "autorepair" / "autorepair_ledger.tsv"
        if not ledger.is_file():
            for acc in active:
                terminal.setdefault(acc, {"final_state": "unsupported_or_exhausted", "reason_code": "bridge_ledger_missing"})
            break
        rows = {r["accession"]: r for r in csv.DictReader(ledger.open(newline=""), delimiter="\t")}

        next_active: list[str] = []
        next_root = stage / "next_candidates"
        next_root.mkdir(parents=True, exist_ok=True)
        enrich_states: dict[str, tuple[str, str]] = {}
        enrich_candidates: dict[str, Path] = {}
        enrich_bridge_rows: dict[str, dict[str, str]] = {}

        for acc in active:
            row = rows.get(acc)
            if not row:
                terminal[acc] = {"final_state": "unsupported_or_exhausted", "reason_code": "bridge_result_missing"}
                continue
            cand = Path(row.get("candidate_path") or "")
            if not cand.is_file():
                terminal[acc] = {"final_state": "unsupported_or_exhausted", "reason_code": "bridge_candidate_missing"}
                continue
            rec = validator_gate(args, acc, cand, f"pass{pass_no:02d}_bridge_candidate", out)
            append_validation(validation_rows, rec, "bridge_candidate")
            state = terminal_from_bridge(row)
            reason = row.get("reason_code", "")

            if state == "needs_independent_review":
                readiness_path = readiness_json_for_bridge(row)
                projected = locate_projected(stage, acc, row.get("projected_sha256", ""))
                if readiness_path and projected:
                    result = review_and_promote(args, acc, cand, projected, readiness_path, out, validation_rows, f"pass{pass_no:02d}", evidence_roots_by_acc.get(acc))
                    terminal[acc] = result
                    if result.get("final_state") == "needs_independent_review" and result.get("review_status") == "insufficient_evidence" and pass_no < args.max_closure_passes and acc not in evidence_attempted:
                        terminal.pop(acc, None)
                        enrich_states[acc] = ("blocked_internal_evidence", "review_insufficient_evidence")
                        enrich_candidates[acc] = cand
                        enrich_bridge_rows[acc] = row
                    continue
                terminal[acc] = {"final_state": "needs_independent_review", "reason_code": "review_ready_projection_or_readiness_missing", "projected_sha256": row.get("projected_sha256", "")}
                continue

            if state == "ontology_mapping_required":
                resolved, changed = run_cellosaurus(args, acc, cand, stage, out, validation_rows)
                if changed and pass_no < args.max_closure_passes:
                    copy_candidate(resolved, next_root, acc)
                    next_active.append(acc)
                    continue
                if changed and pass_no >= args.max_closure_passes:
                    terminal[acc] = recheck_deterministic_candidate(
                        args, acc, resolved, out, validation_rows, f"pass{pass_no:02d}_cellosaurus",
                        evidence_roots_by_acc.get(acc),
                    )
                    continue
                if acc not in evidence_attempted and pass_no < args.max_closure_passes:
                    enrich_states[acc] = (state, reason)
                    enrich_candidates[acc] = resolved if changed else cand
                    enrich_bridge_rows[acc] = row
                    continue
                terminal[acc] = {"final_state": state, "reason_code": reason}
                continue

            if state == "row_mapping_required":
                if acc not in evidence_attempted and pass_no < args.max_closure_passes:
                    enrich_states[acc] = (state, reason)
                    enrich_candidates[acc] = cand
                    enrich_bridge_rows[acc] = row
                    continue
                terminal[acc] = {"final_state": state, "reason_code": reason}
                continue

            if state in {"provenance_conflict", "template_gap"}:
                terminal[acc] = {"final_state": state, "reason_code": reason}
                continue

            if state == "submission_ready":
                # Bridge readiness should not normally reach this without a supplied review
                # manifest, but do not downgrade it.  Exact final-byte validation is still
                # required before accepting the terminal state.
                rec2 = validator_gate(args, acc, cand, f"pass{pass_no:02d}_bridge_submission", out)
                append_validation(validation_rows, rec2, "bridge_submission")
                terminal[acc] = {
                    "final_state": "submission_ready" if rec2.get("validator_gate") == "green" else "needs_independent_review",
                    "reason_code": "bridge_submission_validator_green" if rec2.get("validator_gate") == "green" else "bridge_submission_validator_red",
                    "final_sha256": sha256_file(cand),
                    "all_submission_boundaries_green": rec2.get("validator_gate") == "green",
                }
                continue

            # Unsupported/exhausted is an evidence lane once, not an immediate terminal.
            if state == "unsupported_or_exhausted" and acc not in evidence_attempted and pass_no < args.max_closure_passes:
                enrich_states[acc] = (state, reason)
                enrich_candidates[acc] = cand
                enrich_bridge_rows[acc] = row
                continue

            if pass_no < args.max_closure_passes:
                copy_candidate(cand, next_root, acc)
                next_active.append(acc)
            else:
                terminal[acc] = {"final_state": state, "reason_code": reason}

        if enrich_states:
            evidence_attempted.update(enrich_states)
            evidence_rc, evidence_root = run_evidence_enrichment(args, stage, enrich_states, enrich_candidates)
            for acc in enrich_states:
                evidence_roots_by_acc[acc] = evidence_root
            for acc, (state, reason) in enrich_states.items():
                base = enrich_candidates[acc]
                evidence_candidate = find_candidate(evidence_root / "sdrf_annotation", acc)
                selected = evidence_candidate if evidence_candidate and evidence_candidate.is_file() else base
                if evidence_candidate and evidence_candidate.is_file():
                    ev_rec = validator_gate(args, acc, evidence_candidate, f"pass{pass_no:02d}_evidence_candidate", out)
                    append_validation(validation_rows, ev_rec, "evidence_enrichment")
                changed = sha256_file(selected) != sha256_file(base)

                if state == "row_mapping_required":
                    mapped, mapped_changed = run_structured_mapping(args, acc, selected, evidence_root, stage, out, validation_rows)
                    if mapped_changed:
                        selected = mapped
                        changed = True

                if pass_no < args.max_closure_passes:
                    copy_candidate(selected, next_root, acc)
                    next_active.append(acc)
                else:
                    terminal[acc] = {
                        "final_state": state if state in {"row_mapping_required", "ontology_mapping_required"} else "unsupported_or_exhausted",
                        "reason_code": ("evidence_enrichment_changed_candidate_but_pass_budget_exhausted" if changed else reason) + (f":evidence_rc={evidence_rc}" if evidence_rc else ""),
                    }

        current_root = next_root
        active = sorted(set(next_active))

    fields = [
        "accession", "initial_candidate_sha256", "final_state", "reason_code",
        "projected_sha256", "final_sha256", "review_status", "final_path",
        "all_submission_boundaries_green",
    ]
    ledger_out = out / "closure_ledger.tsv"
    with ledger_out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for acc in accs:
            info = terminal.get(acc, {"final_state": "unsupported_or_exhausted", "reason_code": "closure_incomplete"})
            writer.writerow({
                "accession": acc,
                "initial_candidate_sha256": original_sha.get(acc, ""),
                "final_state": info.get("final_state", ""),
                "reason_code": info.get("reason_code", ""),
                "projected_sha256": info.get("projected_sha256", ""),
                "final_sha256": info.get("final_sha256", ""),
                "review_status": info.get("review_status", ""),
                "final_path": info.get("final_path", ""),
                "all_submission_boundaries_green": str(bool(info.get("all_submission_boundaries_green", False))).lower(),
            })

    val_out = out / "validation_ledger.tsv"
    with val_out.open("w", newline="", encoding="utf-8") as fh:
        fields2 = ["accession", "stage", "boundary", "candidate_sha256", "validator_gate", "validator_version", "runtime_sha256"]
        writer = csv.DictWriter(fh, fieldnames=fields2, delimiter="\t", lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(validation_rows)

    counts = Counter(x.get("final_state", "") for x in terminal.values())
    summary = {
        "controller": VERSION,
        "accessions": len(accs),
        "state_counts": dict(counts),
        "submission_ready": counts.get("submission_ready", 0),
        "ledger": str(ledger_out),
        "validation_ledger": str(val_out),
        "validator_gated": True,
        "validator_version": args._validator_version,
        "validator_runtime_sha256": args._validator_runtime_sha256,
        "evidence_escalation_attempted": len(evidence_attempted),
        "closure_dispatch": {
            "review_promotion": True,
            "cellosaurus_exact": True,
            "structured_row_mapping": True,
            "explicit_multiplex_expansion": True,
            "bounded_evidence_enrichment": True,
        },
    }
    (out / "closure_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
