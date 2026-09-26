#!/usr/bin/env python3
"""Generalized source-grounded SDRF evidence graph for PRIDE single-cell proteomics.

The runtime deliberately contains no accession-specific scientific behavior.  PRIDE accessions are
identifiers only.  Experimental branches, reporter contracts, file membership, external analysis
sources, table joins, and cross-accession relations are inferred from source evidence and reusable
format/schema rules.

The 105-accession reference cohort may be used by wrappers/tests as a benchmark, but is not read as
runtime truth and does not alter inference behavior.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from sdrf_external_artifact_acquisition import acquire_external_artifacts  # noqa: E402

from sdrf_multiplex_evidence_graph import (  # noqa: E402
    DesignContract,
    ExternalSource,
    StructuredEvidence,
    acquire,
    artifact_priority,
    channels_in,
    extract_design_contracts,
    extract_external_sources,
    fetch_github_high_value,
    github_repo_parts,
    manifest_texts,
    norm,
    parse_artifact,
    project_json,
    project_title,
    raw_files,
    read_accessions,
    read_tsv,
    relation_candidates,
    repo_rows,
    supplementary_sources,
    title_similarity,
    write_tsv,
)

VERSION = "pride-scp-sdrf-generalized-evidence-graph-v0.1"
RAW_EXT_RE = re.compile(r"(?i)\.(?:raw|d|wiff|wiff2|mzml|mzxml)$")
RAW_TOKEN_RE = re.compile(r"(?i)([^\s\t,;|]+\.(?:raw|d|wiff|wiff2|mzml|mzxml))")
PXD_RE = re.compile(r"\bPXD\d{6,}\b", re.I)

# These are reusable scientific/format concepts, not dataset identifiers.
ACQ_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("surequant", re.compile(r"(?i)\bsurequant\b")),
    ("prm", re.compile(r"(?i)(?:^|[^a-z])prm(?:[^a-z]|$)")),
    ("dia", re.compile(r"(?i)(?:^|[^a-z])(?:dia|directdia|wwa)(?:[^a-z]|$)|wide[-_ ]window")),
    ("dda", re.compile(r"(?i)(?:^|[^a-z])dda(?:[^a-z]|$)|data[-_ ]dependent")),
)
CHEM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("tmtpro18", re.compile(r"(?i)tmtpro\s*18|18[-_ ]?plex")),
    ("tmtpro16", re.compile(r"(?i)tmtpro\s*16|16[-_ ]?plex")),
    ("tmtpro", re.compile(r"(?i)\btmtpro\b")),
    ("tmt10", re.compile(r"(?i)tmt\s*10|tmt10(?:plex)?")),
    ("tmt8", re.compile(r"(?i)tmt\s*8|tmt8(?:plex)?")),
    ("tmt6", re.compile(r"(?i)tmt\s*6|tmt6(?:plex)?")),
    ("tmt", re.compile(r"(?i)\btmt\b|tandem\s+mass\s+tag")),
    ("itraq", re.compile(r"(?i)\bitraq\b")),
)
SINGLE_RE = re.compile(r"(?i)\b(?:single[-_ ]?cell|single[-_ ]?cells|single[-_ ]?zygote|single[-_ ]?zygotes|single[-_ ]?neuron|single[-_ ]?oocyte|scp)\b")
LABEL_FREE_RE = re.compile(r"(?i)\b(?:label[-_ ]?free|lfq)\b")
TARGETED_RE = re.compile(r"(?i)\b(?:targeted|surequant|prm|triggered\s+ms/?ms)\b")
METHODDEV_RE = re.compile(r"(?i)\b(?:method(?:dev|development|test)|optimization|gradient|benchmark|comparator|control|reference|hela)\b")

# High-value analysis repository paths.  These are schema/semantic classes and therefore reusable.
EXTERNAL_PATH_RE = re.compile(
    r"(?i)(?:cell|sample|input|design|metadata|annotation|characteristic|cellenone|channel|reporter|tmt|raw|run|plex|batch|well|manifest|mapping|sorted|unsorted|table|supp)"
)
EXTERNAL_EXTS = {
    ".txt", ".tsv", ".csv", ".xlsx", ".xls", ".json", ".xml", ".sky", ".pdresult",
    ".pdstudy", ".msf", ".r", ".py", ".zip", ".7z",
}


@dataclass(frozen=True)
class SourceFeatures:
    acquisition: tuple[str, ...] = ()
    chemistry: tuple[str, ...] = ()
    single_cell: bool = False
    label_free: bool = False
    targeted: bool = False
    method_development: bool = False
    reporter_channels: tuple[str, ...] = ()


@dataclass
class BranchSeed:
    accession: str
    seed_id: str
    source_kind: str
    source_ref: str
    modality: str
    acquisition: list[str]
    chemistry: list[str]
    analytical_channels: list[str]
    carrier_channels: list[str]
    blank_channels: list[str]
    reference_channels: list[str]
    expected_analytical_count: int | None
    confidence: str
    evidence_text: str


@dataclass
class BranchContract:
    accession: str
    branch_id: str
    modality: str
    acquisition: list[str]
    chemistry: list[str]
    analytical_channels: list[str]
    carrier_channels: list[str]
    blank_channels: list[str]
    reference_channels: list[str]
    expected_analytical_count: int | None
    source_kinds: list[str]
    source_refs: list[str]
    confidence: str
    generation_eligibility: str
    blocker: str
    evidence_count: int


@dataclass
class FileMembership:
    accession: str
    branch_id: str
    file_name: str
    file_category: str
    membership: str
    score: int
    confidence: str
    matched_features: list[str]
    reason: str


@dataclass
class JoinEvidence:
    accession: str
    source_file: str
    source_location: str
    row_index: int
    repository_raw: str
    join_method: str
    join_confidence: str
    raw_token: str
    channels: list[str]
    sample_tokens: list[str]
    cell_tokens: list[str]
    schema_fields: list[str]
    evidence_text: str


@dataclass
class ExternalFetch:
    accession: str
    source_type: str
    source_ref: str
    source_url: str
    resolved_url: str
    repository_path: str
    local_path: str
    archive_member: str
    size_bytes: int
    sha256: str
    parse_status: str
    parser: str
    structural_hits: int


@dataclass
class RelationAssessment:
    accession_a: str
    accession_b: str
    title_similarity: float
    raw_a: int
    raw_b: int
    shared_raws: int
    a_fraction_shared: float
    b_fraction_shared: float
    raw_jaccard: float
    announced_a: str
    announced_b: str
    relation_class: str
    confidence: str
    generation_policy: str
    reason: str


def _ordered_unique(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def feature_text(text: str) -> str:
    # Repository filenames frequently encode branch semantics in camelCase/underscore tokens.
    # Normalize those separators before applying the same reusable scientific vocabulary used for prose.
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", str(text or ""))
    text = re.sub(r"[_-]+", " ", text)
    return norm(text)


def features(text: str) -> SourceFeatures:
    text = feature_text(text)
    acq = tuple(name for name, pat in ACQ_PATTERNS if pat.search(text))
    chem = tuple(name for name, pat in CHEM_PATTERNS if pat.search(text))
    reporter = tuple(channels_in(text)) if (chem or re.search(r"(?i)(?:reporter|channel|label)", text)) else ()
    return SourceFeatures(
        acquisition=acq,
        chemistry=chem,
        single_cell=bool(SINGLE_RE.search(text)),
        label_free=bool(LABEL_FREE_RE.search(text)),
        targeted=bool(TARGETED_RE.search(text)),
        method_development=bool(METHODDEV_RE.search(text)),
        reporter_channels=reporter,
    )


def modality_from_features(f: SourceFeatures, contract: DesignContract | None = None) -> str:
    if contract and (contract.analytical_channels or contract.carrier_channels or contract.reference_channels):
        return "reporter_multiplexed_single_cell" if f.single_cell or contract.expected_analytical_count else "reporter_multiplexed"
    if f.targeted and (f.chemistry or f.reporter_channels):
        return "targeted_reporter_single_cell" if f.single_cell else "targeted_reporter"
    if f.label_free or (f.acquisition and not f.chemistry):
        return "label_free_single_cell" if f.single_cell else "label_free"
    if f.single_cell and f.chemistry:
        return "reporter_multiplexed_single_cell"
    if f.single_cell:
        return "single_cell_modality_unresolved"
    return "modality_unresolved"


def publication_chunks(text: str) -> list[str]:
    """Return bounded source neighborhoods suitable for generic branch discovery."""
    chunks = [norm(x) for x in re.split(r"\n\s*\n|(?<=[.;])\s+(?=[A-Z])", text) if norm(x)]
    out: list[str] = []
    for i, chunk in enumerate(chunks):
        f = features(chunk)
        if f.single_cell or f.chemistry or f.label_free or f.targeted:
            out.append(norm(" ".join(chunks[max(0, i - 1):min(len(chunks), i + 2)])))
    return _ordered_unique(out)


def contract_seed(contract: DesignContract) -> BranchSeed:
    f = features(contract.evidence_text)
    # Chemistry from the parser is authoritative for this source block when present. Acquisition
    # words from neighboring prose are not branch-defining for an isobaric design contract unless
    # the same block is explicitly targeted (e.g. SureQuant/PRM).
    chem = _ordered_unique([contract.chemistry.lower()] + list(f.chemistry)) if contract.chemistry else list(f.chemistry)
    acquisition = list(f.acquisition) if f.targeted else []
    return BranchSeed(
        accession=contract.accession,
        seed_id=contract.contract_id,
        source_kind=contract.source_kind,
        source_ref=contract.source_ref,
        modality=modality_from_features(f, contract),
        acquisition=acquisition,
        chemistry=chem,
        analytical_channels=list(contract.analytical_channels),
        carrier_channels=list(contract.carrier_channels),
        blank_channels=list(contract.blank_channels),
        reference_channels=list(contract.reference_channels),
        expected_analytical_count=contract.expected_analytical_count,
        confidence=contract.confidence,
        evidence_text=contract.evidence_text,
    )


def chunk_seed(accession: str, ref: str, idx: int, chunk: str) -> BranchSeed | None:
    f = features(chunk)
    modality = modality_from_features(f)
    if modality == "modality_unresolved" and not f.method_development:
        return None
    confidence = "high" if f.single_cell and (f.chemistry or f.label_free or f.targeted) else "medium"
    return BranchSeed(
        accession=accession,
        seed_id=f"{accession}:publication_context:{idx}",
        source_kind="publication_context",
        source_ref=ref,
        modality=modality,
        acquisition=list(f.acquisition),
        chemistry=list(f.chemistry),
        analytical_channels=[],
        carrier_channels=[],
        blank_channels=[],
        reference_channels=[],
        expected_analytical_count=None,
        confidence=confidence,
        evidence_text=chunk[:1600],
    )


def file_seed(accession: str, file_name: str, category: str) -> BranchSeed | None:
    f = features(file_name)
    if not (f.acquisition or f.chemistry or f.single_cell or f.label_free or f.targeted):
        return None
    modality = modality_from_features(f)
    if f.method_development and not f.single_cell:
        if f.targeted:
            modality = "targeted_method_development"
        elif f.acquisition:
            modality = "method_development"
    return BranchSeed(
        accession=accession,
        seed_id=f"{accession}:repository_file:{hashlib.sha1(file_name.encode()).hexdigest()[:10]}",
        source_kind="repository_filename",
        source_ref=file_name,
        modality=modality,
        acquisition=list(f.acquisition),
        chemistry=list(f.chemistry),
        analytical_channels=[], carrier_channels=[], blank_channels=[], reference_channels=[],
        expected_analytical_count=None,
        confidence="high" if f.single_cell or (f.acquisition and f.chemistry) else "medium",
        evidence_text=f"{category}:{file_name}",
    )


def _seed_signature(seed: BranchSeed) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    # Reporter chemistry and acquisition are branch-defining.  For otherwise unresolved single-cell
    # contexts, modality itself keeps the seed separate until stronger evidence arrives.
    chemistry = tuple(sorted(x.lower() for x in seed.chemistry))
    acquisition = tuple(sorted(seed.acquisition))
    modality_family = seed.modality
    if seed.modality in {"reporter_multiplexed", "reporter_multiplexed_single_cell"}:
        modality_family = "reporter_multiplexed"
    elif seed.modality in {"label_free", "label_free_single_cell"}:
        modality_family = "label_free"
    return modality_family, chemistry, acquisition


def _compatible(a: BranchSeed, b: BranchSeed) -> bool:
    ma, ca, aa = _seed_signature(a)
    mb, cb, ab = _seed_signature(b)
    if ma != mb:
        return False
    if ca and cb and not (set(ca) & set(cb)):
        return False
    if aa and ab and not (set(aa) & set(ab)):
        return False
    # Distinct complete reporter layouts in the same chemistry are separate branches when role sets
    # disagree.  This handles multiple plex designs without naming a particular dataset.
    roles_a = (set(a.analytical_channels), set(a.carrier_channels), set(a.blank_channels), set(a.reference_channels))
    roles_b = (set(b.analytical_channels), set(b.carrier_channels), set(b.blank_channels), set(b.reference_channels))
    if any(roles_a) and any(roles_b):
        for xa, xb in zip(roles_a, roles_b):
            if xa and xb and xa != xb:
                return False
    return True


def merge_branch_seeds(accession: str, seeds: list[BranchSeed]) -> list[BranchContract]:
    groups: list[list[BranchSeed]] = []
    for seed in sorted(seeds, key=lambda s: (s.source_kind != "publication", s.seed_id)):
        placed = False
        for group in groups:
            if all(_compatible(seed, existing) for existing in group):
                group.append(seed); placed = True; break
        if not placed:
            groups.append([seed])

    contracts: list[BranchContract] = []
    for i, group in enumerate(groups, start=1):
        modalities = Counter(s.modality for s in group)
        # Prefer the most scientifically specific modality in the group.
        priority = [
            "targeted_reporter_single_cell", "reporter_multiplexed_single_cell",
            "label_free_single_cell", "single_cell_modality_unresolved",
            "targeted_reporter", "reporter_multiplexed", "label_free",
            "targeted_method_development", "method_development", "modality_unresolved",
        ]
        modality = next((m for m in priority if modalities[m]), modalities.most_common(1)[0][0])
        chemistry = _ordered_unique(x for s in group for x in s.chemistry)
        acquisition = _ordered_unique(x for s in group for x in s.acquisition)
        analytical = _ordered_unique(x for s in group for x in s.analytical_channels)
        carrier = _ordered_unique(x for s in group for x in s.carrier_channels)
        blank = _ordered_unique(x for s in group for x in s.blank_channels)
        reference = _ordered_unique(x for s in group for x in s.reference_channels)
        expected = next((s.expected_analytical_count for s in group if s.expected_analytical_count), None)
        high = sum(s.confidence == "high" for s in group)
        confidence = "high" if high and (len(group) >= 2 or analytical or modality.endswith("single_cell")) else "medium"
        design_closed = bool(analytical and (carrier or reference or blank))
        if modality in {"label_free_single_cell", "label_free"}:
            eligibility, blocker = "branch_file_mapping_open", "branch file/sample mapping required"
        elif design_closed:
            eligibility, blocker = "design_closed_file_mapping_open", "branch file/sample/channel mapping required"
        else:
            eligibility, blocker = "source_graph_open", "branch design and/or file mapping incomplete"
        token = "_".join(chemistry or acquisition or [modality])
        token = re.sub(r"[^a-z0-9]+", "_", token.lower()).strip("_")[:40] or "branch"
        branch_id = f"branch_{i:02d}_{token}"
        contracts.append(BranchContract(
            accession=accession, branch_id=branch_id, modality=modality,
            acquisition=acquisition, chemistry=chemistry,
            analytical_channels=analytical, carrier_channels=carrier,
            blank_channels=blank, reference_channels=reference,
            expected_analytical_count=expected,
            source_kinds=_ordered_unique(s.source_kind for s in group),
            source_refs=_ordered_unique(s.source_ref for s in group),
            confidence=confidence, generation_eligibility=eligibility,
            blocker=blocker, evidence_count=len(group),
        ))
    return contracts


def _membership_score(branch: BranchContract, file_name: str, category: str) -> tuple[int, list[str]]:
    f = features(file_name)
    score = 0; matched: list[str] = []
    bchem = {x.lower() for x in branch.chemistry}; fchem = {x.lower() for x in f.chemistry}
    bacq = set(branch.acquisition); facq = set(f.acquisition)
    if bchem and fchem:
        if bchem & fchem: score += 5; matched.append("chemistry")
        else: score -= 5
    elif bchem and re.search(r"(?i)(?:tmt|itraq|reporter)", file_name):
        score -= 2
    if bacq and facq:
        if bacq & facq: score += 4; matched.append("acquisition")
        else: score -= 3
    if branch.modality.endswith("single_cell") and f.single_cell:
        score += 4; matched.append("single_cell")
    if branch.modality.startswith("label_free") and (f.label_free or (facq and not fchem)):
        score += 3; matched.append("label_free")
    if branch.modality.startswith("targeted") and f.targeted:
        score += 3; matched.append("targeted")
    if "method_development" in branch.modality and f.method_development:
        score += 2; matched.append("method_development")
    # Branch source references can themselves contain explicit file-family tokens.  Match only long
    # alphanumeric tokens, never arbitrary accession strings.
    source_text = " ".join(branch.source_refs)
    stem_tokens = [t.lower() for t in re.findall(r"[A-Za-z0-9]{5,}", source_text) if not PXD_RE.fullmatch(t)]
    low = file_name.lower()
    shared = [t for t in stem_tokens if t in low and t not in {"single", "cells", "label", "report", "publication", "repository"}]
    if shared:
        score += min(4, len(set(shared))); matched.append("source_family_token")
    if category.upper() == "RAW" and score > 0:
        score += 1
    return score, matched


def assign_files(accession: str, branches: list[BranchContract], snapshot: Path) -> list[FileMembership]:
    rows = repo_rows(snapshot, accession)
    out: list[FileMembership] = []
    for row in rows:
        scored = []
        for branch in branches:
            score, matched = _membership_score(branch, row.name, row.category)
            scored.append((score, branch, matched))
        scored.sort(key=lambda x: (-x[0], x[1].branch_id))
        best = scored[0][0] if scored else 0
        second = scored[1][0] if len(scored) > 1 else -999
        if best >= 6 and best - second >= 2:
            score, branch, matched = scored[0]
            out.append(FileMembership(accession, branch.branch_id, row.name, row.category, "member", score, "high", matched, "unique high-scoring source-feature match"))
        elif best >= 4:
            for score, branch, matched in scored:
                if score == best:
                    out.append(FileMembership(accession, branch.branch_id, row.name, row.category, "candidate", score, "medium", matched, "ambiguous/partial source-feature match"))
        else:
            out.append(FileMembership(accession, "", row.name, row.category, "unassigned", best, "low", [], "no branch-specific source evidence"))
    return out


def _normalize_raw_token(value: str) -> str:
    value = Path(value.strip().strip('"\'')).name.lower()
    value = re.sub(r"\.(?:raw|d|wiff2?|mzml|mzxml)$", "", value, flags=re.I)
    value = re.sub(r"[^a-z0-9]+", "", value)
    return value


def _row_tokens(row: dict[str, str]) -> tuple[list[str], list[str], list[str]]:
    raws: list[str] = []
    channels: list[str] = []
    samples: list[str] = []
    for key, value in row.items():
        k = norm(key).lower(); v = norm(value)
        if not v: continue
        if re.search(r"(?:raw|file|run|spectrum)", k):
            raws.extend(m.group(1) for m in RAW_TOKEN_RE.finditer(v))
            if RAW_EXT_RE.search(v): raws.append(v)
        if re.search(r"(?:channel|reporter|tmt|label)", k):
            channels.extend(channels_in(v))
        if re.search(r"(?:sample|cell|well|plex|batch|set|replicate|run)", k):
            samples.append(v)
    return _ordered_unique(raws), _ordered_unique(channels), _ordered_unique(samples)


def parse_tabular_rows(data: bytes, name: str) -> tuple[list[str], list[dict[str, str]]]:
    try:
        text = data.decode("utf-8-sig", errors="replace")
    except Exception:
        return [], []
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters="\t,;")
    except Exception:
        dialect = csv.excel_tab if "\t" in sample else csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    fields = [norm(x) for x in (reader.fieldnames or []) if norm(x)]
    rows: list[dict[str, str]] = []
    for i, row in enumerate(reader):
        if i >= 5000: break
        rows.append({norm(k): norm(v) for k, v in row.items() if k is not None})
    return fields, rows


def _parse_archive(path: Path, max_archive_bytes: int) -> list[tuple[str, bytes]]:
    members: list[tuple[str, bytes]] = []
    if path.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(path) as zf:
                for info in zf.infolist():
                    if info.is_dir() or info.file_size > max_archive_bytes: continue
                    suffix = Path(info.filename).suffix.lower()
                    if suffix not in EXTERNAL_EXTS - {".zip", ".7z"}: continue
                    if not EXTERNAL_PATH_RE.search(info.filename): continue
                    members.append((info.filename, zf.read(info)))
        except Exception:
            pass
    elif path.suffix.lower() == ".7z":
        # Prefer py7zr; fall back to 7z/7za if available.  Extraction is bounded by path selection and
        # the caller's archive size limit.
        try:
            import py7zr  # type: ignore
            with tempfile.TemporaryDirectory() as td:
                with py7zr.SevenZipFile(path, mode="r") as zf:
                    names = [n for n in zf.getnames() if Path(n).suffix.lower() in EXTERNAL_EXTS - {".zip", ".7z"} and EXTERNAL_PATH_RE.search(n)]
                    zf.extract(path=td, targets=names[:100])
                for n in names[:100]:
                    p = Path(td) / n
                    if p.is_file() and p.stat().st_size <= max_archive_bytes:
                        members.append((n, p.read_bytes()))
        except Exception:
            exe = shutil.which("7z") or shutil.which("7za")
            if exe:
                try:
                    listing = subprocess.run([exe, "l", "-ba", str(path)], capture_output=True, text=True, timeout=30).stdout
                    names = []
                    for line in listing.splitlines():
                        parts = line.split()
                        if len(parts) >= 6:
                            n = " ".join(parts[5:])
                            if Path(n).suffix.lower() in EXTERNAL_EXTS - {".zip", ".7z"} and EXTERNAL_PATH_RE.search(n): names.append(n)
                    for n in names[:100]:
                        proc = subprocess.run([exe, "x", "-so", str(path), n], capture_output=True, timeout=60)
                        if proc.returncode == 0 and len(proc.stdout) <= max_archive_bytes:
                            members.append((n, proc.stdout))
                except Exception:
                    pass
    return members


def parse_join_evidence(accession: str, source_file: str, source_location: str, data: bytes, repository_raws: list[str]) -> list[JoinEvidence]:
    suffix = Path(source_file).suffix.lower()
    if suffix not in {".txt", ".tsv", ".csv"}:
        return []
    fields, rows = parse_tabular_rows(data, source_file)
    if not rows: return []
    raw_by_norm: dict[str, list[str]] = defaultdict(list)
    for raw in repository_raws:
        raw_by_norm[_normalize_raw_token(raw)].append(raw)
    out: list[JoinEvidence] = []
    for idx, row in enumerate(rows, start=2):
        raw_tokens, chans, samples = _row_tokens(row)
        # Also accept a full RAW-like value in any column if present.
        for value in row.values():
            raw_tokens.extend(m.group(1) for m in RAW_TOKEN_RE.finditer(value))
        raw_tokens = _ordered_unique(raw_tokens)
        matches: list[tuple[str, str, str]] = []
        for token in raw_tokens:
            base = Path(token).name
            if base in repository_raws:
                matches.append((base, "exact_repository_raw", "high")); continue
            n = _normalize_raw_token(token)
            candidates = raw_by_norm.get(n, [])
            if len(candidates) == 1:
                matches.append((candidates[0], "normalized_raw_basename", "high"))
        # Generic composite-key fallback: only if one repository RAW uniquely contains every long
        # alphanumeric token from a run/sample value.  This never uses row order.
        if not matches:
            for sample in samples:
                toks = [x.lower() for x in re.findall(r"[A-Za-z0-9]{4,}", sample) if not x.isdigit()]
                if not toks: continue
                candidates = [raw for raw in repository_raws if all(t in raw.lower() for t in toks)]
                if len(candidates) == 1:
                    matches.append((candidates[0], "unique_composite_source_key", "medium"))
        for raw, method, conf in _ordered_unique(matches):  # type: ignore[arg-type]
            cell_tokens = [s for s in samples if re.search(r"(?i)(?:cell|ecto|endo|meso|unsorted|gfp|zygote|oocyte|neuron|well)", s)]
            out.append(JoinEvidence(
                accession, source_file, source_location, idx, raw, method, conf,
                raw_tokens[0] if raw_tokens else "", chans, samples, cell_tokens, fields,
                norm(" | ".join(f"{k}={v}" for k, v in row.items()))[:1600],
            ))
    return out


def fetch_external_sources(
    sources: list[ExternalSource], output: Path, max_files: int, max_bytes: int, max_archive_bytes: int,
    reuse_roots: list[Path], snapshot: Path,
) -> tuple[list[ExternalFetch], list[JoinEvidence], list[StructuredEvidence]]:
    inventory: list[ExternalFetch] = []
    joins: list[JoinEvidence] = []
    structured: list[StructuredEvidence] = []
    seen_repo: dict[str, list[Path]] = {}

    def record_path(src: ExternalSource, path: Path, *, resolved_url: str, repository_path: str,
                    parse_status: str, archive_member: str = "") -> None:
        parser = ""
        ev: list[StructuredEvidence] = []
        try:
            data = path.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            size = len(data)
        except OSError:
            return
        try:
            parser, ev = parse_artifact(src.accession, path)
        except Exception as exc:
            parser = f"parse_error:{type(exc).__name__}"
            ev = []
        structured.extend(ev)
        repo_raws = raw_files(snapshot, src.accession)
        joins.extend(parse_join_evidence(src.accession, path.name, str(path), data, repo_raws))
        inventory.append(ExternalFetch(
            src.accession, src.source_type, src.source_ref, src.url, resolved_url, repository_path,
            str(path), archive_member, size, sha, parse_status, parser, len(ev),
        ))

    def record_archive(src: ExternalSource, path: Path, *, resolved_url: str) -> None:
        if path.suffix.lower() not in {".zip", ".7z"} or path.stat().st_size > max_archive_bytes:
            return
        repo_raws = raw_files(snapshot, src.accession)
        member_root = output / "_archive_members" / src.accession / hashlib.sha256(str(path).encode()).hexdigest()[:12]
        for member, blob in _parse_archive(path, max_archive_bytes):
            if len(blob) > max_bytes:
                inventory.append(ExternalFetch(
                    src.accession, src.source_type, src.source_ref, src.url, resolved_url, path.name,
                    str(path), member, len(blob), hashlib.sha256(blob).hexdigest(),
                    f"archive_member_too_large:{len(blob)}", "", 0,
                ))
                continue
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(member).name) or "archive_member"
            member_path = member_root / safe
            member_path.parent.mkdir(parents=True, exist_ok=True)
            member_path.write_bytes(blob)
            parser = ""
            ev: list[StructuredEvidence] = []
            try:
                parser, ev = parse_artifact(src.accession, member_path)
            except Exception as exc:
                parser = f"parse_error:{type(exc).__name__}"
            structured.extend(ev)
            joins.extend(parse_join_evidence(
                src.accession, Path(member).name, f"{path}!{member}", blob, repo_raws
            ))
            inventory.append(ExternalFetch(
                src.accession, src.source_type, src.source_ref, src.url, resolved_url, path.name,
                str(member_path), member, len(blob), hashlib.sha256(blob).hexdigest(),
                "archive_member", parser, len(ev),
            ))

    for src in sources:
        if src.source_type == "github":
            key = src.url.rstrip("/")
            files = seen_repo.get(key)
            if files is None:
                files = []
                # Reuse already downloaded analysis files first.
                owner_repo = github_repo_parts(src.url)
                if owner_repo:
                    owner, repo = owner_repo
                    for root in reuse_roots:
                        p = root / owner / repo
                        if p.is_dir():
                            files.extend(
                                x for x in p.rglob("*")
                                if x.is_file() and x.suffix.lower() in EXTERNAL_EXTS
                                and EXTERNAL_PATH_RE.search(str(x.relative_to(p)))
                            )
                if not files:
                    files = fetch_github_high_value(src, output, max_files, max_bytes)
                seen_repo[key] = files[:max_files]
            repo_raws = raw_files(snapshot, src.accession)
            for path in files[:max_files]:
                record_path(
                    src, path, resolved_url=src.url, repository_path=path.name, parse_status="selected",
                )
                record_archive(src, path, resolved_url=src.url)
            continue

        source_key = hashlib.sha256(src.url.encode("utf-8", errors="replace")).hexdigest()[:12]
        provider_dest = output / src.accession / source_key
        acquired = acquire_external_artifacts(
            source_type=src.source_type,
            source_url=src.url,
            dest=provider_dest,
            max_files=max_files,
            max_bytes=max_bytes,
            max_archive_bytes=max_archive_bytes,
        )
        for art in acquired:
            if art.local_path:
                path = Path(art.local_path)
                record_path(
                    src, path, resolved_url=art.resolved_url, repository_path=art.remote_name,
                    parse_status=art.status,
                )
                record_archive(src, path, resolved_url=art.resolved_url)
            else:
                inventory.append(ExternalFetch(
                    src.accession, src.source_type, src.source_ref, src.url, art.resolved_url,
                    art.remote_name, "", "", art.size_bytes, art.sha256, art.status, "", 0,
                ))
    return inventory, joins, structured


def project_announce(snapshot: Path, accession: str) -> str:
    obj = project_json(snapshot, accession)
    vals: list[str] = []
    def walk(x: Any) -> None:
        if isinstance(x, dict):
            for k, v in x.items():
                if isinstance(v, str) and re.search(r"(?i)(?:announce|publish|submission|date)", str(k)) and re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T.*)?", v): vals.append(v[:10])
                walk(v)
        elif isinstance(x, list):
            for y in x: walk(y)
    walk(obj)
    return min(vals) if vals else ""


def assess_relations(snapshot: Path, accessions: list[str]) -> list[RelationAssessment]:
    out: list[RelationAssessment] = []
    for rel in relation_candidates(snapshot, accessions):
        a, b = rel.accession_a, rel.accession_b
        ra, rb = set(raw_files(snapshot, a)), set(raw_files(snapshot, b))
        if not ra or not rb: continue
        shared = ra & rb
        fa, fb = len(shared) / len(ra), len(shared) / len(rb)
        aa, ab = project_announce(snapshot, a), project_announce(snapshot, b)
        relation_class = rel.relation_class
        confidence = "medium"
        policy = "review_before_cross_accession_generation"
        reason = rel.reason
        # Generic predecessor/expanded-redeposit heuristic: high title identity plus strong one-way RAW
        # containment and compatible chronology.  No accession identity participates in the rule.
        if rel.title_similarity >= 0.9 and max(fa, fb) >= 0.85 and len(shared) >= 5:
            earlier, later = (a, b)
            earlier_fraction = fa
            if aa and ab and ab < aa:
                earlier, later = b, a; earlier_fraction = fb
            elif len(rb) < len(ra):
                earlier, later = b, a; earlier_fraction = fb
            if earlier_fraction >= 0.85:
                relation_class = "probable_predecessor_expanded_redeposit"
                confidence = "high"
                policy = f"block_earlier_until_relationship_resolved:{earlier};prefer_larger_or_later:{later}"
                reason = "near-identical project identity plus strong RAW-set containment; review as predecessor/expanded redeposit rather than independent studies"
        out.append(RelationAssessment(a,b,rel.title_similarity,len(ra),len(rb),len(shared),fa,fb,len(shared)/len(ra|rb),aa,ab,relation_class,confidence,policy,reason))
    return out


def branch_join_status(branch: BranchContract, memberships: list[FileMembership], joins: list[JoinEvidence]) -> tuple[str, str]:
    member_raws = {m.file_name for m in memberships if m.branch_id == branch.branch_id and m.file_category.upper() == "RAW" and m.membership == "member"}
    joined = {j.repository_raw for j in joins if j.repository_raw in member_raws}
    if member_raws and joined == member_raws and all(j.join_confidence == "high" for j in joins if j.repository_raw in member_raws):
        if branch.modality.startswith("reporter") or branch.modality.startswith("targeted_reporter"):
            channel_rows = [j for j in joins if j.repository_raw in member_raws and j.channels]
            if len({j.repository_raw for j in channel_rows}) == len(member_raws):
                return "explicit_row_mapping_candidate", "all branch RAWs have high-confidence source joins with channel evidence"
        else:
            return "explicit_row_mapping_candidate", "all branch RAWs have high-confidence source joins"
    if member_raws and joined:
        return "mapping_partial", f"joined {len(joined)}/{len(member_raws)} branch RAWs"
    return branch.generation_eligibility, branch.blocker


def _write_dataclasses(path: Path, objects: Iterable[Any], fields: list[str]) -> None:
    write_tsv(path, (asdict(x) for x in objects), fields)


def run(args: argparse.Namespace) -> int:
    accessions = read_accessions(args.accessions_file)
    wanted = set(accessions)
    out = args.output; out.mkdir(parents=True, exist_ok=True)
    pub = manifest_texts(args.publication_manifest, wanted)

    all_designs: list[DesignContract] = []
    seeds_by: dict[str, list[BranchSeed]] = defaultdict(list)
    external: list[ExternalSource] = supplementary_sources(args.supplementary_links, wanted) if args.supplementary_links and args.supplementary_links.is_file() else []

    for acc in accessions:
        for ref, text in pub.get(acc, []):
            designs = extract_design_contracts(acc, ref, text)
            all_designs.extend(designs)
            seeds_by[acc].extend(contract_seed(c) for c in designs)
            for idx, chunk in enumerate(publication_chunks(text), start=1):
                seed = chunk_seed(acc, ref, idx, chunk)
                if seed: seeds_by[acc].append(seed)
            external.extend(extract_external_sources(acc, ref, text))
        for row in repo_rows(args.snapshot, acc):
            seed = file_seed(acc, row.name, row.category)
            if seed: seeds_by[acc].append(seed)

    # Deduplicate source-discovered external repositories without making accession-specific choices.
    ext_unique: list[ExternalSource] = []
    seen = set()
    for e in external:
        key = (e.accession, e.source_type, e.url.rstrip("/"))
        if e.url and key not in seen:
            seen.add(key); ext_unique.append(e)
    external = ext_unique

    branches: list[BranchContract] = []
    memberships: list[FileMembership] = []
    for acc in accessions:
        bs = merge_branch_seeds(acc, seeds_by[acc])
        branches.extend(bs)
        memberships.extend(assign_files(acc, bs, args.snapshot))

    # Generic structured repository artifacts.
    structured: list[StructuredEvidence] = []
    artifact_rows: list[dict[str, Any]] = []
    reuse_roots = [p for p in args.reuse_root if p.is_dir()]
    for acc in accessions:
        candidates = []
        for row in repo_rows(args.snapshot, acc):
            pri, reason = artifact_priority(row)
            if pri > 0: candidates.append((pri, row, reason))
        candidates.sort(key=lambda x: (-x[0], x[1].name.lower()))
        for pri, row, reason in candidates[:args.max_artifacts_per_accession]:
            path, acq = acquire(row, out / "structured_artifacts" / acc, args.max_artifact_bytes, reuse_roots)
            parser = ""; ev: list[StructuredEvidence] = []
            if path:
                parser, ev = parse_artifact(acc, path); structured.extend(ev)
            artifact_rows.append({"accession":acc,"file_name":row.name,"priority":pri,"reason":reason,"acquire_status":acq,"local_path":str(path or ""),"parser":parser,"structural_hits":len(ev)})

    # Generic external analysis repository acquisition/join resolution.
    ext_inventory: list[ExternalFetch] = []
    joins: list[JoinEvidence] = []
    external_structured: list[StructuredEvidence] = []
    if args.fetch_external_analysis:
        ext_inventory, joins, external_structured = fetch_external_sources(
            external, out / "external_analysis", args.max_external_files, args.max_external_bytes,
            args.max_archive_bytes, args.reuse_external_root, args.snapshot
        )
        structured.extend(external_structured)

    # Structured evidence may include exact RAW/channel rows too; convert them into generic joins.
    raw_sets = {acc: raw_files(args.snapshot, acc) for acc in accessions}
    for ev in structured:
        for token in ev.raw_files:
            base = Path(token).name
            raw = base if base in raw_sets[ev.accession] else ""
            if not raw:
                n = _normalize_raw_token(base)
                cands = [r for r in raw_sets[ev.accession] if _normalize_raw_token(r) == n]
                if len(cands) == 1: raw = cands[0]
            if raw:
                joins.append(JoinEvidence(ev.accession,ev.file_name,ev.source_location,0,raw,"structured_exact_or_normalized_raw","high",token,list(ev.channels),list(ev.sample_tokens),[],list(ev.schema_fields),ev.text[:1600]))

    branch_rows = []
    for b in branches:
        ms = [m for m in memberships if m.accession == b.accession and m.branch_id == b.branch_id]
        js = [j for j in joins if j.accession == b.accession]
        status, blocker = branch_join_status(b, ms, js)
        member_raw = sum(m.membership == "member" and m.file_category.upper() == "RAW" for m in ms)
        cand_raw = sum(m.membership == "candidate" and m.file_category.upper() == "RAW" for m in ms)
        joined_raw = len({j.repository_raw for j in js if any(m.file_name == j.repository_raw and m.membership == "member" for m in ms)})
        branch_rows.append({**asdict(b),"member_raw_files":member_raw,"candidate_raw_files":cand_raw,"joined_member_raw_files":joined_raw,"resolved_status":status,"resolved_blocker":blocker})

    relations = assess_relations(args.snapshot, accessions)
    blocked_accessions = set()
    for r in relations:
        if r.relation_class == "probable_predecessor_expanded_redeposit" and r.generation_policy.startswith("block_earlier"):
            m = re.search(r"block_earlier_until_relationship_resolved:(PXD\d+)", r.generation_policy)
            if m: blocked_accessions.add(m.group(1))

    accession_rows = []
    for acc in accessions:
        bs = [r for r in branch_rows if r["accession"] == acc]
        rawset = set(raw_sets[acc])
        assigned = {m.file_name for m in memberships if m.accession == acc and m.file_category.upper()=="RAW" and m.membership in {"member","candidate"}}
        statuses = Counter(r["resolved_status"] for r in bs)
        status = "generation_blocked_accession_integrity" if acc in blocked_accessions else ("explicit_row_mapping_candidate" if statuses["explicit_row_mapping_candidate"] else "multi_branch_mapping_open" if len(bs)>1 else "single_branch_mapping_open")
        accession_rows.append({"accession":acc,"branches":len(bs),"modalities":"|".join(sorted({r['modality'] for r in bs})),"repository_raw_files":len(rawset),"assigned_raw_files":len(assigned),"unassigned_raw_files":len(rawset-assigned),"explicit_row_mapping_candidate_branches":statuses["explicit_row_mapping_candidate"],"accession_status":status})

    _write_dataclasses(out/"design_contracts.tsv", all_designs, list(DesignContract.__dataclass_fields__))
    _write_dataclasses(out/"branch_seeds.tsv", [s for acc in accessions for s in seeds_by[acc]], list(BranchSeed.__dataclass_fields__))
    _write_dataclasses(out/"branch_contracts.tsv", branches, list(BranchContract.__dataclass_fields__))
    _write_dataclasses(out/"branch_file_membership.tsv", memberships, list(FileMembership.__dataclass_fields__))
    _write_dataclasses(out/"join_evidence.tsv", joins, list(JoinEvidence.__dataclass_fields__))
    _write_dataclasses(out/"external_analysis_fetch_inventory.tsv", ext_inventory, list(ExternalFetch.__dataclass_fields__))
    _write_dataclasses(out/"relation_assessments.tsv", relations, list(RelationAssessment.__dataclass_fields__))
    write_tsv(out/"structured_artifact_inventory.tsv", artifact_rows, ["accession","file_name","priority","reason","acquire_status","local_path","parser","structural_hits"])
    write_tsv(out/"branch_resolution.tsv", branch_rows, list(BranchContract.__dataclass_fields__) + ["member_raw_files","candidate_raw_files","joined_member_raw_files","resolved_status","resolved_blocker"])
    write_tsv(out/"accession_summary.tsv", accession_rows, ["accession","branches","modalities","repository_raw_files","assigned_raw_files","unassigned_raw_files","explicit_row_mapping_candidate_branches","accession_status"])

    summary = {
        "auditor_version": VERSION,
        "accessions": len(accessions),
        "branch_contracts": len(branches),
        "branch_modalities": dict(sorted(Counter(b.modality for b in branches).items())),
        "branch_resolution_counts": dict(sorted(Counter(r["resolved_status"] for r in branch_rows).items())),
        "accession_status_counts": dict(sorted(Counter(r["accession_status"] for r in accession_rows).items())),
        "external_analysis_sources": len(external),
        "external_source_types": dict(sorted(Counter(e.source_type for e in external).items())),
        "external_files": sum(bool(x.local_path) for x in ext_inventory),
        "external_fetch_inventory_rows": len(ext_inventory),
        "external_structured_evidence_rows": len(external_structured),
        "external_structured_parsers": dict(sorted(Counter(x.parser for x in ext_inventory if x.parser).items())),
        "join_evidence_rows": len(joins),
        "relation_assessments": len(relations),
        "runtime_accession_specific_rules": False,
        "gt_metadata_used": False,
        "non_generative": True,
        "outputs": {
            "branches": str(out/"branch_contracts.tsv"),
            "membership": str(out/"branch_file_membership.tsv"),
            "joins": str(out/"join_evidence.tsv"),
            "relations": str(out/"relation_assessments.tsv"),
            "branch_resolution": str(out/"branch_resolution.tsv"),
            "accession_summary": str(out/"accession_summary.tsv"),
        },
    }
    (out/"generalized_evidence_graph_summary.json").write_text(json.dumps(summary, indent=2)+"\n")
    print(json.dumps(summary, indent=2))
    for r in accession_rows:
        print(f"{r['accession']} branches={r['branches']} modalities={r['modalities'] or '-'} raw={r['repository_raw_files']} assigned={r['assigned_raw_files']} unassigned={r['unassigned_raw_files']} candidates={r['explicit_row_mapping_candidate_branches']} status={r['accession_status']}")
    return 0


def self_test() -> None:
    # No real accession is used here: runtime behavior must be invariant to identifier choice.
    a = "PXD900001"
    pub = (
        "For the TMT6plex set, single cells were labeled with 126, 127, 128 and 129 channels; "
        "the carrier sample was labeled with TMT 131 channel and the blank used TMT 130 channel. "
        "A separate label-free DIA gradient was used for method optimization."
    )
    designs = extract_design_contracts(a, "paper", pub)
    seeds = [contract_seed(x) for x in designs]
    seeds.extend(x for i,c in enumerate(publication_chunks(pub),1) if (x:=chunk_seed(a,"paper",i,c)))
    seeds.append(file_seed(a,"study_TMT6plexSingleCell_report.xlsx","SUPPORT"))
    seeds.append(file_seed(a,"study_DIAgradient_report.xlsx","SUPPORT"))
    branches = merge_branch_seeds(a, [x for x in seeds if x])
    assert any(b.modality.startswith("reporter_multiplexed") and set(b.analytical_channels) >= {"126","127","128","129"} and b.carrier_channels == ["131"] and b.blank_channels == ["130"] for b in branches), branches
    assert any(b.modality.startswith("label_free") for b in branches), branches

    # Generic join resolver: exact RAW and normalized basename work without row-order assumptions.
    data = b"Raw file\tChannel\tCell\nrun_A.raw\tTMT126\tcell_1\nrun-B.RAW\tTMT127N\tcell_2\n"
    joins = parse_join_evidence(a,"input.tsv","fixture",data,["run_A.raw","run-B.RAW"])
    assert {j.repository_raw for j in joins} == {"run_A.raw","run-B.RAW"}
    assert all(j.join_confidence == "high" for j in joins)

    # Branch membership depends on evidence features, not accession identity.
    b = next(x for x in branches if x.modality.startswith("reporter_multiplexed"))
    score, matched = _membership_score(b,"study_TMT6plexSingleCell_01.raw","RAW")
    assert score >= 6 and "chemistry" in matched

    # Source relation heuristic is content-driven.  Exact accession strings are arbitrary fixtures.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td); (root/"projects").mkdir(); (root/"files").mkdir()
        title = "Example single-cell targeted proteomics study"
        (root/"projects"/"PXD900010.json").write_text(json.dumps({"title":title,"announcementDate":"2025-01-01"}))
        (root/"projects"/"PXD900011.json").write_text(json.dumps({"title":title,"announcementDate":"2025-02-01"}))
        def dump(acc: str, names: list[str]):
            (root/"files"/f"{acc}.json").write_text(json.dumps([{"fileName":n,"fileCategory":{"name":"RAW"}} for n in names]))
        dump("PXD900010", [f"r{i}.raw" for i in range(10)])
        dump("PXD900011", [f"r{i}.raw" for i in range(10)] + ["extra1.raw","extra2.raw"])
        rels = assess_relations(root,["PXD900010","PXD900011"])
        assert rels and rels[0].relation_class == "probable_predecessor_expanded_redeposit", rels
    print("sdrf_generalized_evidence_graph self-test: PASS")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--accessions-file", type=Path)
    p.add_argument("--snapshot", type=Path)
    p.add_argument("--publication-manifest", type=Path)
    p.add_argument("--supplementary-links", type=Path)
    p.add_argument("--output", type=Path)
    p.add_argument("--reuse-root", type=Path, action="append", default=[])
    p.add_argument("--reuse-external-root", type=Path, action="append", default=[])
    p.add_argument("--max-artifacts-per-accession", type=int, default=12)
    p.add_argument("--max-artifact-bytes", type=int, default=100*1024*1024)
    p.add_argument("--fetch-external-analysis", action="store_true")
    p.add_argument("--max-external-files", type=int, default=24)
    p.add_argument("--max-external-bytes", type=int, default=25*1024*1024)
    p.add_argument("--max-archive-bytes", type=int, default=200*1024*1024)
    p.add_argument("--self-test", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()
    if args.self_test:
        self_test(); return 0
    required = [args.accessions_file,args.snapshot,args.publication_manifest,args.output]
    if any(x is None for x in required):
        raise SystemExit("--accessions-file, --snapshot, --publication-manifest and --output are required")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
