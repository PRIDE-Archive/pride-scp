#!/usr/bin/env python3
"""Fail-closed SDRF readiness and BigBio validation gate.

This stage is intentionally non-generative.  It never asks an LLM to write or repair an SDRF and it
never infers sample/file/channel mappings.  It consumes candidate SDRFs that already exist from a
source-grounded PRIDE_SCP lane (or an explicitly supplied external/curated source), checks internal
source-closure signals, then applies the BigBio-facing acceptance stack:

  PRIDE_SCP source closure -> single-cell contract -> parse_sdrf -> sdrf-skills check/score
  -> hash-bound independent review -> submission layout

A validator pass is necessary but never treated as proof of scientific truth. Source candidates are
immutable; the readiness gate may create a hash-audited BigBio-1.1 compatibility derivative containing
only schema/order/version/template metadata normalization. Missing scientific values remain blockers.
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

from sdrf_scientific_guard import VERSION as SCIENTIFIC_GUARD_VERSION
from sdrf_scientific_guard import analyze as analyze_scientific_guard

VERSION = "pride-scp-sdrf-readiness-v0.2"
POLICY_VERSION = "pride-scp-bigbio-readiness-v0.5.14.3"
SDRF_PIPELINES_PIN = "0.1.6"

# Authoritative SDRF-Proteomics contract used by this gate.  The web specification is treated as
# normative; the pinned validator is an implementation of that contract, not a source of scientific
# truth.  Known implementation/template drift is handled explicitly and auditably below.
SDRF_SPEC_URL = "https://sdrf.quantms.org/specification.html"
SDRF_SINGLE_CELL_SPEC_URL = "https://sdrf.quantms.org/specification.html#_single_cell"
SDRF_VALIDATOR_DIA_DRIFT_ISSUE = "https://github.com/bigbio/sdrf-pipelines/issues/345"

SDRF_SPEC_VERSION = "1.1.0"
SDRF_SPEC_VALUE = f"v{SDRF_SPEC_VERSION}"

# These required-value rules mirror the pinned BigBio 1.1.0 templates.  They are intentionally
# conservative: schema/metadata can be normalized, but scientific values are never invented.
REQUIRED_CONCRETE_RULES: dict[str, dict[str, Any]] = {
    "characteristics[biological replicate]": {"pattern": re.compile(r"^(?:\d+|pooled)$", re.I), "allow_not_applicable": False},
    "comment[technical replicate]": {"pattern": re.compile(r"^\d+$"), "allow_not_applicable": False},
    "comment[fraction identifier]": {"pattern": re.compile(r"^\d+$"), "allow_not_applicable": False},
    "comment[label]": {"pattern": None, "allow_not_applicable": False},
    "comment[cleavage agent details]": {"pattern": None, "allow_not_applicable": True},
    "comment[proteomics data acquisition method]": {"pattern": None, "allow_not_applicable": False},
    "comment[instrument]": {"pattern": None, "allow_not_applicable": False},
}

ORGANISM_TEMPLATE_NAMES = {"human", "vertebrates", "invertebrates", "plants"}
ROW_DERIVED_TEMPLATE_NAMES = ORGANISM_TEMPLATE_NAMES | {"dia-acquisition", "cell-lines"}

PRIDE_SCP_ANNOTATION_TOOL = "pride-scp-sdrf"
PRIDE_SCP_ANNOTATION_TOOL_RE = re.compile(
    r"^(?:pride-scp-sdrf\s+)?pride-scp-sdrf-v(?P<version>\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?)$",
    re.I,
)
PRIDE_SCP_ANNOTATION_TOOL_VALID_RE = re.compile(
    r"^pride-scp-sdrf\s+v\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$",
    re.I,
)

# SDRF 1.1.0, sections 8.1/8.4 and template 14.11.  Keep the recommended NT/AC DIA
# representation.  sdrf-pipelines 0.1.5/0.1.6 bundle a stale dia-acquisition 1.1.0 template that
# rejects this *spec-valid* representation (upstream issue #345); that validator drift is handled
# explicitly rather than by degrading the SDRF to a validator-specific spelling.
DIA_SPEC_ALLOWED_VALUES = {
    "data-independent acquisition",
    "nt=data-independent acquisition;ac=pride:0000450",
    "diapasef",
    "nt=diapasef;ac=pride:0000650",
    "swath ms",
    "nt=swath ms;ac=pride:0000447",
}
KNOWN_DIA_TEMPLATE_DRIFT_PINS = {"0.1.5", "0.1.6"}
KNOWN_DIA_TEMPLATE_DRIFT_ERROR_RE = re.compile(
    r"^ERROR: Invalid value 'NT=Data-independent acquisition;AC=PRIDE:0000450' "
    r"- must be one of the allowed values$"
)

# Current SDRF 1.1.0 section 8.3 states that HCD is canonically MS:1000422
# (beam-type collision-induced dissociation), while the short label `HCD` is also valid.  Older
# datasets and validators used PRIDE:0000590 / MS:1002481 or long noncanonical labels.  We only
# canonicalize exact known HCD encodings; no fragmentation method is inferred.
HCD_KNOWN_ACCESSIONS = {"ms:1000422", "pride:0000590", "ms:1002481"}
HCD_KNOWN_LABELS = {
    "hcd",
    "beam-type collision-induced dissociation",
    "higher energy beam-type collision-induced dissociation",
    "higher-energy beam-type collision-induced dissociation",
    "higher-energy c-trap dissociation",
}
RESERVED_WORDS_CANONICAL = {
    "not available": "not available",
    "not applicable": "not applicable",
    "anonymized": "anonymized",
    "pooled": "pooled",
}

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

# SDRF 1.1 section 10.3 says comment[sdrf template] declares leaf templates only; parents are implied.
# This map is used only to serialize file-level template metadata. Validation still runs each selected
# parent/leaf separately because sdrf-pipelines <=0.1.6 does not safely merge repeated --template.
TEMPLATE_PARENT = {
    "dia-acquisition": "ms-proteomics",
    "single-cell": "ms-proteomics",
    "immunopeptidomics": "ms-proteomics",
    "crosslinking": "ms-proteomics",
    "metaproteomics": "ms-proteomics",
    "cell-lines": "sample-metadata",
    "human": "sample-metadata",
    "vertebrates": "sample-metadata",
    "invertebrates": "sample-metadata",
    "plants": "sample-metadata",
    "clinical-metadata": "sample-metadata",
}


@dataclass
class CommandResult:
    label: str
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    available: bool = True
    compatibility_override: bool = False
    compatibility_reason: str = ""

    @property
    def passed(self) -> bool:
        return self.available and (self.returncode == 0 or self.compatibility_override)


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
class NormalizationInfo:
    applied: bool = False
    source_sha256: str = ""
    projected_sha256: str = ""
    actions: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    manifest_path: str = ""


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
    projected_sha256: str = ""
    normalization: NormalizationInfo = field(default_factory=NormalizationInfo)
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


def column_indices(headers: list[str], header: str) -> list[int]:
    target = norm_header(header)
    return [i for i, value in enumerate(headers) if value == target]


def row_values_all(headers: list[str], rows: list[list[str]], header: str) -> list[str]:
    indices = column_indices(headers, header)
    out: list[str] = []
    for row in rows:
        for idx in indices:
            out.append(row[idx] if idx < len(row) else "")
    return out


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


def _organism_token(value: str) -> str:
    low = norm_value(value).lower()
    m = re.search(r"nt=([^;]+);\s*ac=ncbitaxon:(\d+)", low)
    if m:
        return f"{m.group(1).strip()}|ncbitaxon:{m.group(2)}"
    return low


def _is_human_organism(value: str) -> bool:
    low = _organism_token(value)
    return (
        low in {"homo sapiens", "human", "ncbitaxon:9606", "homo sapiens|ncbitaxon:9606"}
        or low.startswith("homo sapiens (")
    )


def _normalize_pride_scp_annotation_tool(value: str) -> str:
    text = norm_value(value)
    if PRIDE_SCP_ANNOTATION_TOOL_VALID_RE.fullmatch(text):
        return text
    match = PRIDE_SCP_ANNOTATION_TOOL_RE.fullmatch(text)
    if not match:
        return text
    return f"{PRIDE_SCP_ANNOTATION_TOOL} v{match.group('version')}"


def _is_dia_acquisition(value: str) -> bool:
    low = norm_value(value).lower()
    # This is a whole-file template-selection classifier, not an ontology resolver. Keep it strict
    # enough that mixed DDA/DIA studies are not validated wholesale with the DIA leaf template.
    return bool(
        re.search(r"\bdata[- ]independent acquisition\b", low)
        or re.search(r"\bdia\b", low)
        or "diapasef" in low
        or "swath" in low
    )


def _normalize_dia_acquisition_value(value: str) -> str:
    text = norm_value(value)
    low = text.lower()
    # Historical PRIDE_SCP drafts used PRIDE:0000628 for the exact label Data-independent acquisition.
    # SDRF 1.1.0 section 8.4 and dia-acquisition 1.1.0 specify PRIDE:0000450 and recommend NT/AC.
    # This is a controlled identifier correction only; it never changes DDA to DIA or infers a method.
    if re.fullmatch(
        r"nt=data-independent acquisition;\s*ac=pride:0000628", low, re.I
    ):
        return "NT=Data-independent acquisition;AC=PRIDE:0000450"
    return text


def _normalize_reserved_word(value: str) -> str:
    text = norm_value(value)
    return RESERVED_WORDS_CANONICAL.get(text.lower(), text)


def _normalize_dissociation_method_value(value: str) -> str:
    text = norm_value(value)
    low = text.lower()
    if low in HCD_KNOWN_LABELS:
        return "HCD"
    match = re.fullmatch(r"nt\s*=\s*(?P<name>[^;]+);\s*ac\s*=\s*(?P<accession>[^;]+)", text, re.I)
    if not match:
        return text
    name = norm_value(match.group("name")).lower()
    accession = norm_value(match.group("accession")).lower()
    if name in HCD_KNOWN_LABELS and accession in HCD_KNOWN_ACCESSIONS:
        return "HCD"
    return text


def _contract_key(value: str) -> str:
    text = norm_value(value).lower()
    text = re.sub(r"\s*=\s*", "=", text)
    text = re.sub(r"\s*;\s*", ";", text)
    return text


def _dia_spec_contract_errors(headers: list[str], rows: list[list[str]]) -> list[str]:
    """Validate the stable SDRF 1.1 DIA rules needed to detect validator-template drift.

    This is intentionally narrow.  It does not replace parse_sdrf: it only proves that a file meets
    the exact acquisition-value/cardinality contract documented in SDRF 1.1.0 template 14.11 before
    we waive the known stale-template error in sdrf-pipelines 0.1.5/0.1.6.
    """
    indices = column_indices(headers, "comment[proteomics data acquisition method]")
    if len(indices) != 1:
        return [f"dia_spec_acquisition_column_cardinality:{len(indices)}"]
    values = [row[indices[0]] if indices[0] < len(row) else "" for row in rows]
    concrete = [value for value in values if not placeholder(value)]
    if not concrete:
        return ["dia_spec_acquisition_value_missing"]
    keys = [_contract_key(value) for value in concrete]
    invalid = sorted({key for key in keys if key not in DIA_SPEC_ALLOWED_VALUES})
    errors: list[str] = []
    if invalid:
        errors.append("dia_spec_invalid_acquisition_value:" + "|".join(invalid))
    if len(set(keys)) != 1:
        errors.append("dia_spec_acquisition_requires_single_value")
    return errors


def _only_known_dia_template_drift(result: CommandResult) -> bool:
    lines = [line.strip() for line in (result.stdout + "\n" + result.stderr).splitlines() if line.strip()]
    error_lines = [line for line in lines if line.startswith("ERROR:")]
    return len(error_lines) == 1 and bool(KNOWN_DIA_TEMPLATE_DRIFT_ERROR_RE.fullmatch(error_lines[0]))


def _apply_known_validator_drift_override(path: Path, template: str, result: CommandResult) -> CommandResult:
    if result.passed or template != "dia-acquisition" or SDRF_PIPELINES_PIN not in KNOWN_DIA_TEMPLATE_DRIFT_PINS:
        return result
    if not _only_known_dia_template_drift(result):
        return result
    headers, rows = read_sdrf(path)
    contract_errors = _dia_spec_contract_errors(headers, rows)
    if contract_errors:
        return result
    result.compatibility_override = True
    result.compatibility_reason = (
        f"known_sdrf_pipelines_{SDRF_PIPELINES_PIN}_dia_template_drift_issue_345;"
        f"spec={SDRF_SPEC_URL}#_dia_acquisition"
    )
    return result


def derive_templates(headers: list[str], rows: list[list[str]], explicit: list[str]) -> list[str]:
    explicit_names = {x.strip().lower() for x in explicit if x.strip()}
    names: set[str] = set(explicit_names)
    names.add("single-cell")
    names.add("ms-proteomics")

    # Historical PRIDE_SCP artifacts may contain stale/incorrect whole-file template metadata.
    # Reuse templates that are not intrinsically row-derived. Organism and DIA templates are re-derived
    # conservatively from the actual SDRF values below, or may be forced explicitly by CLI.
    for value in row_values_all(headers, rows, "comment[sdrf template]"):
        low = value.lower()
        for name in KNOWN_TEMPLATE_NAMES:
            if name in ROW_DERIVED_TEMPLATE_NAMES:
                continue
            if re.search(rf"(?<![a-z0-9-]){re.escape(name)}(?![a-z0-9-])", low):
                names.add(name)

    acquisition_values = [
        value
        for value in row_values_all(headers, rows, "comment[proteomics data acquisition method]")
        if not placeholder(value)
    ]
    dia_flags = [_is_dia_acquisition(value) for value in acquisition_values]
    if acquisition_values and all(dia_flags):
        names.add("dia-acquisition")
    elif "dia-acquisition" not in explicit_names:
        names.discard("dia-acquisition")

    # Derive organism-layer templates from *all* characteristics[organism] columns. Historical
    # SDRFs can contain duplicate organism columns; looking only at the first occurrence can
    # incorrectly classify a mixed/non-human file as wholly human. Every concrete organism
    # observation across every duplicate column participates in the whole-file template decision.
    organism_values = [
        value for value in row_values_all(headers, rows, "characteristics[organism]") if not placeholder(value)
    ]
    if organism_values and all(_is_human_organism(value) for value in organism_values):
        names.add("human")
    elif "human" not in explicit_names:
        names.discard("human")

    # `cell-lines` is also a whole-file sample-layer contract: the template requires an actual
    # characteristics[cell line] value for every row. Mixed experimental designs can legitimately
    # contain cell-line samples alongside zero-cell blanks/controls where cell line is `not applicable`.
    # Do not inherit a stale file-level cell-lines declaration into such files. This exact failure mode
    # is documented upstream (sdrf-pipelines issue #312). An explicit CLI template remains an operator
    # override and is therefore preserved.
    cell_line_values = row_values_all(headers, rows, "characteristics[cell line]")
    if cell_line_values and all(not placeholder(value) for value in cell_line_values):
        names.add("cell-lines")
    elif "cell-lines" not in explicit_names:
        names.discard("cell-lines")

    # Parent templates are validated separately because sdrf-pipelines 0.1.6 has a known repeated
    # --template pitfall; one subprocess per template is unambiguous.
    order = ["ms-proteomics", "single-cell", "dia-acquisition", "human", "vertebrates", "invertebrates", "plants", "cell-lines"]
    return sorted(names, key=lambda x: (order.index(x) if x in order else len(order), x))


def _declared_leaf_templates(templates: list[str]) -> list[str]:
    selected = set(templates)
    non_leaf = {parent for child, parent in TEMPLATE_PARENT.items() if child in selected and parent in selected}
    return [template for template in templates if template not in non_leaf]


def _column_group(header: str) -> int:
    if header == "source name":
        return 0
    if header.startswith("characteristics[") or header == "material type":
        return 1
    if header == "assay name":
        return 2
    if header == "technology type":
        return 3
    if header.startswith("factor value["):
        return 7
    if header == "comment[sdrf version]":
        return 5
    if header == "comment[sdrf template]":
        return 6
    return 4


def _write_sdrf(path: Path, headers: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, delimiter="\t", lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(headers)
        writer.writerows(rows)


def _add_uniform_column(
    headers: list[str], rows: list[list[str]], header: str, value: str, *, before_factors: bool = True
) -> None:
    idx = len(headers)
    if before_factors:
        for i, h in enumerate(headers):
            if h.startswith("factor value["):
                idx = i
                break
    headers.insert(idx, header)
    for row in rows:
        row.insert(idx, value)


def _remove_columns(headers: list[str], rows: list[list[str]], header: str) -> int:
    indices = list(reversed(column_indices(headers, header)))
    for idx in indices:
        headers.pop(idx)
        for row in rows:
            if idx < len(row):
                row.pop(idx)
    return len(indices)


def _required_value_blockers(headers: list[str], rows: list[list[str]]) -> list[str]:
    blockers: list[str] = []
    required_headers = {
        "characteristics[organism]",
        "characteristics[organism part]",
        "characteristics[biological replicate]",
        "comment[technical replicate]",
        "comment[data file]",
        "comment[proteomics data acquisition method]",
        "comment[instrument]",
        "comment[cleavage agent details]",
        "comment[label]",
        "comment[fraction identifier]",
    }
    for header in sorted(required_headers):
        if header not in headers:
            blockers.append(f"bigbio_required_column_missing:{header}")

    for header, rule in REQUIRED_CONCRETE_RULES.items():
        values = row_values(headers, rows, header)
        if not values:
            continue
        bad_rows = 0
        for value in values:
            low = norm_value(value).lower().replace("_", " ")
            if low == "not applicable" and rule["allow_not_applicable"]:
                continue
            if placeholder(value):
                bad_rows += 1
                continue
            pattern = rule.get("pattern")
            if pattern is not None and not pattern.fullmatch(norm_value(value)):
                bad_rows += 1
        if bad_rows:
            blockers.append(f"bigbio_required_value_unresolved:{header}:{bad_rows}_rows")
    return blockers


def normalize_bigbio_projection(
    source: Path, projected: Path, templates: list[str]
) -> tuple[list[str], list[list[str]], NormalizationInfo]:
    headers, rows = read_sdrf(source)
    info = NormalizationInfo(source_sha256=sha256_file(source))
    if not headers or not rows:
        info.blockers.append("candidate_sdrf_has_no_data_rows")
        return headers, rows, info

    # Work on a detached copy. No source candidate is modified in place.
    headers = list(headers)
    rows = [list(row) for row in rows]

    # BigBio 1.1 requires all characteristics columns before assay name and factor values last.
    old_order = list(headers)
    indexed = list(enumerate(headers))
    indexed.sort(key=lambda pair: (_column_group(pair[1]), pair[0]))
    order = [idx for idx, _ in indexed]
    if order != list(range(len(headers))):
        headers = [headers[i] for i in order]
        rows = [[row[i] if i < len(row) else "" for i in order] for row in rows]
        info.actions.append("reordered_columns_to_bigbio_1_1_groups")

    # SDRF 1.1 reserved words are lowercase by contract.  Canonicalize only the exact reserved
    # tokens, never arbitrary biological or technical text.
    reserved_word_changes = 0
    for row in rows:
        for idx, old_value in enumerate(row):
            new_value = _normalize_reserved_word(old_value)
            if new_value != old_value:
                row[idx] = new_value
                reserved_word_changes += 1
    if reserved_word_changes:
        info.actions.append(f"normalized_reserved_word_case:{reserved_word_changes}_cells")

    # Canonicalize exact known HCD encodings according to SDRF 1.1 section 8.3.  `HCD` is explicitly
    # permitted and avoids legacy/deprecated accession/label combinations without inferring a method.
    hcd_changes = 0
    for idx in column_indices(headers, "comment[dissociation method]"):
        for row in rows:
            old_value = row[idx]
            new_value = _normalize_dissociation_method_value(old_value)
            if new_value != old_value:
                row[idx] = new_value
                hcd_changes += 1
    if hcd_changes:
        info.actions.append(f"normalized_hcd_dissociation_method:{hcd_changes}_cells")

    # comment[sdrf version] is the specification version, not the PRIDE_SCP generator version.
    version_indices = column_indices(headers, "comment[sdrf version]")
    if not version_indices:
        _add_uniform_column(headers, rows, "comment[sdrf version]", SDRF_SPEC_VALUE)
        info.actions.append(f"added_comment_sdrf_version:{SDRF_SPEC_VALUE}")
    else:
        first = version_indices[0]
        if any(norm_value(row[first]) != SDRF_SPEC_VALUE for row in rows):
            for row in rows:
                row[first] = SDRF_SPEC_VALUE
            info.actions.append(f"normalized_comment_sdrf_version:{SDRF_SPEC_VALUE}")
        # Collapse duplicate legacy version columns; provenance is preserved in the external manifest.
        for idx in reversed(version_indices[1:]):
            headers.pop(idx)
            for row in rows:
                row.pop(idx)
            info.actions.append("removed_duplicate_comment_sdrf_version")

    # BigBio 1.1 defines comment[sdrf annotation tool] as either `name vX.Y.Z`,
    # `NT=name;VV=vX.Y.Z`, or `manual curation`. Historical PRIDE_SCP drafts serialized the
    # internal generator token directly (or duplicated the tool name), e.g.
    # `pride-scp-sdrf pride-scp-sdrf-v0.3.1`. Normalize only this known PRIDE_SCP lineage.
    annotation_tool_changes = 0
    for idx in column_indices(headers, "comment[sdrf annotation tool]"):
        for row in rows:
            old_value = row[idx]
            new_value = _normalize_pride_scp_annotation_tool(old_value)
            if new_value != old_value:
                row[idx] = new_value
                annotation_tool_changes += 1
    if annotation_tool_changes:
        info.actions.append(f"normalized_pride_scp_annotation_tool:{annotation_tool_changes}_cells")

    # Canonicalize the one known legacy PRIDE accession for the exact DIA term. This is a standards
    # identifier correction for an already-explicit DIA value, not scientific inference.
    dia_value_changes = 0
    for idx in column_indices(headers, "comment[proteomics data acquisition method]"):
        for row in rows:
            old_value = row[idx]
            new_value = _normalize_dia_acquisition_value(old_value)
            if new_value != old_value:
                row[idx] = new_value
                dia_value_changes += 1
    if dia_value_changes:
        info.actions.append(f"normalized_dia_acquisition_accession:{dia_value_changes}_cells")

    # Replace stale PRIDE_SCP/internal template metadata with valid BigBio template declarations.
    removed_templates = _remove_columns(headers, rows, "comment[sdrf template]")
    if removed_templates:
        info.actions.append(f"replaced_legacy_comment_sdrf_template_columns:{removed_templates}")
    declared_templates = _declared_leaf_templates(templates)
    for template in declared_templates:
        version = TEMPLATE_VERSION_HINTS.get(template)
        if not version:
            info.warnings.append(f"template_version_hint_unavailable:{template}")
            continue
        _add_uniform_column(headers, rows, "comment[sdrf template]", f"{template} v{version}")
    if declared_templates:
        info.actions.append("declared_bigbio_leaf_templates:" + ",".join(declared_templates))

    # Human required demographics explicitly allow 'not available'. Adding that sentinel documents
    # absence of source metadata; it does not invent a biological value.
    if "human" in templates:
        for header in ("characteristics[disease]", "characteristics[age]", "characteristics[sex]"):
            if header not in headers:
                _add_uniform_column(headers, rows, header, "not available")
                info.actions.append(f"added_allowed_not_available:{header}")

    # Single-cell recommended metadata must not remain as blank strings when the semantic state is
    # already explicit.  These are reserved-word/schema normalizations only; no biological value is
    # invented. For label-free rows, carrier/reference channels are inapplicable by definition.
    if "single-cell" in templates:
        batch_changes = 0
        for idx in column_indices(headers, "comment[sample preparation batch]"):
            for row in rows:
                if not norm_value(row[idx]):
                    row[idx] = "not available"
                    batch_changes += 1
        if batch_changes:
            info.actions.append(f"filled_blank_sample_preparation_batch_not_available:{batch_changes}_cells")

        labels = [norm_value(v).lower() for v in row_values(headers, rows, "comment[label]")]
        label_free = bool(labels) and all(("label free" in v or "label-free" in v) for v in labels if v)
        if label_free:
            for header in ("comment[carrier channel]", "comment[reference channel]"):
                changes = 0
                for idx in column_indices(headers, header):
                    for row in rows:
                        if not norm_value(row[idx]):
                            row[idx] = "not applicable"
                            changes += 1
                if changes:
                    info.actions.append(f"filled_blank_label_free_{header}_not_applicable:{changes}_cells")

    # Re-run canonical column grouping after metadata/template additions.
    indexed = list(enumerate(headers))
    indexed.sort(key=lambda pair: (_column_group(pair[1]), pair[0]))
    order = [idx for idx, _ in indexed]
    headers = [headers[i] for i in order]
    rows = [[row[i] if i < len(row) else "" for i in order] for row in rows]

    info.blockers.extend(_required_value_blockers(headers, rows))
    info.blockers = list(dict.fromkeys(info.blockers))
    info.actions = list(dict.fromkeys(info.actions))
    info.warnings = list(dict.fromkeys(info.warnings))
    info.applied = bool(info.actions)

    _write_sdrf(projected, headers, rows)
    info.projected_sha256 = sha256_file(projected)
    manifest = projected.with_suffix(".normalization.json")
    info.manifest_path = str(manifest)
    manifest.write_text(json.dumps(asdict(info), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return headers, rows, info


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
        result = run_command(base + ["--template", template, "--skip-ontology"], timeout=timeout)
        results.append(_apply_known_validator_drift_override(path, template, result))
    if ontology_mode == "online":
        # Ontology validation is an additional gate, never a replacement for the structural/template
        # invocations above.  Current upstream tooling may still miss truth-level accession defects.
        results.append(run_command(base, timeout=timeout))
        for template in templates:
            result = run_command(base + ["--template", template], timeout=timeout)
            results.append(_apply_known_validator_drift_override(path, template, result))
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
        "compatibility_override": result.compatibility_override,
        "compatibility_reason": result.compatibility_reason,
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
    if "bigbio_required_value_unresolved" in text or "bigbio_required_column_missing" in text:
        return "blocked_metadata_incomplete"
    if ("mapping" in text or "data_file" in text or "repository_scope" in text
            or "multiorganism" in text or "acquisition" in text):
        return "blocked_mapping_incomplete"
    if ("metadata" in text or "isolation" in text or "placeholder" in text
            or "individual_" in text or "empty_control" in text or "truncated_protocol" in text
            or "organism_part_project" in text):
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
    project_json = Path(args.snapshot) / "projects" / f"{accession}.json"
    scientific_guard = analyze_scientific_guard(candidate_path, project_json if project_json.is_file() else None)
    blockers.extend(f"scientific_guard:{x}" for x in scientific_guard.blockers)
    warnings.extend(f"scientific_guard:{x}" for x in scientific_guard.warnings)
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

    # Create a BigBio-1.1 compatibility derivative. Only schema/order/version/template metadata are
    # normalized automatically. Required scientific values that are missing/placeholder remain
    # blockers; they are never guessed from identifiers, filenames or model output.
    normalized = Path(args.output) / "normalized" / accession / f"{accession}.sdrf.tsv"
    normalized_headers, normalized_rows, normalization = normalize_bigbio_projection(
        candidate_path, normalized, templates
    )
    result.normalization = normalization
    result.warnings.extend(x for x in normalization.warnings if x not in result.warnings)
    if normalization.blockers:
        result.blockers.extend(x for x in normalization.blockers if x not in result.blockers)
        result.state = blocker_state(result.blockers)
        return result

    normalized_guard = analyze_scientific_guard(normalized, project_json if project_json.is_file() else None)
    if normalized_guard.blockers:
        result.blockers.extend(
            x for x in (f"scientific_guard:{b}" for b in normalized_guard.blockers) if x not in result.blockers
        )
        result.warnings.extend(
            x for x in (f"scientific_guard:{w}" for w in normalized_guard.warnings) if x not in result.warnings
        )
        result.state = blocker_state(result.blockers)
        return result

    # Only a normalized candidate with no unresolved required scientific values becomes projected.
    projected = Path(args.output) / "projected" / accession / f"{accession}.sdrf.tsv"
    ensure_copy(normalized, projected)
    result.projected_path = str(projected)
    result.projected_sha256 = sha256_file(projected)
    result.normalization.projected_sha256 = result.projected_sha256
    if result.normalization.manifest_path:
        projected_manifest = projected.parent / f"{accession}.normalization.json"
        ensure_copy(Path(result.normalization.manifest_path), projected_manifest)
        result.normalization.manifest_path = str(projected_manifest)
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
        and receipt.get("sha256", "").strip().lower() == result.projected_sha256.lower()
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
        "projected_sha256", "normalization_applied", "normalization_actions", "normalization_blockers",
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
                "projected_sha256": r.projected_sha256,
                "normalization_applied": str(r.normalization.applied).lower(),
                "normalization_actions": ";".join(r.normalization.actions),
                "normalization_blockers": ";".join(r.normalization.blockers),
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
        "scientific_guard_version": SCIENTIFIC_GUARD_VERSION,
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
        "sdrf_spec_contract": {
            "version": f"v{SDRF_SPEC_VERSION}",
            "specification": SDRF_SPEC_URL,
            "single_cell": SDRF_SINGLE_CELL_SPEC_URL,
            "known_validator_dia_drift_issue": SDRF_VALIDATOR_DIA_DRIFT_ISSUE,
            "validator_drift_overrides": sum(
                1 for r in results for x in r.parse_sdrf if x.get("compatibility_override") is True
            ),
        },
        "policies": {
            "model_generates_sdrf": False,
            "model_creates_sample_file_channel_mapping": False,
            "candidate_rewritten_by_gate": False,
            "source_candidate_mutated_by_gate": False,
            "normalized_derivative_created_by_gate": True,
            "candidate_schema_metadata_normalized_by_gate": True,
            "candidate_scientific_values_invented_by_gate": False,
            "normalization_is_hash_audited": True,
            "parse_sdrf_proves_scientific_truth": False,
            "sdrf_skills_fix_auto_applied": False,
            "multiple_templates_validated_in_separate_processes": True,
            "specification_is_normative_over_known_validator_template_drift": True,
            "validator_drift_override_requires_exact_known_signature_and_local_spec_contract": True,
            "submission_ready_requires_hash_bound_review": True,
            "scientific_guard_runs_before_external_validation": True,
            "blank_single_cell_reserved_words_normalized_without_scientific_inference": True,
            "gt_runtime_truth_used": False,
        },
        "outputs": {
            "readiness_tsv": str(out / "sdrf_readiness.tsv"),
            "per_accession": str(out / "accessions"),
            "normalized": str(out / "normalized"),
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
        legacy_headers = [
            "source name", "assay name", "technology type", "characteristics[organism]",
            "characteristics[organism part]", "characteristics[biological replicate]",
            "characteristics[single cell isolation protocol]", "characteristics[cell identifier]",
            "characteristics[individual]", "characteristics[cells per well]",
            "comment[proteomics data acquisition method]", "comment[instrument]",
            "comment[cleavage agent details]", "comment[label]", "comment[fraction identifier]",
            "comment[technical replicate]", "comment[data file]", "comment[sdrf version]",
            "comment[sdrf template]", "comment[sdrf annotation tool]", "factor value[condition]",
        ]
        legacy_rows = [
            ["cell_A", "assay_A", "proteomic profiling by mass spectrometry", "Homo sapiens", "blood", "1",
             "cellenONE", "cell_A", "donor1", "1", "Data-dependent acquisition", "Orbitrap Fusion Lumos",
             "NT=Trypsin;AC=MS:1001251", "label free sample", "1", "1", "cell_A.raw",
             "pride-scp-sdrf-v0.3.6", "pride-scp-sdrf-v0.3.6",
             "pride-scp-sdrf pride-scp-sdrf-v0.3.6", "case"],
            ["cell_B", "assay_B", "proteomic profiling by mass spectrometry", "Homo sapiens", "blood", "2",
             "cellenONE", "cell_B", "donor2", "1", "Data-dependent acquisition", "Orbitrap Fusion Lumos",
             "NT=Trypsin;AC=MS:1001251", "label free sample", "1", "1", "cell_B.raw",
             "pride-scp-sdrf-v0.3.6", "pride-scp-sdrf-v0.3.6",
             "pride-scp-sdrf-v0.3.6", "control"],
        ]
        _write_sdrf(sdrf, [norm_header(x) for x in legacy_headers], legacy_rows)
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
        original_digest = sha256_file(sdrf)
        reviews.write_text(f"accession\tsha256\tstatus\treviewer\n{accession}\t{original_digest}\tapproved\ttest-reviewer\n")

        ns = argparse.Namespace(
            candidate_root=[str(candidate_root)], graph_db="", snapshot=str(snapshot), template=[],
            output=str(root / "out"), validator_mode="required", parse_sdrf=str(fake), ontology_mode="skip",
            command_timeout=10, skills_mode="off", sdrf_skills_root="", python=sys.executable,
        )
        res = evaluate_accession(ns, accession, review_manifest(reviews))
        assert res.ready_for_projection
        assert res.state == "needs_independent_review"
        assert not res.submission_ready
        assert set(res.templates) >= {"ms-proteomics", "single-cell", "human"}
        assert len(res.parse_sdrf) == 4
        assert all(x["passed"] for x in res.parse_sdrf)
        assert res.normalization.applied
        assert res.projected_sha256 and res.projected_sha256 != original_digest
        # The source candidate is a provenance anchor and must remain byte-identical.
        assert sha256_file(sdrf) == original_digest
        assert Path(res.normalization.manifest_path).is_file()

        projected_headers, projected_rows = read_sdrf(Path(res.projected_path))
        assert max(column_indices(projected_headers, "characteristics[organism]")) < header_index(projected_headers, "assay name")
        assert projected_headers[-1].startswith("factor value[")
        assert set(row_values(projected_headers, projected_rows, "comment[sdrf version]")) == {"v1.1.0"}
        template_values = row_values_all(projected_headers, projected_rows, "comment[sdrf template]")
        assert template_values and all(re.fullmatch(r"[\w-]+ v\d+\.\d+\.\d+(?:-[\w.]+)?", x) for x in template_values)
        # single-cell extends ms-proteomics, so file-level metadata declares only the selected leaves.
        assert "single-cell v1.0.0" in template_values
        assert "human v1.1.0" in template_values
        assert "ms-proteomics v1.1.0" not in template_values
        annotation_values = row_values_all(projected_headers, projected_rows, "comment[sdrf annotation tool]")
        assert annotation_values == ["pride-scp-sdrf v0.3.6", "pride-scp-sdrf v0.3.6"]
        assert not any("pride-scp-sdrf-v" in x for x in annotation_values)
        assert "characteristics[age]" in projected_headers
        assert "characteristics[sex]" in projected_headers
        assert "characteristics[disease]" in projected_headers

        # Missing scientific values that BigBio requires must block rather than being synthesized.
        bad_acc = "PXD999998"
        (snapshot / "files" / f"{bad_acc}.json").write_text(json.dumps({
            "files": [{"fileName": "bad.raw", "fileCategory": {"value": "RAW"}}]
        }))
        bad = candidate_root / f"{bad_acc}.sdrf.tsv"
        bad_rows = [list(legacy_rows[0])]
        bad_rows[0][5] = "not available"
        bad_rows[0][16] = "bad.raw"
        _write_sdrf(bad, [norm_header(x) for x in legacy_headers], bad_rows)
        res2 = evaluate_accession(ns, bad_acc, {})
        assert res2.state == "blocked_metadata_incomplete"
        assert not res2.ready_for_projection
        assert any("characteristics[biological replicate]" in x for x in res2.normalization.blockers)
        assert not res2.parse_sdrf

        # Mixed-organism files must not be validated wholesale as human merely because one row is human
        # or because stale historical template metadata says human.
        mixed_headers = [
            "source name", "characteristics[organism]", "assay name", "technology type",
            "comment[proteomics data acquisition method]", "comment[sdrf template]",
        ]
        mixed_rows = [
            ["h", "Homo sapiens", "h", "proteomic profiling by mass spectrometry", "DIA", "human v1.1.0"],
            ["m", "Mus musculus (mouse)", "m", "proteomic profiling by mass spectrometry", "DIA", "human v1.1.0"],
        ]
        mixed_templates = derive_templates(mixed_headers, mixed_rows, [])
        assert "human" not in mixed_templates
        assert "dia-acquisition" in mixed_templates

        # Historical files may carry duplicate organism columns. Whole-file template derivation must
        # inspect every duplicate column, not only the first one. This protects arbitrary mixed-species
        # candidates from accidental `human` template selection.
        duplicate_organism_headers = [
            "source name", "characteristics[organism]", "characteristics[organism]",
            "assay name", "technology type", "comment[proteomics data acquisition method]",
            "comment[sdrf template]",
        ]
        duplicate_organism_rows = [
            ["h", "Homo sapiens", "Homo sapiens", "h", "proteomic profiling by mass spectrometry", "DIA", "human v1.1.0"],
            ["m", "Homo sapiens", "Mus musculus (mouse)", "m", "proteomic profiling by mass spectrometry", "DIA", "human v1.1.0"],
        ]
        duplicate_templates = derive_templates(duplicate_organism_headers, duplicate_organism_rows, [])
        assert "human" not in duplicate_templates
        assert "dia-acquisition" in duplicate_templates

        # `cell-lines` is a whole-file contract. A mixed cell-line + zero-cell-control SDRF must not
        # inherit stale cell-lines metadata because the template requires actual cell-line values on
        # every row. Conversely, a file whose every row has a concrete cell line should select it.
        mixed_cell_line_headers = [
            "source name", "characteristics[cell line]", "assay name", "technology type",
            "comment[proteomics data acquisition method]", "comment[sdrf template]",
        ]
        mixed_cell_line_rows = [
            ["cell", "U-87 MG", "cell", "proteomic profiling by mass spectrometry",
             "Data-dependent acquisition", "cell-lines v1.1.0"],
            ["blank", "not applicable", "blank", "proteomic profiling by mass spectrometry",
             "Data-dependent acquisition", "cell-lines v1.1.0"],
        ]
        assert "cell-lines" not in derive_templates(mixed_cell_line_headers, mixed_cell_line_rows, [])
        assert "cell-lines" in derive_templates(mixed_cell_line_headers, mixed_cell_line_rows, ["cell-lines"])
        all_cell_line_rows = [
            ["a", "U-87 MG", "a", "proteomic profiling by mass spectrometry",
             "Data-dependent acquisition", ""],
            ["b", "HeLa", "b", "proteomic profiling by mass spectrometry",
             "Data-dependent acquisition", ""],
        ]
        assert "cell-lines" in derive_templates(mixed_cell_line_headers, all_cell_line_rows, [])

        # A mixed DDA/DIA file must not receive a whole-file DIA leaf template merely because one
        # row or stale template declaration mentions DIA.
        mixed_acquisition_headers = [
            "source name", "assay name", "technology type",
            "comment[proteomics data acquisition method]", "comment[sdrf template]",
        ]
        mixed_acquisition_rows = [
            ["a", "a", "proteomic profiling by mass spectrometry", "Data-independent acquisition", "dia-acquisition v1.1.0"],
            ["b", "b", "proteomic profiling by mass spectrometry", "Data-dependent acquisition", "dia-acquisition v1.1.0"],
        ]
        assert "dia-acquisition" not in derive_templates(mixed_acquisition_headers, mixed_acquisition_rows, [])
        assert "dia-acquisition" in derive_templates(mixed_acquisition_headers, mixed_acquisition_rows, ["dia-acquisition"])
        assert (
            _normalize_dia_acquisition_value("NT=Data-independent acquisition;AC=PRIDE:0000628")
            == "NT=Data-independent acquisition;AC=PRIDE:0000450"
        )
        assert _normalize_dia_acquisition_value("Data-dependent acquisition") == "Data-dependent acquisition"

        # Encode the normative SDRF 1.1 DIA contract so a stale validator template cannot make us
        # rewrite a correct NT/AC value into a validator-specific workaround.
        dia_contract_headers = ["comment[proteomics data acquisition method]"]
        dia_contract_rows = [["NT=Data-independent acquisition;AC=PRIDE:0000450"] for _ in range(2)]
        assert not _dia_spec_contract_errors(dia_contract_headers, dia_contract_rows)
        assert not _dia_spec_contract_errors(dia_contract_headers, [["Data-independent acquisition"]])
        assert _dia_spec_contract_errors(
            dia_contract_headers,
            [["Data-independent acquisition"], ["NT=SWATH MS;AC=PRIDE:0000447"]],
        )
        stale = CommandResult(
            "parse_sdrf validate-sdrf",
            ["parse_sdrf", "validate-sdrf", "--template", "dia-acquisition"],
            1,
            "",
            "ERROR: Invalid value 'NT=Data-independent acquisition;AC=PRIDE:0000450' - must be one of the allowed values\nThere were validation errors.\n",
        )
        contract_file = root / "dia_contract.sdrf.tsv"
        _write_sdrf(contract_file, dia_contract_headers, dia_contract_rows)
        overridden = _apply_known_validator_drift_override(contract_file, "dia-acquisition", stale)
        assert overridden.passed and overridden.compatibility_override
        assert "issue_345" in overridden.compatibility_reason
        unrelated = CommandResult(
            "parse_sdrf validate-sdrf",
            ["parse_sdrf", "validate-sdrf", "--template", "dia-acquisition"],
            1,
            "",
            "ERROR: some other DIA problem\nThere were validation errors.\n",
        )
        assert not _apply_known_validator_drift_override(contract_file, "dia-acquisition", unrelated).passed

        # Stable representation-only normalizations directly encoded by SDRF 1.1.
        assert _normalize_reserved_word("Not Available") == "not available"
        assert _normalize_reserved_word("Tumor") == "Tumor"
        assert _normalize_dissociation_method_value("higher energy beam-type collision-induced dissociation") == "HCD"
        assert _normalize_dissociation_method_value("NT=beam-type collision-induced dissociation;AC=MS:1000422") == "HCD"
        assert _normalize_dissociation_method_value("NT=HCD;AC=PRIDE:0000590") == "HCD"
        assert _normalize_dissociation_method_value("ETD") == "ETD"

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
    p.add_argument("--output", default="data/sdrf_bigbio_readiness_v0514")
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
    # Scientific standards failures are expected readiness outputs and must not make a 105-accession
    # Slurm batch fail. Only unavailable tooling that was explicitly required is operationally fatal.
    fatal = False
    if args.validator_mode == "required":
        fatal = fatal or any("parse_sdrf_unavailable" in r.blockers for r in results)
    if args.skills_mode == "required":
        fatal = fatal or any("sdrf_skills_unavailable" in r.blockers for r in results)
    return 4 if fatal else 0


if __name__ == "__main__":
    raise SystemExit(main())
