#!/usr/bin/env python3
"""Bounded evidence-escalation controller for unresolved PRIDE-SCP SDRF accessions.

The controller is deliberately non-generative with respect to sample/file/channel
identity. It orchestrates existing repository components so that unresolved cases
can automatically acquire more public evidence before another annotation pass:

  readiness/triage -> evidence needs -> local evidence inventory
  -> optional online publication/full-text recovery
  -> optional supplementary/external-analysis evidence graph
  -> small-LLM publication annotation
  -> Rust sdrf-annotate
  -> deterministic post-run triage

The small LLM is a semantic reader. It is never authorized to invent RAW/sample/
channel mappings or fill unsupported biological identity. Missing evidence remains
fail-closed and is emitted to a manual/evidence queue after the bounded attempt.
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
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

VERSION = "pride-scp-sdrf-evidence-escalation-v0.1.3"
POLICY_VERSION = "pride-scp-evidence-escalation-policy-v0.1.3"

VALID_STATES_NO_ESCALATION = {"submission_ready", "needs_independent_review"}

LANE_NEEDS: dict[str, tuple[str, ...]] = {
    "candidate_missing": (
        "publication_identity",
        "publication_fulltext",
        "required_metadata",
    ),
    "required_metadata": (
        "publication_fulltext",
        "required_metadata",
    ),
    "mapping": (
        "publication_fulltext",
        "supplementary_design_assets",
        "sample_file_or_channel_mapping",
    ),
    "archive_mapping": (
        "repository_support_assets",
        "sample_file_or_channel_mapping",
    ),
    "semantic_conflict": (
        "publication_fulltext",
        "semantic_disambiguation",
    ),
    "publication_missing": (
        "publication_identity",
        "publication_fulltext",
    ),
    "validator_compatibility": (
        "deterministic_validator_compatibility",
    ),
    "general_evidence": (
        "publication_fulltext",
        "required_metadata",
    ),
    "closed": (),
}

ONLINE_LANES = {
    "candidate_missing",
    "required_metadata",
    "mapping",
    "archive_mapping",
    "semantic_conflict",
    "publication_missing",
    "general_evidence",
}

MAPPING_LANES = {"mapping", "archive_mapping"}


@dataclass
class LocalEvidence:
    snapshot_project: bool = False
    snapshot_files: bool = False
    resolved_sdrf: bool = False
    publication_rows: int = 0
    publication_identifiers: int = 0
    publication_content_rows: int = 0
    publication_content_chars: int = 0
    publication_content_paths: list[str] = field(default_factory=list)


@dataclass
class CasePlan:
    accession: str
    state: str
    lane: str
    blockers: list[str]
    warnings: list[str]
    evidence_needs: list[str]
    local: LocalEvidence
    online_requested: bool
    external_analysis_requested: bool
    model_requested: bool
    stop_reason: str = ""


def read_accessions(path: Path) -> list[str]:
    vals: list[str] = []
    for line in path.read_text(errors="replace").splitlines():
        cell = re.split(r"[\t,]", line.strip(), maxsplit=1)[0].strip().upper()
        if re.fullmatch(r"PXD\d{6,}", cell):
            vals.append(cell)
    return sorted(set(vals))


def read_tsv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(errors="replace", newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def write_tsv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, delimiter="\t", extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fields})


def json_load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(errors="replace"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def readiness_path(root: Path | None, accession: str) -> Path | None:
    if not root:
        return None
    options = [
        root / "accessions" / f"{accession}.readiness.json",
        root / f"{accession}.readiness.json",
    ]
    return next((p for p in options if p.is_file()), None)


def classify_lane(state: str, blockers: list[str], warnings: list[str]) -> str:
    state_l = (state or "").lower()
    text = " ".join([state_l, *blockers, *warnings]).lower()
    if state_l in VALID_STATES_NO_ESCALATION:
        return "closed"
    if "validator" in text or "skills" in text or "ontology" in text:
        # Mapping/metadata/semantic evidence takes precedence over tool drift.
        if not any(k in text for k in ("mapping", "metadata", "semantic", "candidate")):
            return "validator_compatibility"
    if "archive" in text and "mapping" in text:
        return "archive_mapping"
    if any(k in text for k in (
        "sample_to_file", "sample-file", "file_or_channel", "channel_mapping",
        "mapping_incomplete", "data_file_not_in_pride", "reporter mapping",
    )):
        return "mapping"
    if "semantic" in text or "conflict" in text:
        return "semantic_conflict"
    if any(k in text for k in ("metadata_incomplete", "required_metadata", "missing_required")):
        return "required_metadata"
    if any(k in text for k in ("no_trusted_candidate", "candidate_missing", "no source-closed")):
        return "candidate_missing"
    if any(k in text for k in ("publication", "manuscript")) and any(k in text for k in ("missing", "unavailable", "no_")):
        return "publication_missing"
    return "general_evidence"


def publication_index(path: Path | None) -> dict[str, list[dict[str, str]]]:
    out: dict[str, list[dict[str, str]]] = {}
    if not path:
        return out
    for row in read_tsv(path):
        acc = (row.get("accession") or row.get("project_accession") or "").strip().upper()
        if re.fullmatch(r"PXD\d{6,}", acc):
            out.setdefault(acc, []).append(row)
    return out


def valid_content_path(row: dict[str, str]) -> tuple[str, int]:
    candidates = [
        row.get("publication_content_text_path", ""),
        row.get("publication_content_path", ""),
        row.get("pdf_path", ""),
    ]
    for value in candidates:
        value = (value or "").strip()
        if not value:
            continue
        p = Path(value)
        if not p.is_file():
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size < 500:
            continue
        if p.suffix.lower() == ".pdf":
            return str(p.resolve()), int(size)
        try:
            chars = len(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            chars = int(size)
        if chars >= 500:
            return str(p.resolve()), int(chars)
    return "", 0


def inspect_local(
    accession: str,
    snapshot: Path,
    pub_rows: list[dict[str, str]],
    resolved_sdrf_dir: Path | None,
) -> LocalEvidence:
    project_paths = [
        snapshot / "projects" / f"{accession}.json",
        snapshot / "project" / f"{accession}.json",
        snapshot / f"{accession}.json",
    ]
    file_paths = [
        snapshot / "files" / f"{accession}.json",
        snapshot / "file" / f"{accession}.json",
    ]
    identifiers = 0
    content_paths: list[str] = []
    content_chars = 0
    for row in pub_rows:
        if any((row.get(k) or "").strip() for k in (
            "publication_doi", "publication_pmid", "publication_pmcid", "publication_title"
        )):
            identifiers += 1
        p, n = valid_content_path(row)
        if p and p not in content_paths:
            content_paths.append(p)
            content_chars += n
    resolved = False
    if resolved_sdrf_dir:
        resolved = any((resolved_sdrf_dir / name).is_file() for name in (
            f"{accession}.sdrf.tsv",
            f"{accession}.tsv",
        ))
    return LocalEvidence(
        snapshot_project=any(p.is_file() for p in project_paths),
        snapshot_files=any(p.is_file() for p in file_paths),
        resolved_sdrf=resolved,
        publication_rows=len(pub_rows),
        publication_identifiers=identifiers,
        publication_content_rows=len(content_paths),
        publication_content_chars=content_chars,
        publication_content_paths=content_paths,
    )


def case_plan(
    accession: str,
    readiness_root: Path | None,
    readiness_rows: dict[str, dict[str, str]],
    snapshot: Path,
    pub_index: dict[str, list[dict[str, str]]],
    resolved_sdrf_dir: Path | None,
    online_mode: str,
) -> CasePlan:
    rp = readiness_path(readiness_root, accession)
    obj = json_load(rp) if rp else {}
    row = readiness_rows.get(accession, {})
    state = str(obj.get("state") or row.get("state") or "unknown")
    if obj:
        blockers = [str(x) for x in (obj.get("blockers") or [])]
        warnings = [str(x) for x in (obj.get("warnings") or [])]
    else:
        blockers = [x for x in (row.get("blockers") or "").split(";") if x]
        warnings = [x for x in (row.get("warnings") or "").split(";") if x]
    lane = classify_lane(state, blockers, warnings)
    local = inspect_local(accession, snapshot, pub_index.get(accession, []), resolved_sdrf_dir)
    needs = list(LANE_NEEDS[lane])
    if local.publication_content_rows > 0:
        needs = [x for x in needs if x != "publication_fulltext"]
    if local.publication_identifiers > 0:
        needs = [x for x in needs if x != "publication_identity"]
    online_requested = online_mode != "off" and lane in ONLINE_LANES and (
        local.publication_content_rows == 0
        or lane in MAPPING_LANES
        or lane in {"candidate_missing", "publication_missing"}
    )
    external = online_mode != "off" and lane in MAPPING_LANES
    model = lane not in {"closed", "validator_compatibility"}
    stop = "already review/promotion ready" if lane == "closed" else (
        "deterministic validator/tooling lane; model/internet escalation suppressed"
        if lane == "validator_compatibility" else ""
    )
    return CasePlan(
        accession=accession,
        state=state,
        lane=lane,
        blockers=blockers,
        warnings=warnings,
        evidence_needs=needs,
        local=local,
        online_requested=online_requested,
        external_analysis_requested=external,
        model_requested=model,
        stop_reason=stop,
    )


def write_accessions(path: Path, vals: Iterable[str]) -> None:
    xs = sorted(set(vals))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{x}\n" for x in xs), encoding="utf-8")


def command_record(argv: list[str], *, stage: str, accession_file: str = "") -> dict[str, Any]:
    return {
        "stage": stage,
        "argv": argv,
        "accessions_file": accession_file,
    }


def run_command(
    argv: list[str],
    *,
    stage: str,
    logs: Path,
    records: list[dict[str, Any]],
    execute: bool,
    required: bool,
) -> bool:
    records.append(command_record(argv, stage=stage))
    if not execute:
        print("PLAN", stage, "::", " ".join(argv))
        return True
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / f"{stage}.log"
    started = time.time()
    proc = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log.write_text(proc.stdout or "", encoding="utf-8")
    ok = proc.returncode == 0
    print(f"{stage}: returncode={proc.returncode} wall={time.time()-started:.1f}s log={log}")
    if not ok and required:
        raise RuntimeError(f"{stage} failed with returncode={proc.returncode}; see {log}")
    return ok


def content_missing_accessions(manifest: Path, accessions: Iterable[str]) -> list[str]:
    idx = publication_index(manifest)
    missing = []
    for acc in accessions:
        if not any(valid_content_path(r)[0] for r in idx.get(acc, [])):
            missing.append(acc)
    return missing


def snapshot_missing_inputs(snapshot: Path, accessions: Iterable[str]) -> dict[str, list[str]]:
    missing_projects: list[str] = []
    missing_files: list[str] = []
    for acc in accessions:
        if not (snapshot / "projects" / f"{acc}.json").is_file():
            missing_projects.append(acc)
        if not (snapshot / "files" / f"{acc}.json").is_file():
            missing_files.append(acc)
    return {"projects": missing_projects, "files": missing_files}


def annotation_result_counts(path: Path) -> dict[str, int]:
    rows = read_tsv(path)
    success = sum((r.get("status") or "").strip().lower() == "success" for r in rows)
    errors = sum((r.get("status") or "").strip().lower() == "error" for r in rows)
    locally_valid = sum((r.get("locally_valid") or "").strip().lower() == "true" for r in rows)
    return {"rows": len(rows), "success": success, "errors": errors, "locally_valid": locally_valid}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--accessions-file", type=Path, required=False)
    p.add_argument("--readiness-root", type=Path)
    p.add_argument("--readiness-tsv", type=Path, help="Optional sdrf_readiness.tsv; used when per-accession JSON is not local.")
    p.add_argument("--snapshot", type=Path, default=Path("data/snapshot"))
    p.add_argument("--publication-manifest", type=Path, default=Path("work/python/pride_candidate_publications_with_content.tsv"))
    p.add_argument("--resolved-sdrf-dir", type=Path)
    p.add_argument("--resolved-audit-results", type=Path)
    p.add_argument("--output", type=Path, default=Path("work/residual_evidence_escalation"))
    p.add_argument("--repo-root", type=Path, default=Path.cwd())
    p.add_argument("--pride-scp-bin", default="target/release/pride-scp")
    p.add_argument("--python", default=sys.executable)
    p.add_argument("--model", default="qwen2.5:3b")
    p.add_argument("--ollama-url", default="http://localhost:11434/api/generate")
    p.add_argument("--cpu-threads", type=int, default=4)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--contact-email", default=os.environ.get("CONTACT_EMAIL", os.environ.get("NCBI_EMAIL", "")))
    p.add_argument("--online-mode", choices=["off", "auto", "required"], default="auto")
    p.add_argument("--fetch-external-analysis", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--execute", action="store_true", help="Execute the planned escalation. Without this flag the harness only writes/prints the plan.")
    p.add_argument("--force", action="store_true")
    p.add_argument("--self-test", action="store_true")
    return p


def self_test() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        snap = root / "snapshot"
        (snap / "projects").mkdir(parents=True)
        (snap / "files").mkdir(parents=True)
        for acc in ("PXD900001", "PXD900002", "PXD900003", "PXD900004"):
            (snap / "projects" / f"{acc}.json").write_text(json.dumps({"title": "synthetic"}))
            (snap / "files" / f"{acc}.json").write_text("[]")
        rr = root / "ready" / "accessions"
        rr.mkdir(parents=True)
        fixtures = {
            "PXD900001": {"state": "blocked_metadata_incomplete", "blockers": ["required_metadata_missing"], "warnings": []},
            "PXD900002": {"state": "blocked_mapping_incomplete", "blockers": ["sample_to_file_relation_unresolved"], "warnings": []},
            "PXD900003": {"state": "needs_independent_review", "blockers": [], "warnings": []},
            "PXD900004": {"state": "blocked_bigbio_check", "blockers": ["sdrf_skills_known_ontology_drift"], "warnings": []},
        }
        for acc, obj in fixtures.items():
            (rr / f"{acc}.readiness.json").write_text(json.dumps(obj))
        text = root / "paper.txt"; text.write_text("x" * 900)
        pub = root / "pub.tsv"
        write_tsv(pub, [{"accession": "PXD900001", "publication_doi": "10.1/x", "publication_content_text_path": str(text)}], ["accession","publication_doi","publication_content_text_path"])
        idx = publication_index(pub)
        p1 = case_plan("PXD900001", root/"ready", {}, snap, idx, None, "auto")
        p2 = case_plan("PXD900002", root/"ready", {}, snap, idx, None, "auto")
        p3 = case_plan("PXD900003", root/"ready", {}, snap, idx, None, "auto")
        p4 = case_plan("PXD900004", root/"ready", {}, snap, idx, None, "auto")
        assert p1.lane == "required_metadata" and not p1.online_requested and p1.model_requested
        assert p2.lane == "mapping" and p2.online_requested and p2.external_analysis_requested
        assert p3.lane == "closed" and not p3.model_requested
        assert p4.lane == "validator_compatibility" and not p4.online_requested and not p4.model_requested
        assert content_missing_accessions(pub, ["PXD900001", "PXD900002"]) == ["PXD900002"]
        assert snapshot_missing_inputs(snap, ["PXD900001"] ) == {"projects": [], "files": []}
        assert absolute_without_symlink_resolution(Path("relative/path")).is_absolute()
    print("sdrf_evidence_escalation_harness self-test: PASS")


def absolute_without_symlink_resolution(path: Path) -> Path:
    """Return an absolute path without resolving symlinks.

    This matters on HPC systems where the same shared filesystem may be exposed
    under an operational path such as /nfs/... whose realpath is /gpfs/....
    Container bind mounts are path-spelling-sensitive, so canonicalizing the host
    path can make otherwise valid files disappear inside Singularity.
    """
    return Path(os.path.abspath(os.fspath(path)))


def main() -> int:
    args = build_parser().parse_args()
    if args.self_test:
        self_test(); return 0
    if not args.accessions_file:
        raise SystemExit("--accessions-file is required")
    root = absolute_without_symlink_resolution(args.repo_root)
    out = absolute_without_symlink_resolution(args.output)
    out.mkdir(parents=True, exist_ok=True)
    logs = out / "logs"
    accessions = read_accessions(args.accessions_file)
    if not accessions:
        raise SystemExit("accession list is empty")

    pub_idx = publication_index(args.publication_manifest)
    readiness_rows: dict[str, dict[str, str]] = {}
    if args.readiness_tsv and args.readiness_tsv.is_file():
        readiness_rows = {
            (r.get("accession") or "").strip().upper(): r
            for r in read_tsv(args.readiness_tsv)
            if re.fullmatch(r"PXD\d{6,}", (r.get("accession") or "").strip().upper())
        }
    plans = [
        case_plan(acc, args.readiness_root, readiness_rows, args.snapshot, pub_idx, args.resolved_sdrf_dir, args.online_mode)
        for acc in accessions
    ]
    active = [p.accession for p in plans if p.lane != "closed"]
    snapshot_missing = snapshot_missing_inputs(args.snapshot, active)
    if args.execute and (snapshot_missing["projects"] or snapshot_missing["files"]):
        raise RuntimeError(
            "snapshot preflight failed before evidence/model execution: "
            f"missing projects={snapshot_missing['projects']} files={snapshot_missing['files']}"
        )
    online = [p.accession for p in plans if p.online_requested]
    mapping = [p.accession for p in plans if p.external_analysis_requested]
    model = [p.accession for p in plans if p.model_requested]
    deterministic_only = [p.accession for p in plans if p.lane == "validator_compatibility"]

    for name, vals in (
        ("active_accessions.txt", active),
        ("online_accessions.txt", online),
        ("mapping_accessions.txt", mapping),
        ("model_accessions.txt", model),
        ("deterministic_only_accessions.txt", deterministic_only),
    ):
        write_accessions(out / name, vals)

    plan_rows = []
    for p in plans:
        plan_rows.append({
            "accession": p.accession,
            "state": p.state,
            "lane": p.lane,
            "evidence_needs": ";".join(p.evidence_needs),
            "online_requested": str(p.online_requested).lower(),
            "external_analysis_requested": str(p.external_analysis_requested).lower(),
            "model_requested": str(p.model_requested).lower(),
            "snapshot_project": str(p.local.snapshot_project).lower(),
            "snapshot_files": str(p.local.snapshot_files).lower(),
            "resolved_sdrf": str(p.local.resolved_sdrf).lower(),
            "publication_rows": p.local.publication_rows,
            "publication_identifiers": p.local.publication_identifiers,
            "publication_content_rows": p.local.publication_content_rows,
            "publication_content_chars": p.local.publication_content_chars,
            "blockers": " | ".join(p.blockers),
            "warnings": " | ".join(p.warnings),
            "stop_reason": p.stop_reason,
        })
    fields = list(plan_rows[0])
    write_tsv(out / "evidence_escalation_plan.tsv", plan_rows, fields)

    command_records: list[dict[str, Any]] = []
    current_manifest = absolute_without_symlink_resolution(args.publication_manifest) if args.publication_manifest.is_file() else None
    supplementary_links: Path | None = None

    if active and args.online_mode != "off" and online:
        pubdir = out / "publication_refresh"
        pubdir.mkdir(parents=True, exist_ok=True)
        online_file = out / "online_accessions.txt"
        stage1 = pubdir / "pride_publications.tsv"
        stage2 = pubdir / "pride_publications_with_pdfs.tsv"
        stage3 = pubdir / "pride_publications_with_content.tsv"
        py = args.python
        stages = root / "python" / "stages"
        run_command([
            py, str(stages / "01_fetch_pride_publications.py"),
            "--accessions-file", str(online_file),
            "--workers", str(args.workers),
            "--cache-dir", str(pubdir / "pride_project_cache"),
            "--output", str(stage1),
            *( ["--contact-email", args.contact_email] if args.contact_email else [] ),
        ], stage="01_fetch_publications", logs=logs, records=command_records, execute=args.execute, required=args.online_mode=="required")
        if args.execute and not stage1.is_file():
            # Auto mode: preserve local manifest and continue if internet was unavailable.
            print("publication metadata refresh unavailable; continuing with local evidence")
        else:
            run_command([
                py, str(stages / "02_download_publication_pdfs.py"), str(stage1),
                "--output", str(stage2),
                "--pdf-dir", str(pubdir / "publication_pdfs"),
                "--workers", str(args.workers),
                "--manual-queue", str(pubdir / "manual_pdf_queue.tsv"),
                *( ["--contact-email", args.contact_email] if args.contact_email else [] ),
                *( ["--force"] if args.force else [] ),
            ], stage="02_download_publication_pdfs", logs=logs, records=command_records, execute=args.execute, required=False)
            if not args.execute or stage2.is_file():
                run_command([
                    py, str(stages / "03_resolve_publication_content.py"), str(stage2),
                    "--output", str(stage3),
                    "--content-dir", str(pubdir / "publication_content"),
                    "--workers", str(args.workers),
                    "--accessions-file", str(online_file),
                    *( ["--contact-email", args.contact_email] if args.contact_email else [] ),
                    *( ["--force"] if args.force else [] ),
                ], stage="03_resolve_publication_content", logs=logs, records=command_records, execute=args.execute, required=False)
                if not args.execute or stage3.is_file():
                    current_manifest = stage3

        if current_manifest and (not args.execute or current_manifest.is_file()):
            missing = content_missing_accessions(current_manifest, online) if args.execute else online
            missing_file = out / "publication_recovery_accessions.txt"
            write_accessions(missing_file, missing)
            if missing:
                rec = out / "publication_external_recovery"
                run_command([
                    args.python, str(root / "scripts" / "sdrf_residual_external_publication_recovery.py"),
                    "--queue-file", str(missing_file),
                    "--local-manifest", str(current_manifest),
                    "--snapshot-dir", str(args.snapshot),
                    "--output-dir", str(rec),
                ], stage="04_external_publication_recovery", logs=logs, records=command_records, execute=args.execute, required=False)
                combined = rec / "combined_publication_manifest.tsv"
                links = rec / "publication_supplementary_links.tsv"
                if not args.execute or combined.is_file():
                    current_manifest = combined
                if not args.execute or links.is_file():
                    supplementary_links = links

    if current_manifest is None:
        current_manifest = absolute_without_symlink_resolution(args.publication_manifest)

    if mapping and args.fetch_external_analysis:
        evidence_out = out / "generalized_evidence_graph"
        cmd = [
            args.python, str(root / "scripts" / "sdrf_generalized_evidence_graph.py"),
            "--accessions-file", str(out / "mapping_accessions.txt"),
            "--snapshot", str(args.snapshot),
            "--publication-manifest", str(current_manifest),
            "--output", str(evidence_out),
            "--fetch-external-analysis",
        ]
        if supplementary_links:
            cmd += ["--supplementary-links", str(supplementary_links)]
        run_command(cmd, stage="05_generalized_evidence_graph", logs=logs, records=command_records, execute=args.execute, required=False)

    annotation_dir = out / "publication_annotations"
    if model and current_manifest:
        run_command([
            args.python, str(root / "python" / "stages" / "04_run_pride_scp_annotations.py"),
            str(current_manifest),
            "--targeted-script", str(root / "python" / "stages" / "pride_scp_targeted_ollama.py"),
            "--output-dir", str(annotation_dir),
            "--model", args.model,
            "--cpu-threads", str(args.cpu_threads),
            "--ollama-url", args.ollama_url,
            "--workers", "1",
            "--all-valid-content",
            *( ["--force"] if args.force else [] ),
        ], stage="06_small_llm_publication_annotation", logs=logs, records=command_records, execute=args.execute, required=args.execute)

    annot_out = out / "sdrf_annotation"
    if active:
        cmd = [
            str(absolute_without_symlink_resolution(root / args.pride_scp_bin) if not Path(args.pride_scp_bin).is_absolute() else absolute_without_symlink_resolution(Path(args.pride_scp_bin))),
            "sdrf-annotate",
            "--accessions-file", str(out / "active_accessions.txt"),
            "--snapshot", str(args.snapshot),
            "--annotations-dir", str(annotation_dir / "annotations"),
            "--publication-manifest", str(current_manifest),
            "--output", str(annot_out),
            "--model", args.model,
            "--ollama-url", args.ollama_url,
        ]
        if args.resolved_sdrf_dir:
            cmd += ["--resolved-sdrf-dir", str(args.resolved_sdrf_dir)]
        if args.force:
            cmd += ["--force"]
        run_command(cmd, stage="07_sdrf_annotate", logs=logs, records=command_records, execute=args.execute, required=args.execute)

        results = annot_out / "sdrf_annotation_results.tsv"
        if args.execute:
            if not results.is_file():
                raise RuntimeError("07_sdrf_annotate returned success but did not write sdrf_annotation_results.tsv")
            result_counts = annotation_result_counts(results)
            if result_counts["rows"] == 0 or result_counts["success"] == 0:
                raise RuntimeError(
                    "07_sdrf_annotate produced no successful accession rows: "
                    + json.dumps(result_counts, sort_keys=True)
                )
        triage = out / "postrun_triage"
        if not args.execute or results.is_file():
            cmd = [
                args.python, str(root / "scripts" / "sdrf_postrun_triage.py"),
                "--annotation-results", str(results),
                "--output", str(triage),
            ]
            if args.resolved_audit_results:
                cmd += ["--resolved-audit-results", str(args.resolved_audit_results)]
            run_command(cmd, stage="08_postrun_triage", logs=logs, records=command_records, execute=args.execute, required=False)

    (out / "command_plan.json").write_text(json.dumps(command_records, indent=2) + "\n", encoding="utf-8")
    summary = {
        "version": VERSION,
        "policy_version": POLICY_VERSION,
        "accessions": len(accessions),
        "active": len(active),
        "closed": sum(p.lane == "closed" for p in plans),
        "online_requested": len(online),
        "mapping_external_analysis_requested": len(mapping),
        "model_requested": len(model),
        "deterministic_only": len(deterministic_only),
        "lane_counts": {lane: sum(p.lane == lane for p in plans) for lane in sorted({p.lane for p in plans})},
        "online_mode": args.online_mode,
        "execute": args.execute,
        "snapshot_preflight": {
            "missing_projects": snapshot_missing["projects"],
            "missing_files": snapshot_missing["files"],
        },
        "non_generative_mapping": True,
        "gt_runtime_truth_used": False,
        "network_policy": "bounded public evidence retrieval only; online failures fail closed",
        "llm_policy": "semantic reader only; unsupported identity/mapping remains unresolved",
        "outputs": {
            "plan": str(out / "evidence_escalation_plan.tsv"),
            "command_plan": str(out / "command_plan.json"),
            "publication_manifest": str(current_manifest),
            "annotation_output": str(annot_out),
            "postrun_triage": str(out / "postrun_triage"),
        },
    }
    (out / "evidence_escalation_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
