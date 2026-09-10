#!/usr/bin/env python3
"""Fail-closed SDRF readiness and BigBio validation gate.

This stage is intentionally non-generative.  It never asks an LLM to write or repair an SDRF and it
never infers sample/file/channel mappings.  It consumes candidate SDRFs that already exist from a
source-grounded PRIDE_SCP lane (or an explicitly supplied external/curated source), checks internal
source-closure signals, then applies the BigBio-facing acceptance stack:

  PRIDE_SCP source closure -> single-cell contract -> parse_sdrf -> sdrf-skills check/score
  -> hash-bound independent review -> submission layout

A validator pass is necessary but never treated as proof of scientific truth.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable

VERSION = "pride-scp-sdrf-readiness-v0.1"
POLICY_VERSION = "pride-scp-bigbio-readiness-v0.5.13"
SDRF_PIPELINES_PIN = "0.1.6"

# Versions observed in the current upstream template manifest on 2026-09-10.  Runtime validation is
# still delegated to parse_sdrf; these constants are provenance/reporting anchors, not a replacement
# for the upstream template definitions.
TEMPLATE_VERSION_HINTS = {
    "single-cell": "1.0.0",
    "ms-proteomics": "1.1.0",
    "dia-acquisition": "1.1.0",
    "human": "1.1.0",
    "vertebrates": "1.1.0",
    "invertebrates": "1.1.0",
    "plants": "1.1.0",
    "cell-lines": "1.1.0",
}

REQUIRED_SINGLE_CELL_COLUMNS = {
    "source name",
    "assay name",
    "technology type",
    "comment[data file]",
    "characteristics[single cell isolation protocol]",
    "characteristics[cell identifier]",
}
PLACEHOLDERS = {
    "",
    "none",
    "na",
    "n/a",
    "null",
    "unknown",
    "unspecified",
    "not specified",
    "not_specified",
    "not reported",
    "not available",
    "not applicable",
    "missing",
    "undetermined",
}
RAW_EXTENSIONS = (
    ".raw", ".d", ".d.zip", ".wiff", ".wiff2", ".mzml", ".mzxml", ".mgf", ".tdf",
)
ACCEPTED_INTERNAL_COMPLETENESS = {
    "locally_valid_draft",
    "existing_sdrf_enriched_locally_valid",
    "locally_valid",
    "valid",
    "source_closed",
    "source_closed_sdrf",
}

KNOWN_TEMPLATE_NAMES = set(TEMPLATE_VERSION_HINTS) | {
    "sample-metadata", "base", "clinical-metadata", "oncology-metadata", "human-gut",
    "immunopeptidomics", "metaproteomics", "crosslinking", "affinity-proteomics",
}


@dataclass
class CommandResult:
    label: str
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    available: bool = True

    @property
    def passed(self) -> bool:
        return self.available and self.returncode == 0


@dataclass
class CandidateInfo:
    path: str = ""
    sha256: str = ""
    source_kind: str = ""
    internal_audit_path: str = ""
    locally_valid: bool | None = None
    completeness_status: str = ""
    relation_mode: str = ""
    generation_mode: str = ""
    internal_validation_errors: int | None = None


@dataclass
class GraphSignals:
    available: bool = False
    semantic_conflicts: int = 0
    semantic_hygiene_rejections: int = 0
    canonical_branch_candidates: int = 0
    contradictory_branches: int = 0
    unresolved_branches: int = 0
    clean_unique_contexts: int = 0
    conflicted_context_candidates: int = 0
    accepted_mapping_edges: int = 0


@dataclass
class ReadinessResult:
    accession: str
    state: str
    ready_for_projection: bool
    submission_ready: bool
    candidate: CandidateInfo
    graph: GraphSignals
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    templates: list[str] = field(default_factory=list)
    rows: int = 0
    unique_data_files: int = 0
    matched_repository_files: int = 0
    unmatched_repository_files: list[str] = field(default_factory=list)
    missing_required_columns: list[str] = field(default_factory=list)
    placeholder_required_columns: list[str] = field(default_factory=list)
    parse_sdrf: list[dict[str, Any]] = field(default_factory=list)
    skills_check: dict[str, Any] = field(default_factory=dict)
    skills_score: dict[str, Any] = field(default_factory=dict)
    review: dict[str, Any] = field(default_factory=dict)
    projected_path: str = ""
    submission_path: str = ""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def norm_header(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def norm_value(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def placeholder(value: str) -> bool:
    return norm_value(value).lower().replace("_", " ") in {x.replace("_", " ") for x in PLACEHOLDERS}


def collect_accessions(values: Iterable[str], path: Path | None) -> list[str]:
    out: set[str] = set()
    rx = re.compile(r"\bPXD\d{6}\b", re.I)
    for value in values:
        m = rx.search(value)
        if m:
            out.add(m.group(0).upper())
    if path:
        if not path.is_file():
            raise SystemExit(f"accessions file not found: {path}")
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        for line in text.splitlines():
            m = rx.search(line)
            if m:
                out.add(m.group(0).upper())
    return sorted(out)


def read_sdrf(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
        reader = csv.reader(fh, delimiter="\t")
        try:
            headers = [norm_header(x) for x in next(reader)]
        except StopIteration:
            return [], []
        rows = []
        for rec in reader:
            rec = [norm_value(x) for x in rec]
            if not any(rec):
                continue
            if len(rec) < len(headers):
                rec += [""] * (len(headers) - len(rec))
            rows.append(rec[: len(headers)])
    return headers, rows


def header_index(headers: list[str], name: str) -> int | None:
    try:
        return headers.index(norm_header(name))
    except ValueError:
        return None


def row_values(headers: list[str], rows: list[list[str]], header: str) -> list[str]:
    idx = header_index(headers, header)
    if idx is None:
        return []
    return [row[idx] if idx < len(row) else "" for row in rows]


def object_file_name(obj: dict[str, Any]) -> str:
    for key in ("fileName", "filename", "name"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def file_category(obj: dict[str, Any]) -> str:
    for key in ("fileCategory", "category"):
        value = obj.get(key)
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            for inner in ("value", "name"):
                v = value.get(inner)
                if isinstance(v, str) and v.strip():
                    return v.strip()
    return ""


def looks_raw(name: str, category: str) -> bool:
    if category.lower() == "raw":
        return True
    lower = name.lower()
    return any(lower.endswith(ext) for ext in RAW_EXTENSIONS)


def snapshot_raw_files(snapshot: Path, accession: str) -> set[str]:
    path = snapshot / "files" / f"{accession}.json"
    if not path.is_file():
        return set()
    try:
        root = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return set()
    out: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            name = object_file_name(value)
            cat = file_category(value)
            if name and looks_raw(name, cat):
                out.add(name)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(root)
    return out


def normalize_data_file(value: str) -> str:
    value = value.strip().replace("\\", "/")
    # comment[data file] should normally be a basename, but external annotations sometimes carry a
    # path.  Compare the basename without inventing any relationship.
    return value.rsplit("/", 1)[-1].strip().lower()


def candidate_patterns(root: Path, accession: str) -> list[Path]:
    return [
        root / f"{accession}.sdrf.tsv",
        root / accession / f"{accession}.sdrf.tsv",
        root / "drafts" / f"{accession}.sdrf.tsv",
        root / "resolved" / f"{accession}.sdrf.tsv",
        root / "sdrf" / f"{accession}.sdrf.tsv",
        root / "datasets" / accession / f"{accession}.sdrf.tsv",
        root / "sandbox" / accession / f"{accession}.sdrf.tsv",
    ]


def discover_candidate(roots: list[Path], accession: str) -> tuple[Path | None, list[str]]:
    found: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in candidate_patterns(root, accession):
            if path.is_file() and path not in found:
                found.append(path)
    if not found:
        return None, []
    by_hash: dict[str, list[Path]] = defaultdict(list)
    for path in found:
        by_hash[sha256_file(path)].append(path)
    if len(by_hash) > 1:
        return None, [str(p) for p in found]
    return found[0], [str(p) for p in found]


def audit_candidates(candidate: Path, roots: list[Path], accession: str) -> list[Path]:
    out: list[Path] = []
    likely = [
        candidate.with_suffix(".audit.json"),
        candidate.parent / f"{accession}.sdrf_audit.json",
        candidate.parent.parent / "audit" / f"{accession}.sdrf_audit.json",
        candidate.parent.parent / "audit" / f"{accession}.audit.json",
    ]
    for root in roots:
        likely += [
            root / "audit" / f"{accession}.sdrf_audit.json",
            root / "audit" / f"{accession}.audit.json",
        ]
    for path in likely:
        if path.is_file() and path not in out:
            out.append(path)
    return out


def load_candidate_info(candidate: Path, roots: list[Path], accession: str) -> CandidateInfo:
    info = CandidateInfo(path=str(candidate), sha256=sha256_file(candidate))
    audits = audit_candidates(candidate, roots, accession)
    if not audits:
        return info
    # Prefer the first co-located audit.  It is advisory provenance; the gate still reruns its own
    # deterministic checks and BigBio validation.
    path = audits[0]
    info.internal_audit_path = str(path)
    try:
        audit = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return info
    lv = audit.get("locally_valid")
    if isinstance(lv, bool):
        info.locally_valid = lv
    info.completeness_status = str(audit.get("completeness_status") or "")
    info.relation_mode = str(audit.get("relation_mode") or "")
    info.generation_mode = str(audit.get("generation_mode") or "")
    err = audit.get("validation_error_count")
    if isinstance(err, int):
        info.internal_validation_errors = err
    return info


def graph_signals(db_path: Path | None, accession: str) -> GraphSignals:
    out = GraphSignals()
    if db_path is None or not db_path.is_file():
        return out
    try:
        con = sqlite3.connect(str(db_path))
        con.row_factory = sqlite3.Row
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        out.available = True
        if "resolution_group" in names:
            q = "SELECT status, COUNT(*) n FROM resolution_group WHERE scope_accession=? GROUP BY status"
            counts = {r["status"]: r["n"] for r in con.execute(q, (accession,))}
            out.semantic_conflicts = int(counts.get("conflicted", 0))
            out.semantic_hygiene_rejections = int(counts.get("rejected_semantic_hygiene", 0))
        if "branch_resolution" in names:
            q = "SELECT status, COUNT(*) n FROM branch_resolution WHERE scope_accession=? GROUP BY status"
            counts = {r["status"]: r["n"] for r in con.execute(q, (accession,))}
            out.canonical_branch_candidates = int(counts.get("canonical_branch_candidate", 0))
            out.contradictory_branches = int(counts.get("contradictory_hypothesis", 0))
            out.unresolved_branches = int(counts.get("unresolved_hypothesis", 0))
        if "context_branch_alignment" in names:
            rows = list(con.execute(
                "SELECT context_node_id,status,attrs_json FROM context_branch_alignment WHERE scope_accession=?",
                (accession,),
            ))
            clean_contexts: set[str] = set()
            for row in rows:
                if row["status"] == "context_conflicted_candidate":
                    out.conflicted_context_candidates += 1
                if row["status"] == "unique_compatible_candidate":
                    try:
                        attrs = json.loads(row["attrs_json"] or "{}")
                    except Exception:
                        attrs = {}
                    if attrs.get("clean_candidate_eligible") is True and not attrs.get("conflicts"):
                        clean_contexts.add(str(row["context_node_id"]))
            out.clean_unique_contexts = len(clean_contexts)
        if "edge" in names:
            mapping = ("MAPS_TO_SAMPLE", "MAPS_TO_CELL", "MAPS_TO_CHANNEL", "BELONGS_TO_BRANCH")
            marks = ",".join("?" for _ in mapping)
            q = f"SELECT COUNT(*) FROM edge WHERE scope_accession=? AND status='accepted' AND predicate IN ({marks})"
            out.accepted_mapping_edges = int(con.execute(q, (accession, *mapping)).fetchone()[0])
        con.close()
    except Exception:
        return GraphSignals()
    return out


def derive_templates(headers: list[str], rows: list[list[str]], explicit: list[str]) -> list[str]:
    names: set[str] = {x.strip().lower() for x in explicit if x.strip()}
    names.add("single-cell")
    names.add("ms-proteomics")
    for value in row_values(headers, rows, "comment[sdrf template]"):
        low = value.lower()
        for name in KNOWN_TEMPLATE_NAMES:
            if re.search(rf"(?<![a-z0-9-]){re.escape(name)}(?![a-z0-9-])", low):
                names.add(name)
    acquisitions = " ".join(row_values(headers, rows, "comment[proteomics data acquisition method]")).lower()
    if re.search(r"\bdia\b|data[- ]independent", acquisitions):
        names.add("dia-acquisition")
    organisms = " ".join(row_values(headers, rows, "characteristics[organism]")).lower()
    if "homo sapiens" in organisms or "ncbitaxon:9606" in organisms:
        names.add("human")
    # Parent templates are still validated separately because sdrf-pipelines 0.1.6 has a known
    # repeated --template pitfall; one subprocess per template is unambiguous.
    order = ["ms-proteomics", "single-cell", "dia-acquisition", "human", "vertebrates", "invertebrates", "plants", "cell-lines"]
    return sorted(names, key=lambda x: (order.index(x) if x in order else len(order), x))


def run_command(argv: list[str], *, cwd: Path | None = None, timeout: int = 180) -> CommandResult:
    label = " ".join(argv[:3])
    exe = shutil.which(argv[0]) if os.sep not in argv[0] else argv[0]
    if not exe or (os.sep in argv[0] and not Path(argv[0]).exists()):
        return CommandResult(label, argv, 127, "", f"command not found: {argv[0]}", available=False)
    try:
        cp = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, timeout=timeout, check=False)
        return CommandResult(label, argv, cp.returncode, cp.stdout[-20000:], cp.stderr[-20000:])
    except subprocess.TimeoutExpired as exc:
        return CommandResult(label, argv, 124, (exc.stdout or "")[-20000:] if isinstance(exc.stdout, str) else "", "timeout")
    except Exception as exc:
        return CommandResult(label, argv, 125, "", repr(exc))


def validate_parse_sdrf(path: Path, templates: list[str], command: str, ontology_mode: str, timeout: int) -> list[CommandResult]:
    results: list[CommandResult] = []
    base = [command, "validate-sdrf", "--sdrf_file", str(path)]
    structural = base + ["--skip-ontology"]
    results.append(run_command(structural, timeout=timeout))
    for template in templates:
        results.append(run_command(base + ["--template", template, "--skip-ontology"], timeout=timeout))
    if ontology_mode == "online":
        # Ontology validation is an additional gate, never a replacement for the structural/template
        # invocations above.  Current upstream tooling may still miss truth-level accession defects.
        results.append(run_command(base, timeout=timeout))
        for template in templates:
            results.append(run_command(base + ["--template", template], timeout=timeout))
    return results


def run_skills(path: Path, root: Path | None, python: str, timeout: int) -> tuple[CommandResult, CommandResult]:
    if root is None or not root.is_dir():
        missing = CommandResult("sdrf-skills", [], 127, "", "sdrf-skills root unavailable", available=False)
        return missing, missing
    env_python = shutil.which(python) or python
    # Run from the skills repo root so `python -m tools` and its spec-relative paths resolve exactly
    # as upstream documents them.
    check = run_command([env_python, "-m", "tools", "check", str(path)], cwd=root, timeout=timeout)
    score = run_command([env_python, "-m", "tools", "score", str(path)], cwd=root, timeout=timeout)
    return check, score


def command_dict(result: CommandResult) -> dict[str, Any]:
    return {
        "label": result.label,
        "argv": result.argv,
        "available": result.available,
        "returncode": result.returncode,
        "passed": result.passed,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def review_manifest(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None or not path.is_file():
        return {}
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        return {str(r.get("accession", "")).upper(): {k: str(v or "") for k, v in r.items()} for r in reader}


def classify_missing_candidate(graph: GraphSignals) -> tuple[str, list[str]]:
    if graph.semantic_conflicts or graph.semantic_hygiene_rejections or graph.conflicted_context_candidates:
        return "blocked_semantic_conflict", ["no source-closed SDRF candidate and semantic conflicts remain"]
    if graph.contradictory_branches > 0:
        return "blocked_branch_conflict", ["no source-closed SDRF candidate and contradictory branch hypotheses remain"]
    if graph.clean_unique_contexts > 0 or graph.canonical_branch_candidates > 0:
        return "blocked_mapping_incomplete", ["branch/design evidence exists but no source-closed row mapping SDRF candidate is available"]
    return "blocked_internal_evidence", ["no source-closed SDRF candidate is available"]


def internal_candidate_checks(
    candidate: CandidateInfo,
    path: Path,
    headers: list[str],
    rows: list[list[str]],
    snapshot: Path,
    accession: str,
) -> tuple[list[str], list[str], list[str], list[str], set[str], set[str]]:
    blockers: list[str] = []
    warnings: list[str] = []
    missing = sorted(REQUIRED_SINGLE_CELL_COLUMNS - set(headers))
    placeholders: list[str] = []
    if not headers or not rows:
        blockers.append("candidate_sdrf_has_no_data_rows")
    if missing:
        blockers.append("missing_bigbio_single_cell_required_columns")
    for header in sorted(REQUIRED_SINGLE_CELL_COLUMNS & set(headers)):
        values = row_values(headers, rows, header)
        if values and all(placeholder(v) for v in values):
            placeholders.append(header)
        elif values and any(placeholder(v) for v in values):
            # Mixed-design SDRFs can legitimately contain non-SCP comparator/reference rows.  Do
            # not pre-empt the upstream template's row-level policy here; surface the condition and
            # let parse_sdrf decide.
            warnings.append(f"some_rows_placeholder_in_required_column:{header}")
    if placeholders:
        blockers.append("all_rows_placeholder_in_required_single_cell_column")

    data_files = {normalize_data_file(v) for v in row_values(headers, rows, "comment[data file]") if not placeholder(v)}
    inventory = {x.lower(): x for x in snapshot_raw_files(snapshot, accession)}
    inventory_keys = set(inventory)
    unmatched = {v for v in data_files if v not in inventory_keys}
    if data_files and inventory_keys and unmatched:
        blockers.append("sdrf_data_file_not_in_pride_raw_inventory")
    if not inventory_keys:
        warnings.append("pride_raw_inventory_unavailable")
    if not data_files:
        blockers.append("no_concrete_comment_data_file_rows")

    if candidate.locally_valid is False or (candidate.internal_validation_errors or 0) > 0:
        blockers.append("internal_sdrf_audit_not_locally_valid")
    if candidate.completeness_status and candidate.completeness_status not in ACCEPTED_INTERNAL_COMPLETENESS:
        low = candidate.completeness_status.lower()
        if "mapping" in low or "repository_scope" in low:
            blockers.append("internal_mapping_incomplete")
        elif "metadata" in low or "template" in low or "isolation" in low:
            blockers.append("internal_metadata_incomplete")
        elif "branch" in low or "relation" in low:
            blockers.append("internal_branch_incomplete")
        else:
            blockers.append("internal_candidate_incomplete")
    if candidate.locally_valid is None:
        warnings.append("no_companion_pride_scp_locally_valid_audit")

    return blockers, warnings, missing, placeholders, data_files, unmatched


def blocker_state(blockers: list[str]) -> str:
    text = " ".join(blockers).lower()
    if "template" in text or "single_cell_required" in text:
        return "blocked_bigbio_template"
    if "mapping" in text or "data_file" in text or "repository_scope" in text:
        return "blocked_mapping_incomplete"
    if "metadata" in text or "isolation" in text or "placeholder" in text:
        return "blocked_metadata_incomplete"
    if "branch" in text or "relation" in text:
        return "blocked_branch_conflict"
    if "semantic" in text:
        return "blocked_semantic_conflict"
    return "blocked_internal_evidence"


def ensure_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def evaluate_accession(args: argparse.Namespace, accession: str, reviews: dict[str, dict[str, str]]) -> ReadinessResult:
    roots = [Path(x) for x in args.candidate_root]
    graph = graph_signals(Path(args.graph_db) if args.graph_db else None, accession)
    candidate_path, all_candidates = discover_candidate(roots, accession)
    if candidate_path is None:
        if all_candidates:
            state = "blocked_internal_evidence"
            blockers = ["multiple_nonidentical_candidate_sdrfs"] + all_candidates
        else:
            state, blockers = classify_missing_candidate(graph)
        return ReadinessResult(
            accession=accession, state=state, ready_for_projection=False, submission_ready=False,
            candidate=CandidateInfo(), graph=graph, blockers=blockers,
        )

    info = load_candidate_info(candidate_path, roots, accession)
    headers, rows = read_sdrf(candidate_path)
    blockers, warnings, missing, placeholders, data_files, unmatched = internal_candidate_checks(
        info, candidate_path, headers, rows, Path(args.snapshot), accession
    )
    templates = derive_templates(headers, rows, args.template)
    result = ReadinessResult(
        accession=accession,
        state="blocked_internal_evidence",
        ready_for_projection=False,
        submission_ready=False,
        candidate=info,
        graph=graph,
        blockers=list(dict.fromkeys(blockers)),
        warnings=list(dict.fromkeys(warnings)),
        templates=templates,
        rows=len(rows),
        unique_data_files=len(data_files),
        matched_repository_files=max(0, len(data_files) - len(unmatched)),
        unmatched_repository_files=sorted(unmatched),
        missing_required_columns=missing,
        placeholder_required_columns=placeholders,
    )
    if result.blockers:
        result.state = blocker_state(result.blockers)
        return result

    # A candidate that passes internal deterministic checks is projected byte-for-byte.  The gate
    # never rewrites cell/sample/channel values and therefore cannot create unsupported truth.
    projected = Path(args.output) / "projected" / accession / f"{accession}.sdrf.tsv"
    ensure_copy(candidate_path, projected)
    result.projected_path = str(projected)
    result.ready_for_projection = True
    result.state = "projected_internal_candidate"

    if args.validator_mode != "off":
        parsed = validate_parse_sdrf(
            projected, templates, args.parse_sdrf, args.ontology_mode, args.command_timeout
        )
        result.parse_sdrf = [command_dict(x) for x in parsed]
        unavailable = [x for x in parsed if not x.available]
        failed = [x for x in parsed if x.available and not x.passed]
        if unavailable and args.validator_mode == "required":
            result.state = "blocked_parse_sdrf"
            result.blockers.append("parse_sdrf_unavailable")
            return result
        if failed:
            result.state = "blocked_parse_sdrf"
            result.blockers.append("parse_sdrf_validation_failed")
            return result

    skills_root = Path(args.sdrf_skills_root) if args.sdrf_skills_root else None
    if args.skills_mode != "off":
        check, score = run_skills(projected, skills_root, args.python, args.command_timeout)
        result.skills_check = command_dict(check)
        result.skills_score = command_dict(score)
        if args.skills_mode == "required" and not check.available:
            result.state = "blocked_bigbio_check"
            result.blockers.append("sdrf_skills_unavailable")
            return result
        if check.available and not check.passed:
            result.state = "blocked_bigbio_check"
            result.blockers.append("sdrf_skills_check_failed")
            return result
        if not check.available:
            result.warnings.append("sdrf_skills_not_run")

    sandbox_path = Path(args.output) / "submission" / "sandbox" / accession / f"{accession}.sdrf.tsv"
    ensure_copy(projected, sandbox_path)
    result.submission_path = str(sandbox_path)

    receipt = reviews.get(accession, {})
    result.review = receipt
    approved = (
        receipt.get("status", "").strip().lower() in {"approved", "pass", "passed"}
        and receipt.get("sha256", "").strip().lower() == info.sha256.lower()
    )
    if not approved:
        result.state = "needs_independent_review"
        result.submission_ready = False
        if receipt:
            result.warnings.append("independent_review_receipt_not_approved_for_exact_sha256")
        return result

    # Submission-ready requires deterministic tools to have been available and passed.  A review
    # receipt alone cannot waive missing validation.
    parse_ok = args.validator_mode != "off" and result.parse_sdrf and all(x.get("passed") for x in result.parse_sdrf)
    skills_ok = args.skills_mode != "off" and result.skills_check.get("passed") is True
    if not parse_ok or not skills_ok:
        result.state = "needs_independent_review"
        result.warnings.append("review_receipt_present_but_required_bigbio_validation_not_complete")
        return result

    datasets_path = Path(args.output) / "submission" / "datasets" / accession / f"{accession}.sdrf.tsv"
    ensure_copy(projected, datasets_path)
    result.submission_path = str(datasets_path)
    result.state = "submission_ready"
    result.submission_ready = True
    return result


def write_outputs(args: argparse.Namespace, results: list[ReadinessResult]) -> dict[str, Any]:
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    (out / "accessions").mkdir(exist_ok=True)
    for result in results:
        (out / "accessions" / f"{result.accession}.readiness.json").write_text(
            json.dumps(asdict(result), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    fields = [
        "accession", "state", "ready_for_projection", "submission_ready", "candidate_path", "candidate_sha256",
        "candidate_source_kind", "internal_audit_path", "internal_locally_valid", "completeness_status",
        "rows", "unique_data_files", "matched_repository_files", "unmatched_repository_files",
        "templates", "blockers", "warnings", "semantic_conflicts", "semantic_hygiene_rejections",
        "canonical_branch_candidates", "contradictory_branches", "unresolved_branches",
        "clean_unique_contexts", "conflicted_context_candidates", "accepted_mapping_edges",
    ]
    with (out / "sdrf_readiness.tsv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        for r in results:
            writer.writerow({
                "accession": r.accession,
                "state": r.state,
                "ready_for_projection": str(r.ready_for_projection).lower(),
                "submission_ready": str(r.submission_ready).lower(),
                "candidate_path": r.candidate.path,
                "candidate_sha256": r.candidate.sha256,
                "candidate_source_kind": r.candidate.source_kind,
                "internal_audit_path": r.candidate.internal_audit_path,
                "internal_locally_valid": "" if r.candidate.locally_valid is None else str(r.candidate.locally_valid).lower(),
                "completeness_status": r.candidate.completeness_status,
                "rows": r.rows,
                "unique_data_files": r.unique_data_files,
                "matched_repository_files": r.matched_repository_files,
                "unmatched_repository_files": ";".join(r.unmatched_repository_files),
                "templates": ";".join(r.templates),
                "blockers": ";".join(r.blockers),
                "warnings": ";".join(r.warnings),
                "semantic_conflicts": r.graph.semantic_conflicts,
                "semantic_hygiene_rejections": r.graph.semantic_hygiene_rejections,
                "canonical_branch_candidates": r.graph.canonical_branch_candidates,
                "contradictory_branches": r.graph.contradictory_branches,
                "unresolved_branches": r.graph.unresolved_branches,
                "clean_unique_contexts": r.graph.clean_unique_contexts,
                "conflicted_context_candidates": r.graph.conflicted_context_candidates,
                "accepted_mapping_edges": r.graph.accepted_mapping_edges,
            })

    states = Counter(r.state for r in results)
    summary = {
        "readiness_version": VERSION,
        "policy_version": POLICY_VERSION,
        "accessions": len(results),
        "state_counts": dict(sorted(states.items())),
        "ready_for_projection": sum(r.ready_for_projection for r in results),
        "submission_ready": sum(r.submission_ready for r in results),
        "parse_sdrf_pin": SDRF_PIPELINES_PIN,
        "validator_mode": args.validator_mode,
        "ontology_mode": args.ontology_mode,
        "skills_mode": args.skills_mode,
        "sdrf_skills_root": args.sdrf_skills_root or "",
        "template_version_hints": TEMPLATE_VERSION_HINTS,
        "policies": {
            "model_generates_sdrf": False,
            "model_creates_sample_file_channel_mapping": False,
            "candidate_rewritten_by_gate": False,
            "parse_sdrf_proves_scientific_truth": False,
            "sdrf_skills_fix_auto_applied": False,
            "multiple_templates_validated_in_separate_processes": True,
            "submission_ready_requires_hash_bound_review": True,
            "gt_runtime_truth_used": False,
        },
        "outputs": {
            "readiness_tsv": str(out / "sdrf_readiness.tsv"),
            "per_accession": str(out / "accessions"),
            "projected": str(out / "projected"),
            "sandbox_submission": str(out / "submission" / "sandbox"),
            "datasets_submission": str(out / "submission" / "datasets"),
        },
    }
    (out / "sdrf_readiness_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="pride-scp-readiness-") as td:
        root = Path(td)
        snapshot = root / "snapshot"
        (snapshot / "files").mkdir(parents=True)
        accession = "PXD999999"
        (snapshot / "files" / f"{accession}.json").write_text(json.dumps({
            "files": [
                {"fileName": "cell_A.raw", "fileCategory": {"value": "RAW"}},
                {"fileName": "cell_B.raw", "fileCategory": {"value": "RAW"}},
            ]
        }))
        candidate_root = root / "candidates"
        candidate_root.mkdir()
        sdrf = candidate_root / f"{accession}.sdrf.tsv"
        sdrf.write_text(
            "\t".join([
                "source name", "assay name", "technology type", "characteristics[organism]",
                "characteristics[single cell isolation protocol]", "characteristics[cell identifier]",
                "comment[data file]", "comment[proteomics data acquisition method]", "comment[sdrf template]",
            ]) + "\n" +
            "\t".join(["cell_A", "assay_A", "proteomic profiling by mass spectrometry", "Homo sapiens",
                        "cellenONE", "cell_A", "cell_A.raw", "DDA", "single-cell v1.0.0; human v1.1.0"]) + "\n" +
            "\t".join(["cell_B", "assay_B", "proteomic profiling by mass spectrometry", "Homo sapiens",
                        "cellenONE", "cell_B", "cell_B.raw", "DDA", "single-cell v1.0.0; human v1.1.0"]) + "\n"
        )
        audit_dir = candidate_root / "audit"
        audit_dir.mkdir()
        (audit_dir / f"{accession}.sdrf_audit.json").write_text(json.dumps({
            "locally_valid": True,
            "validation_error_count": 0,
            "completeness_status": "locally_valid_draft",
            "relation_mode": "one_cell_per_data_file",
            "generation_mode": "source_grounded_explicit_mapping",
        }))

        fake = root / "parse_sdrf"
        fake.write_text("#!/usr/bin/env bash\nexit 0\n")
        fake.chmod(0o755)
        reviews = root / "reviews.tsv"
        digest = sha256_file(sdrf)
        reviews.write_text(f"accession\tsha256\tstatus\treviewer\n{accession}\t{digest}\tapproved\ttest-reviewer\n")

        ns = argparse.Namespace(
            candidate_root=[str(candidate_root)], graph_db="", snapshot=str(snapshot), template=[],
            output=str(root / "out"), validator_mode="required", parse_sdrf=str(fake), ontology_mode="skip",
            command_timeout=10, skills_mode="off", sdrf_skills_root="", python=sys.executable,
        )
        res = evaluate_accession(ns, accession, review_manifest(reviews))
        # With skills deliberately off, even an approved receipt cannot yield submission_ready; this
        # proves the independent tools cannot be silently bypassed.
        assert res.ready_for_projection
        assert res.state == "needs_independent_review"
        assert not res.submission_ready
        assert set(res.templates) >= {"ms-proteomics", "single-cell", "human"}
        assert len(res.parse_sdrf) == 4  # default structural + three separate template invocations
        assert all(x["passed"] for x in res.parse_sdrf)
        assert Path(res.projected_path).read_bytes() == sdrf.read_bytes()

        # Missing required single-cell column must fail before external validation.
        bad = candidate_root / "PXD999998.sdrf.tsv"
        bad.write_text("source name\tassay name\ttechnology type\tcomment[data file]\nA\tA\tMS\tcell_A.raw\n")
        res2 = evaluate_accession(ns, "PXD999998", {})
        assert res2.state == "blocked_bigbio_template"
        assert not res2.ready_for_projection

        # Multiple non-identical candidates are fail-closed.
        alt = candidate_root / "datasets" / accession
        alt.mkdir(parents=True)
        (alt / f"{accession}.sdrf.tsv").write_text(sdrf.read_text() + "# different\n")
        res3 = evaluate_accession(ns, accession, {})
        assert res3.state == "blocked_internal_evidence"
        assert "multiple_nonidentical_candidate_sdrfs" in res3.blockers

    print("sdrf_bigbio_readiness self-test: PASS")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--accession", action="append", default=[])
    p.add_argument("--accessions-file", type=Path)
    p.add_argument("--graph-db", default="")
    p.add_argument("--snapshot", required=False, default="data/snapshot")
    p.add_argument("--candidate-root", action="append", default=[], help="repeatable trusted candidate search root")
    p.add_argument("--output", default="data/sdrf_bigbio_readiness_v0513")
    p.add_argument("--template", action="append", default=[], help="additional BigBio leaf template to validate")
    p.add_argument("--parse-sdrf", default="parse_sdrf")
    p.add_argument("--validator-mode", choices=["required", "optional", "off"], default="required")
    p.add_argument("--ontology-mode", choices=["skip", "online"], default="skip")
    p.add_argument("--sdrf-skills-root", default="")
    p.add_argument("--skills-mode", choices=["required", "optional", "off"], default="optional")
    p.add_argument("--review-approved-manifest", type=Path)
    p.add_argument("--python", default=sys.executable)
    p.add_argument("--command-timeout", type=int, default=180)
    p.add_argument("--self-test", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()
    if args.self_test:
        self_test()
        return 0
    accessions = collect_accessions(args.accession, args.accessions_file)
    if not accessions:
        raise SystemExit("no PXD accessions supplied")
    if not args.candidate_root:
        raise SystemExit("at least one --candidate-root is required; candidate discovery is never performed by broad recursive search")
    reviews = review_manifest(args.review_approved_manifest)
    results: list[ReadinessResult] = []
    for idx, accession in enumerate(accessions, 1):
        result = evaluate_accession(args, accession, reviews)
        results.append(result)
        print(f"[{idx}/{len(accessions)}] {accession} -> {result.state} candidate={result.candidate.path or '-'}")
    summary = write_outputs(args, results)
    print(json.dumps(summary, indent=2, sort_keys=True))
    # Block the command only for runtime/tooling failures when those tools were explicitly required;
    # scientific blockers are expected outputs and do not make a 105-accession batch operationally fail.
    fatal_states = {"blocked_parse_sdrf", "blocked_bigbio_check"}
    if any(r.state in fatal_states for r in results) and (args.validator_mode == "required" or args.skills_mode == "required"):
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
