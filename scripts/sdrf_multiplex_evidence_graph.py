#!/usr/bin/env python3
"""Build a source-grounded evidence graph for difficult PRIDE single-cell proteomics designs.

This stage deliberately replaces the earlier "nearest reporter token to role word" model for the
remaining difficult studies.  It treats an SDRF row mapping as the closure of several independent
source-grounded contracts:

1. single-cell branch/modality (label-free vs reporter multiplexed vs mixed repository),
2. global reporter-role design (chemistry, analytical/carrier/blank/reference channel sets),
3. run/plex structure from repository analysis artifacts,
4. quantitative reporter-channel validation where structured abundance columns are available,
5. external analysis-code/data sources explicitly linked from a publication, and
6. cross-accession relation/integrity evidence for highly similar project records.

The stage is non-generative.  It may *authorize* a future explicit-row manifest only when the graph
closes, but it never writes SDRF rows itself.  It never reads GT/reference resources.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import sqlite3
import statistics
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from sdrf_external_artifact_acquisition import classify_external_source, europe_pmc_supplement_url

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from sdrf_reporter_run_scope_audit import (  # noqa: E402
    RepoFile,
    parse_support_file,
    public_http_uri,
    repository_files,
)

VERSION = "pride-scp-sdrf-multiplex-evidence-graph-v0.1"
PXD_RE = re.compile(r"\bPXD\d{6,}\b", re.I)
RAW_EXT_RE = re.compile(r"(?i)\.(?:raw|d|wiff|wiff2|mzml|mzxml)$")
URL_RE = re.compile(r"(?i)\b(?:https?://[^\s<>'\")\]]+|(?:www\.)?github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+|doi\.org/10\.5281/zenodo\.\d+)" )

TMT6 = ["126", "127", "128", "129", "130", "131"]
TMT10 = ["126", "127N", "127C", "128N", "128C", "129N", "129C", "130N", "130C", "131"]
TMTPRO16 = ["126", "127N", "127C", "128N", "128C", "129N", "129C", "130N", "130C", "131N", "131C", "132N", "132C", "133N", "133C", "134N"]
TMTPRO18 = TMTPRO16 + ["134C", "135N"]
CHANNEL_ORDER = TMTPRO18
CHANNEL_INDEX = {x: i for i, x in enumerate(CHANNEL_ORDER)}
CHANNEL_RE = re.compile(r"(?i)(?:\bTMT(?:pro)?\s*[-_]?\s*)?(12[6-9]|13[0-5])\s*([NC])?\b")

ISOBARIC_RE = re.compile(r"(?i)\b(?:tmt(?:pro)?|tandem\s+mass\s+tag|itraq|reporter\s+ion|carrier\s+channel|carrier\s+sample)\b")
LABEL_FREE_RE = re.compile(r"(?i)\b(?:label[- ]free|LFQ|label[- ]free\s+quantification|dda|wide[- ]window\s+acquisition|WWA)\b")
SINGLE_RE = re.compile(r"(?i)\b(?:single[- ]cell|single\s+cells?|single\s+zygote|single\s+zygotes|single\s+neuron|single\s+oocyte|single\s+HeLa)\b")
BULK_RE = re.compile(r"(?i)\b(?:bulk|40\s*cell|250\s*pg|400\s*ng|immunoprecipitation|AP[- ]MS|multi[- ]species|proteomix|benchmark)\b")

STRUCTURED_EXTS = {".xlsx", ".csv", ".tsv", ".txt", ".json", ".xml", ".sky", ".pdresult", ".pdstudy", ".msf"}
HIGH_STRUCTURED_RE = re.compile(r"(?i)(?:design|sample|metadata|annotation|characteristic|cellenone|channel|tmt|single|pgreport|skyline|\.sky$|\.pdstudy$)")
RESULT_TABLE_RE = re.compile(r"(?i)(?:protein|peptide|psm|realtimesearch|search[_ -]?result|quant(?:ification)?|pgreport)")
SCHEMA_SIGNAL_RE = re.compile(r"(?i)(?:sample|channel|reporter|quan|quant|label|file|raw|spectrum|study|workflow|mass[_ ]?tag|replicate)")
REPORTER_HEADER_RE = re.compile(r"(?i)(?:abundance|intensity|s/?n|reporter|quan|channel|tmt).{0,30}(12[6-9]|13[0-5])\s*([NC])?")
RAW_TOKEN_RE = re.compile(r"(?i)([^\s\t,;|]+\.(?:raw|d|wiff|wiff2|mzml|mzxml))")


@dataclass
class DesignContract:
    accession: str
    contract_id: str
    source_kind: str
    source_ref: str
    chemistry: str
    universe: list[str]
    analytical_channels: list[str]
    carrier_channels: list[str]
    blank_channels: list[str]
    reference_channels: list[str]
    excluded_channels: list[str]
    expected_analytical_count: int | None
    confidence: str
    complete_global_layout: bool
    evidence_text: str


@dataclass
class BranchContract:
    accession: str
    modality: str
    confidence: str
    single_isobaric_contexts: int
    single_label_free_contexts: int
    mixed_repository_contexts: int
    reporter_contracts: int
    complete_reporter_contracts: int
    reason: str


@dataclass
class ArtifactStatus:
    accession: str
    file_name: str
    category: str
    uri: str
    suffix: str
    priority: int
    reason: str
    acquired: bool
    acquire_status: str
    local_path: str
    parser: str
    structural_hits: int


@dataclass
class StructuredEvidence:
    accession: str
    file_name: str
    source_location: str
    evidence_type: str
    raw_files: list[str]
    channels: list[str]
    sample_tokens: list[str]
    schema_fields: list[str]
    numeric_summary: str
    text: str


@dataclass
class ExternalSource:
    accession: str
    source_type: str
    url: str
    source_ref: str
    confidence: str


@dataclass
class PublicationPromotion:
    accession: str
    candidate_title: str
    candidate_doi: str
    candidate_pmcid: str
    title_overlap: float
    exact_accession_verified: bool
    status: str
    source_url: str


@dataclass
class RelationCandidate:
    accession_a: str
    accession_b: str
    title_similarity: float
    raw_exact_overlap: int
    raw_a: int
    raw_b: int
    raw_jaccard: float
    relation_class: str
    reason: str


def norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def read_tsv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(errors="replace") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def write_tsv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, delimiter="\t", extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for row in rows:
            cooked = {}
            for key in fields:
                value = row.get(key, "")
                if isinstance(value, (list, tuple, set)):
                    value = ",".join(str(x) for x in value)
                elif isinstance(value, bool):
                    value = str(value).lower()
                cooked[key] = value
            w.writerow(cooked)


def read_accessions(path: Path) -> list[str]:
    return sorted({x.strip().upper() for x in path.read_text(errors="replace").splitlines() if re.fullmatch(r"PXD\d{6,}", x.strip(), re.I)})


def manifest_texts(path: Path, wanted: set[str]) -> dict[str, list[tuple[str, str]]]:
    out: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for row in read_tsv(path):
        acc = (row.get("accession") or "").strip().upper()
        if acc not in wanted:
            continue
        p = Path((row.get("publication_content_text_path") or "").strip())
        if not p.is_file():
            continue
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        ref = (row.get("publication_doi") or row.get("publication_title") or p.name).strip()
        out[acc].append((ref, text))
    return out


def project_json(snapshot: Path, accession: str) -> Any:
    for p in (snapshot / "projects" / f"{accession}.json", snapshot / "project" / f"{accession}.json", snapshot / f"{accession}.json"):
        if p.is_file():
            try:
                return json.loads(p.read_text(errors="replace"))
            except Exception:
                pass
    return {}


def project_title(snapshot: Path, accession: str) -> str:
    obj = project_json(snapshot, accession)
    if isinstance(obj, dict):
        for key in ("title", "projectTitle", "name"):
            if norm(obj.get(key)):
                return norm(obj.get(key))
    candidates: list[str] = []
    def walk(x: Any) -> None:
        if isinstance(x, dict):
            for k, v in x.items():
                if str(k).lower() in {"title", "projecttitle", "name"} and isinstance(v, str):
                    candidates.append(norm(v))
                walk(v)
        elif isinstance(x, list):
            for v in x: walk(v)
    walk(obj)
    return next((x for x in candidates if len(x) > 12), "")


def raw_files(snapshot: Path, accession: str) -> list[str]:
    p = snapshot / "files" / f"{accession}.json"
    if not p.is_file():
        return []
    try:
        rows = repository_files(json.loads(p.read_text(errors="replace")))
    except Exception:
        return []
    return sorted(r.name for r in rows if r.category.upper() == "RAW" or RAW_EXT_RE.search(r.name))


def channel_token(number: str, suffix: str | None) -> str:
    return f"{number}{(suffix or '').upper()}"


def channels_in(text: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for m in CHANNEL_RE.finditer(text):
        ch = channel_token(m.group(1), m.group(2))
        if ch not in seen:
            seen.add(ch); out.append(ch)
    return out


def universe_for(text: str) -> tuple[str, list[str]]:
    if re.search(r"(?i)TMTpro\s*18", text): return "TMTpro18", TMTPRO18.copy()
    if re.search(r"(?i)(?:16\s*plex\s+of\s+TMTpro|TMTpro\s*16)", text): return "TMTpro16", TMTPRO16.copy()
    if re.search(r"(?i)TMT10\s*plex", text): return "TMT10plex", TMT10.copy()
    if re.search(r"(?i)TMT6\s*plex", text): return "TMT6plex", TMT6.copy()
    if re.search(r"(?i)TMTpro", text): return "TMTpro", []
    if re.search(r"(?i)\bTMT\b|tandem\s+mass\s+tag", text): return "TMT", []
    if re.search(r"(?i)\biTRAQ\b", text): return "iTRAQ", []
    return "", []


def _role_channels(block: str, role: str) -> list[str]:
    pats: list[re.Pattern[str]] = []
    if role == "analytical":
        pats = [
            # Role-first grammar: "single cells were labeled with 126, 127, ...".
            re.compile(r"(?is)(?:single\s+(?:cells?|zygotes?|oocytes?|neurons?)|single[- ]cell(?:\s+samples?)?).{0,140}?(?:labeled|labelled|tagged)\s+(?:with\s+)?(?:eight\s+channels?\s+from\s+16\s*plex\s+of\s+TMTpro\s*[:：]?\s*)?([^.;]{1,240})"),
            re.compile(r"(?is)(?:analytical|analyte)(?:\s+(?:sample|channel|channels))?.{0,100}?(?:TMT(?:pro)?\s*)?((?:12[6-9]|13[0-5])(?:[NC])?(?:\s*(?:,|and|/|\+)\s*(?:12[6-9]|13[0-5])(?:[NC])?){0,20})"),
            # Channel-first grammar used by real papers: "channels 126 ... 129 contained single zygotes".
            re.compile(r"(?is)channels?\s+([^.;]{1,180}?)\s+(?:contained|represented|corresponded\s+to|were\s+(?:assigned|used)\s+(?:for|as))\s+(?:the\s+)?single\s+(?:cells?|zygotes?|oocytes?|neurons?)"),
        ]
    elif role == "carrier":
        pats = [
            re.compile(r"(?is)carrier(?:\s+(?:sample|channel|proteome|cells?))?.{0,180}?(?:labeled|labelled|tagged|using|with)\s+(?:the\s+)?(?:TMT(?:pro)?\s*[-_]?\s*)?((?:12[6-9]|13[0-5])(?:[NC])?)"),
            re.compile(r"(?is)(?:TMT(?:pro)?\s*[-_]?\s*)?(?:channel\s+)?((?:12[6-9]|13[0-5])(?:[NC])?)\s+(?:channel\s+)?(?:was|is)\s+(?:used\s+as\s+|the\s+)?(?:carrier)(?:\s+(?:channel|sample))?"),
        ]
    elif role == "blank":
        pats = [
            re.compile(r"(?is)blank(?:\s+(?:sample|channel))?(?:(?!(?:carrier|reference|bridge|analytical|analyte|single\s+(?:cell|zygote|oocyte|neuron))).){0,120}?(?:labeled|labelled|tagged|using|with)\s+(?:the\s+)?(?:TMT(?:pro)?\s*[-_]?\s*)?((?:12[6-9]|13[0-5])(?:[NC])?)"),
            re.compile(r"(?is)(?:TMT(?:pro)?\s*[-_]?\s*)?((?:12[6-9]|13[0-5])(?:[NC])?)\s+channel\s+(?:was|is)\s+(?:left\s+)?(?:empty|unused|blank)"),
            re.compile(r"(?is)channel\s+(?:TMT(?:pro)?\s*[-_]?\s*)?((?:12[6-9]|13[0-5])(?:[NC])?)\s+(?:was|is)\s+(?:left\s+)?(?:empty|unused|blank)"),
            re.compile(r"(?is)blank(?:\s+(?:sample|channel))?(?:(?!(?:carrier|reference|bridge|analytical|analyte|single\s+(?:cell|zygote|oocyte|neuron))).){0,100}?(?:used|using|with|was\s+labeled|was\s+labelled)\s+(?:the\s+)?(?:TMT(?:pro)?\s*[-_]?\s*)?((?:12[6-9]|13[0-5])(?:[NC])?)"),
        ]
    elif role == "reference":
        pats = [
            re.compile(r"(?is)(?:reference|bridge)(?:\s+(?:sample|channel|proteome))?.{0,180}?(?:labeled|labelled|tagged|using|with)\s+(?:the\s+)?(?:TMT(?:pro)?\s*[-_]?\s*)?((?:12[6-9]|13[0-5])(?:[NC])?)"),
            re.compile(r"(?is)(?:TMT(?:pro)?\s*[-_]?\s*)?(?:channel\s+)?((?:12[6-9]|13[0-5])(?:[NC])?)\s+(?:channel\s+)?(?:was|is)\s+(?:used\s+as\s+|the\s+)?(?:reference|bridge)(?:\s+(?:channel|sample))?"),
        ]
    out: list[str] = []
    for pat in pats:
        for m in pat.finditer(block):
            value = m.group(1)
            found = channels_in(value)
            if not found and re.fullmatch(r"(?:12[6-9]|13[0-5])(?:[NC])?", norm(value), re.I):
                found = [norm(value).upper()]
            for ch in found:
                if ch not in out: out.append(ch)
    return out


def _expected_single_count(block: str) -> int | None:
    nums: list[int] = []
    for pat in (
        re.compile(r"(?i)\b(\d{1,4})\s+single\s+cells?\b"),
        re.compile(r"(?i)\b(\d{1,4})\s+single[- ]cell\s+(?:samples?|channels?|wells?)\b"),
        re.compile(r"(?i)\b(\d{1,4})\s+single\s+zygotes?\b"),
    ):
        nums.extend(int(m.group(1)) for m in pat.finditer(block))
    return min(nums) if nums else None


def contract_blocks(text: str) -> list[tuple[str, str]]:
    clean = re.sub(r"[\t\r]+", " ", text)
    marker = re.compile(r"(?i)(?:for\s+the\s+)?(?:TMT6\s*plex\s+set|TMT8\s*plex\s+set|TMT10\s*plex|TMTpro\s*18(?:\s+labeling)?|TMTpro\s*16(?:\s*plex)?)")
    ms = list(marker.finditer(clean))
    blocks: list[tuple[str, str]] = []
    marker_coverage: list[tuple[int, int]] = []
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else min(len(clean), m.start() + 2600)
        blocks.append((norm(m.group(0)), clean[m.start():end]))
        marker_coverage.append((m.start(), end))
    # Generic carrier-centered windows retain partial contracts (for example a carrier-only partial design) only when
    # the carrier clause is not already owned by an explicit chemistry/set block.
    for i, m in enumerate(re.finditer(r"(?i)\bcarrier(?:\s+(?:sample|channel|cells?|proteome))?\b", clean)):
        if any(lo <= m.start() < hi for lo, hi in marker_coverage):
            continue
        b = clean[max(0, m.start() - 800):min(len(clean), m.end() + 1200)]
        if SINGLE_RE.search(b) and ISOBARIC_RE.search(b):
            blocks.append((f"carrier_window_{i+1}", b))
    # De-duplicate nearly identical normalized blocks.
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for label, block in blocks:
        key = norm(block)[:1200]
        if key and key not in seen:
            seen.add(key); out.append((label, block))
    return out


def extract_design_contracts(accession: str, source_ref: str, text: str, source_kind: str = "publication") -> list[DesignContract]:
    contracts: list[DesignContract] = []
    for idx, (label, block) in enumerate(contract_blocks(text), start=1):
        chemistry, universe = universe_for(block)
        analytical = _role_channels(block, "analytical")
        carrier = _role_channels(block, "carrier")
        blank = _role_channels(block, "blank")
        reference = _role_channels(block, "reference")
        excluded: list[str] = []
        m = re.search(r"(?is)all\s+channels?\s+except(?:\s+for)?\s+([^.;]{1,160})", block)
        if m:
            excluded = channels_in(m.group(1))
            if universe and (SINGLE_RE.search(block) or (carrier and blank)):
                analytical = [ch for ch in universe if ch not in set(excluded)]
        expected = _expected_single_count(block)
        # Explicit channel list may itself define expected count.
        if analytical and expected is None:
            expected = len(analytical)
        role_sets = [set(analytical), set(carrier), set(blank), set(reference)]
        contradiction = any(role_sets[i] & role_sets[j] for i in range(len(role_sets)) for j in range(i + 1, len(role_sets)))
        complete = bool(analytical and (carrier or reference) and not contradiction)
        confidence = "high" if complete and (universe or len(analytical) >= 2) else "medium" if (analytical or carrier or blank or reference) else "low"
        if not (chemistry or analytical or carrier or blank or reference):
            continue
        contracts.append(DesignContract(
            accession=accession,
            contract_id=f"{accession}:{source_kind}:{idx}:{re.sub(r'[^A-Za-z0-9]+','_',label)[:50]}",
            source_kind=source_kind,
            source_ref=source_ref,
            chemistry=chemistry,
            universe=universe,
            analytical_channels=analytical,
            carrier_channels=carrier,
            blank_channels=blank,
            reference_channels=reference,
            excluded_channels=excluded,
            expected_analytical_count=expected,
            confidence=confidence,
            complete_global_layout=complete,
            evidence_text=norm(block)[:1400],
        ))
    # Keep distinct role signatures only.
    unique: list[DesignContract] = []
    seen_sig: set[tuple[Any, ...]] = set()
    for c in contracts:
        sig = (c.chemistry, tuple(c.analytical_channels), tuple(c.carrier_channels), tuple(c.blank_channels), tuple(c.reference_channels), c.expected_analytical_count)
        if sig not in seen_sig:
            seen_sig.add(sig); unique.append(c)
    return unique


def branch_contexts(text: str) -> tuple[int, int, int]:
    # Paragraph/section-sized neighborhoods are more faithful than sentence-local token proximity.
    chunks = [norm(x) for x in re.split(r"\n\s*\n|(?<=[.;])\s+(?=[A-Z])", text) if norm(x)]
    iso = lfq = mixed = 0
    for i, chunk in enumerate(chunks):
        neigh = " ".join(chunks[max(0, i-1):min(len(chunks), i+2)])
        if not SINGLE_RE.search(neigh):
            continue
        hi = bool(ISOBARIC_RE.search(neigh)); hl = bool(LABEL_FREE_RE.search(neigh)); hb = bool(BULK_RE.search(neigh))
        iso += int(hi); lfq += int(hl); mixed += int(hb)
    return iso, lfq, mixed


def classify_branch(accession: str, texts: list[str], contracts: list[DesignContract]) -> BranchContract:
    iso = lfq = mixed = 0
    for text in texts:
        a, b, c = branch_contexts(text); iso += a; lfq += b; mixed += c
    complete = sum(c.complete_global_layout for c in contracts)
    if complete > 0:
        modality, conf = "reporter_multiplexed_single_cell", "high"
        reason = "publication design contract explicitly assigns analytical and carrier/reference reporter roles"
    elif lfq >= 2 and iso == 0:
        modality, conf = "label_free_single_cell", "high"
        reason = "multiple single-cell-local contexts explicitly describe label-free/LFQ acquisition or quantification"
    elif lfq >= 2 and iso > 0 and not any(c.complete_global_layout for c in contracts):
        modality, conf = "label_free_single_cell_in_mixed_repository", "high"
        reason = "single-cell-local label-free evidence is strong while isobaric evidence lacks a closed single-cell reporter contract"
    elif iso > 0:
        modality, conf = "reporter_multiplexed_single_cell_partial", "medium"
        reason = "single-cell-local isobaric evidence exists but reporter design is incomplete"
    elif lfq > 0:
        modality, conf = "label_free_single_cell", "medium"
        reason = "single-cell-local label-free evidence exists"
    else:
        modality, conf = "branch_modality_unresolved", "low"
        reason = "publication text does not close the single-cell acquisition modality"
    return BranchContract(accession, modality, conf, iso, lfq, mixed, len(contracts), complete, reason)


def repo_rows(snapshot: Path, accession: str) -> list[RepoFile]:
    p = snapshot / "files" / f"{accession}.json"
    if not p.is_file(): return []
    try: return repository_files(json.loads(p.read_text(errors="replace")))
    except Exception: return []


def artifact_priority(row: RepoFile) -> tuple[int, str]:
    if row.category.upper() == "RAW" or RAW_EXT_RE.search(row.name): return 0, "raw"
    suffix = Path(row.name).suffix.lower()
    if suffix not in STRUCTURED_EXTS: return 0, "unsupported"
    if suffix == ".sky": return 5, "skyline_document"
    if suffix == ".pdstudy": return 5, "proteome_discoverer_study"
    if suffix == ".xlsx" and HIGH_STRUCTURED_RE.search(row.name): return 5, "structured_workbook"
    if suffix in {".pdresult", ".msf"}: return 4, "proteome_discoverer_sqlite"
    if suffix == ".xlsx": return 4, "workbook"
    if suffix in {".csv", ".tsv", ".txt"} and HIGH_STRUCTURED_RE.search(row.name): return 4, "structured_table"
    if suffix in {".csv", ".tsv", ".txt"} and RESULT_TABLE_RE.search(row.name): return 2, "result_table_schema_only"
    if suffix in {".json", ".xml"}: return 3, "structured_metadata"
    return 1, "other_structured"


def acquire(row: RepoFile, dest: Path, max_bytes: int, reuse_roots: list[Path]) -> tuple[Path | None, str]:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(row.name).name)
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / safe
    if target.is_file() and target.stat().st_size > 0: return target, "cached"
    for root in reuse_roots:
        candidates = [root / safe, root / row.name, root / row.name.replace("/", "_")]
        try:
            candidates.extend(root.glob(f"*/{safe}"))
        except OSError:
            pass
        for p in candidates:
            if p.is_file() and p.stat().st_size > 0:
                try:
                    target.write_bytes(p.read_bytes())
                    return target, f"reused:{p}"
                except OSError:
                    pass
    uri = public_http_uri(row.uri)
    if not uri.startswith(("http://", "https://")): return None, "missing_or_unsupported_uri"
    try:
        import requests
        with requests.get(uri, stream=True, timeout=(20, 120), headers={"User-Agent": "PRIDE-SCP-multiplex-evidence-graph/0.1"}) as r:
            r.raise_for_status()
            clen = r.headers.get("content-length")
            if clen and int(clen) > max_bytes: return None, f"too_large:{clen}"
            n = 0
            with target.open("wb") as fh:
                for chunk in r.iter_content(1024 * 256):
                    if not chunk: continue
                    n += len(chunk)
                    if n > max_bytes:
                        fh.close(); target.unlink(missing_ok=True); return None, f"too_large_streamed:{n}"
                    fh.write(chunk)
        return target, "downloaded"
    except Exception as exc:
        target.unlink(missing_ok=True)
        return None, f"download_error:{type(exc).__name__}:{exc}"


def _row_evidence(
    accession: str, file_name: str, location: str, cells: list[str], schema_fields: list[str] | None = None,
) -> StructuredEvidence | None:
    fields = [norm(x) for x in (schema_fields or [])]
    named = bool(fields and len(fields) == len(cells))
    if named:
        text = " | ".join(
            f"{fields[i]}={norm(value)}" for i, value in enumerate(cells)
            if fields[i] and norm(value)
        )
    else:
        text = " | ".join(norm(x) for x in cells if norm(x))
    if not text:
        return None
    value_text = " | ".join(norm(x) for x in cells if norm(x))
    raws = sorted(set(m.group(1) for m in RAW_TOKEN_RE.finditer(value_text)))
    channels = channels_in(text) if re.search(r"(?i)(?:TMT|reporter|channel|abundance|intensity|S/?N)", text) else []
    samples = []
    for i, cell in enumerate(cells):
        c = norm(cell)
        key = fields[i] if named else ""
        if (
            re.search(r"(?i)(?:sample|cell|zygote|neuron|replicate|well|carrier|blank)", c)
            or re.search(r"(?i)(?:sample|cell|well|replicate|source|assay)", key)
        ) and len(c) <= 180:
            samples.append(c)
    if not (raws or channels or samples):
        return None
    et = "row_mapping" if raws and (channels or samples) else "channel_schema" if channels else "sample_schema"
    return StructuredEvidence(
        accession, file_name, location, et, raws, channels, samples[:12], fields if named else [], "",
        text[:1200],
    )


def parse_tabular_structured(accession: str, path: Path, max_rows: int = 80) -> list[StructuredEvidence]:
    out: list[StructuredEvidence] = []
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        try:
            rows = parse_support_file(path)
        except Exception:
            return []
        by_sheet: dict[str, list[Any]] = defaultdict(list)
        for row in rows[:max_rows * 5]:
            by_sheet[row.sheet].append(row)
        for sheet, sheet_rows in by_sheet.items():
            headers: list[str] = []
            header_row_number = -1
            for row in sheet_rows[:12]:
                score = sum(bool(SCHEMA_SIGNAL_RE.search(norm(cell))) for cell in row.cells)
                if score >= 2:
                    headers = [norm(x) for x in row.cells]
                    header_row_number = row.row_number
                    break
            for row in sheet_rows:
                fields = headers if headers and row.row_number > header_row_number and len(headers) == len(row.cells) else []
                ev = _row_evidence(
                    accession, path.name, f"{sheet}:row{row.row_number}", row.cells, fields
                )
                if ev:
                    out.append(ev)
        # Quantitative summary from workbook headers + numeric columns is intentionally conservative;
        # detailed role validation is emitted only where a recognizable reporter header is found.
        return out[:max_rows]
    try:
        text = path.read_text(errors="replace")
    except Exception:
        return []
    sample = "\n".join(text.splitlines()[:20])
    delim = "\t" if suffix == ".tsv" else ","
    if suffix == ".txt":
        try: delim = csv.Sniffer().sniff(sample, delimiters="\t,;").delimiter
        except Exception: delim = "\t"
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    rows = []
    for i, row in enumerate(reader):
        rows.append(row)
        if i >= max_rows: break
    if not rows: return []
    header = [norm(x) for x in rows[0]]
    reporter_cols: list[tuple[int, str]] = []
    for i, h in enumerate(header):
        m = REPORTER_HEADER_RE.search(h)
        if m: reporter_cols.append((i, channel_token(m.group(1), m.group(2))))
    if reporter_cols:
        values: dict[str, list[float]] = defaultdict(list)
        for row in rows[1:]:
            for idx, ch in reporter_cols:
                if idx >= len(row): continue
                try:
                    v = float(row[idx].replace(",", ""))
                    if math.isfinite(v) and v >= 0: values[ch].append(v)
                except Exception: pass
        med = {ch: statistics.median(vs) for ch, vs in values.items() if vs}
        base = statistics.median([x for x in med.values() if x > 0]) if any(x > 0 for x in med.values()) else 0.0
        summary = ";".join(f"{ch}:median={med[ch]:.6g},ratio={med[ch]/base:.3g}" for ch in sorted(med) if base > 0)
        out.append(StructuredEvidence(accession, path.name, "header", "quantitative_reporter_columns", [], [ch for _, ch in reporter_cols], [], header, summary, " | ".join(header)[:1200]))
    for i, row in enumerate(rows[1:max_rows], start=2):
        fields = header if len(header) == len(row) else []
        ev = _row_evidence(accession, path.name, f"row{i}", row, fields)
        if ev:
            out.append(ev)
    return out[:max_rows]


def quote_sql_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def parse_sqlite_structured(accession: str, path: Path, max_tables: int = 30) -> list[StructuredEvidence]:
    try:
        if path.read_bytes()[:16] != b"SQLite format 3\x00": return []
    except OSError:
        return []
    out: list[StructuredEvidence] = []
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        scored = []
        for t in tables:
            try: cols = [r[1] for r in con.execute(f"PRAGMA table_info({quote_sql_ident(t)})")]
            except Exception: cols = []
            score = int(bool(SCHEMA_SIGNAL_RE.search(t))) * 3 + sum(bool(SCHEMA_SIGNAL_RE.search(c)) for c in cols)
            if score: scored.append((score, t, cols))
        for _, t, cols in sorted(scored, reverse=True)[:max_tables]:
            sig_cols = [c for c in cols if SCHEMA_SIGNAL_RE.search(c)]
            out.append(StructuredEvidence(accession, path.name, f"sqlite:{t}", "sqlite_schema", [], channels_in(" ".join(cols)), [], sig_cols, "", f"table={t}; columns={','.join(cols[:80])}"))
            # Sample a bounded subset of schema-relevant columns; this is structural, not semantic line scanning.
            selected = sig_cols[:10]
            if not selected: continue
            q = f"SELECT {','.join(quote_sql_ident(c) for c in selected)} FROM {quote_sql_ident(t)} LIMIT 12"
            try:
                for ridx, row in enumerate(con.execute(q), start=1):
                    cells = [norm(x) for x in row]
                    ev = _row_evidence(
                        accession, path.name, f"sqlite:{t}:row{ridx}", cells, selected
                    )
                    if ev:
                        ev.evidence_type = "sqlite_structured_row"
                        out.append(ev)
            except Exception:
                pass
        con.close()
    except Exception:
        return out
    return out


def parse_skyline(accession: str, path: Path) -> list[StructuredEvidence]:
    try: root = ET.parse(path).getroot()
    except Exception: return []
    out: list[StructuredEvidence] = []
    for elem in root.iter():
        tag = elem.tag.rsplit("}", 1)[-1].lower()
        attrs = {k.rsplit("}", 1)[-1]: norm(v) for k, v in elem.attrib.items() if norm(v)}
        if not attrs: continue
        joined = " | ".join(f"{k}={v}" for k, v in attrs.items())
        if any(x in tag for x in ("replicate", "sample", "file", "result")) or RAW_TOKEN_RE.search(joined):
            ev = _row_evidence(
                accession, path.name, f"xml:{tag}", list(attrs.values()), list(attrs)
            )
            if ev:
                ev.evidence_type = "skyline_structured"
                out.append(ev)
    return out[:200]


def parse_artifact(accession: str, path: Path) -> tuple[str, list[StructuredEvidence]]:
    suffix = path.suffix.lower()
    if suffix in {".pdresult", ".pdstudy", ".msf"}:
        return "sqlite", parse_sqlite_structured(accession, path)
    if suffix == ".sky":
        return "skyline_xml", parse_skyline(accession, path)
    if suffix in {".xlsx", ".csv", ".tsv", ".txt"}:
        return "structured_table", parse_tabular_structured(accession, path)
    if suffix in {".xml", ".json"}:
        try: rows = parse_support_file(path)
        except Exception: return "structured_metadata", []
        evidence = []
        for row in rows[:100]:
            ev = _row_evidence(accession, path.name, f"{row.sheet}:row{row.row_number}", row.cells)
            if ev: evidence.append(ev)
        return "structured_metadata", evidence
    return "unsupported", []


def extract_external_sources(accession: str, source_ref: str, text: str) -> list[ExternalSource]:
    out: list[ExternalSource] = []
    seen: set[str] = set()
    for m in URL_RE.finditer(text):
        url = m.group(0).rstrip(".,;:)]}")
        if url.lower().startswith("github.com/") or url.lower().startswith("www.github.com/"):
            url = "https://" + url.replace("www.", "", 1)
        elif url.lower().startswith("doi.org/"):
            url = "https://" + url
        typ = classify_external_source(url)
        if typ == "other":
            continue
        if url not in seen:
            seen.add(url)
            out.append(ExternalSource(accession, typ, url, source_ref, "high"))
    return out


def recover_review_publication_candidates(path: Path, wanted: set[str], timeout: float = 45.0) -> tuple[dict[str, list[tuple[str, str]]], list[ExternalSource], list[PublicationPromotion]]:
    """Promote a review-only PMC candidate only after re-verifying the exact accession in full text.

    This closes the review-only exact-accession recovery failure mode without weakening publication identity rules: title
    overlap merely authorizes a bounded fetch; exact PXD verification in the fetched article is still
    mandatory before the text can enter the evidence graph.
    """
    texts: dict[str, list[tuple[str, str]]] = defaultdict(list)
    sources: list[ExternalSource] = []
    promotions: list[PublicationPromotion] = []
    rows = read_tsv(path) if path and path.is_file() else []
    try:
        import requests
    except Exception:
        return texts, sources, promotions
    sess = requests.Session(); sess.headers.update({"User-Agent":"PRIDE-SCP-evidence-graph/0.1"})
    for row in rows:
        acc = (row.get("accession") or "").strip().upper()
        if acc not in wanted or (row.get("status") or "").strip().lower() != "review":
            continue
        try: overlap = float(row.get("title_overlap") or 0)
        except Exception: overlap = 0.0
        url = (row.get("fulltext_url") or "").strip()
        if overlap < 0.70 or not url.startswith(("http://","https://")):
            continue
        title = norm(row.get("candidate_title")); doi = norm(row.get("candidate_doi")); pmcid = norm(row.get("candidate_pmcid"))
        verified = False; text = ""
        try:
            r = sess.get(url, timeout=timeout); r.raise_for_status()
            if len(r.content) > 8 * 1024 * 1024: raise ValueError("fulltext_too_large")
            root = ET.fromstring(r.content)
            attr_values = []
            for e in root.iter():
                attr_values.extend(norm(v) for v in e.attrib.values() if norm(v))
            text = " ".join(norm(x) for x in root.itertext() if norm(x)) + " " + " ".join(attr_values)
            verified = bool(re.search(rf"\b{re.escape(acc)}\b", text, re.I))
        except Exception:
            verified = False
        status = "promoted_exact_accession" if verified else "review_not_promoted"
        promotions.append(PublicationPromotion(acc,title,doi,pmcid,overlap,verified,status,url))
        if not verified:
            continue
        ref = doi or title or pmcid or url
        texts[acc].append((ref, text))
        sources.extend(extract_external_sources(acc, ref, text))
    return texts, sources, promotions


def supplementary_sources(path: Path, wanted: set[str]) -> list[ExternalSource]:
    out: list[ExternalSource] = []
    for row in read_tsv(path):
        acc = (row.get("accession") or "").strip().upper()
        if acc not in wanted:
            continue
        link = (row.get("link") or "").strip()
        link_type = (row.get("link_type") or "").strip()
        pmcid = (row.get("publication_pmcid") or "").strip()
        typ = classify_external_source(link, link_type)
        source_ref = "publication_supplementary_link"
        if pmcid:
            source_ref += f";pmcid={pmcid}"
        if link_type:
            source_ref += f";link_type={link_type}"
        if typ != "other" and link:
            out.append(ExternalSource(acc, typ, link, source_ref, "high"))

        # PMC/Europe PMC supplementary bundles are authoritative article-associated deposited
        # material.  Add one generic bundle source when the JATS inventory identifies a media or
        # supplementary-material link.  Downstream deduplication collapses repeated rows for the
        # same accession/PMCID.
        bundle_candidate = bool(
            pmcid
            and (
                typ == "supplementary_media"
                or (link and not link.startswith(("http://", "https://")))
                or "supp" in link.lower()
            )
        )
        if bundle_candidate:
            bundle = europe_pmc_supplement_url(pmcid)
            if bundle:
                out.append(ExternalSource(acc, "europe_pmc_supplement", bundle, source_ref, "high"))
    return out


def github_repo_parts(url: str) -> tuple[str, str] | None:
    m = re.search(r"github\.com/([^/]+)/([^/#?]+)", url, re.I)
    if not m: return None
    return m.group(1), m.group(2).removesuffix(".git")


def fetch_github_high_value(source: ExternalSource, dest: Path, max_files: int, max_bytes: int) -> list[Path]:
    parts = github_repo_parts(source.url)
    if not parts: return []
    owner, repo = parts
    try:
        import requests
        sess = requests.Session(); sess.headers.update({"User-Agent":"PRIDE-SCP-evidence-graph/0.1", "Accept":"application/vnd.github+json"})
        meta = sess.get(f"https://api.github.com/repos/{owner}/{repo}", timeout=45); meta.raise_for_status()
        branch = meta.json().get("default_branch") or "main"
        tree = sess.get(f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1", timeout=60); tree.raise_for_status()
        entries = tree.json().get("tree") or []
        candidates = []
        name_re = re.compile(r"(?i)(?:cell|sample|input|design|metadata|annotation|characteristic|channel|tmt|raw|report|cellenone)")
        ext_ok = {".txt", ".csv", ".tsv", ".xlsx", ".json", ".yaml", ".yml", ".r", ".py"}
        for e in entries:
            if e.get("type") != "blob": continue
            p = str(e.get("path") or ""); size = int(e.get("size") or 0)
            if Path(p).suffix.lower() not in ext_ok or not name_re.search(p): continue
            if size and size > max_bytes: continue
            score = 3 if re.search(r"(?i)(?:characteristic|metadata|design|sample|input|cellenone)", p) else 1
            candidates.append((score, size, p))
        candidates.sort(key=lambda x: (-x[0], x[1], x[2]))
        out = []
        for _, _, p in candidates[:max_files]:
            target = dest / owner / repo / p
            target.parent.mkdir(parents=True, exist_ok=True)
            r = sess.get(f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{p}", timeout=60)
            if not r.ok or len(r.content) > max_bytes: continue
            target.write_bytes(r.content); out.append(target)
        return out
    except Exception:
        return []


def title_tokens(text: str) -> set[str]:
    stop = {"the","a","an","and","of","in","for","to","with","using","single","cell","proteomics","proteomic","mass","spectrometry","data","dataset"}
    return {x for x in re.findall(r"[a-z0-9]+", text.lower()) if len(x) >= 3 and x not in stop}


def title_similarity(a: str, b: str) -> float:
    aa, bb = title_tokens(a), title_tokens(b)
    if not aa or not bb: return 0.0
    return len(aa & bb) / min(len(aa), len(bb))


def relation_candidates(snapshot: Path, accessions: list[str]) -> list[RelationCandidate]:
    titles = {a: project_title(snapshot, a) for a in accessions}
    raws = {a: set(raw_files(snapshot, a)) for a in accessions}
    out: list[RelationCandidate] = []
    for i, a in enumerate(accessions):
        for b in accessions[i+1:]:
            sim = title_similarity(titles[a], titles[b])
            if sim < 0.65: continue
            inter = len(raws[a] & raws[b]); union = len(raws[a] | raws[b]); jac = inter / union if union else 0.0
            if jac >= 0.8:
                cls = "probable_duplicate_or_reannouncement"
            elif sim >= 0.9:
                cls = "same_study_or_related_deposition_review"
            else:
                cls = "related_project_review"
            out.append(RelationCandidate(a,b,sim,inter,len(raws[a]),len(raws[b]),jac,cls,"high project-title similarity; compare repository file inventories before SDRF generation"))
    return out


def evidence_graph_status(branch: BranchContract, contracts: list[DesignContract], structured: list[StructuredEvidence], external: list[ExternalSource]) -> tuple[str, str]:
    if branch.modality.startswith("label_free"):
        if branch.confidence == "high": return "branch_reclassification_ready", "single-cell branch is source-grounded label-free; remove from reporter-multiplex reconstruction lane and segment repository runs"
        return "branch_review", "label-free evidence exists but branch needs stronger run segmentation"
    complete = [c for c in contracts if c.complete_global_layout]
    run_links = [e for e in structured if e.raw_files and (e.channels or e.sample_tokens)]
    if complete and run_links:
        return "explicit_row_manifest_candidate", "global reporter contract and structured run/sample evidence are both present"
    if complete:
        return "global_design_closed_run_mapping_open", "reporter roles are source-grounded; structured run/channel mapping is still required"
    if structured:
        return "structured_artifact_evidence_partial", "structured analysis artifacts expose sample/channel/run schema but global reporter contract remains incomplete"
    if external:
        return "external_analysis_source_identified", "publication links an analysis/data source that should be mined structurally"
    return "source_graph_open", "neither reporter design nor run mapping is closed"


def self_test() -> None:
    zygote = """For the TMT6plex set, single zygotes were labeled with 126, 127, 128, and 129 channels. The carrier sample was labeled with the TMT 131 channel. Blank samples were labeled with the TMT 130 channel. For the TMT8plex set, single zygotes were labeled with eight channels from 16plex of TMTpro: 126, 127N, 128C, 129N, 130C, 131N, 132C, and 133N."""
    cs = extract_design_contracts("PXDTEST", "paper", zygote)
    six = next(c for c in cs if c.chemistry == "TMT6plex")
    assert six.analytical_channels == ["126","127","128","129"]
    assert six.blank_channels == ["130"] and six.carrier_channels == ["131"] and six.complete_global_layout
    # Equivalent channel-first prose occurs in real publications and must close the same set-level contract.
    zygote_channel_first = """TMT6plex was used. Channels 126, 127, 128 and 129 contained single zygotes, channel 130 was left blank, and channel 131 was the carrier channel."""
    zcf = extract_design_contracts("PXDTEST", "paper", zygote_channel_first)
    zc = next(c for c in zcf if c.chemistry == "TMT6plex")
    assert zc.analytical_channels == ["126", "127", "128", "129"]
    assert zc.blank_channels == ["130"] and zc.carrier_channels == ["131"] and zc.complete_global_layout
    eight = next(c for c in cs if c.chemistry == "TMTpro16")
    assert eight.analytical_channels == ["126","127N","128C","129N","130C","131N","132C","133N"]

    gastruloid = """Single cell proteomics. For TMTpro 18 labeling, 100mM TMT (all channels except for 126 and 127C) was deposited in each well. The carrier sample was labeled with TMT126. The 127C channel was left empty to avoid potential contamination from the 126 carrier channel."""
    gs = extract_design_contracts("PXDTEST", "paper", gastruloid)
    g = next(c for c in gs if c.chemistry == "TMTpro18" and c.complete_global_layout)
    assert g.carrier_channels == ["126"] and g.blank_channels == ["127C"]
    assert len(g.analytical_channels) == 16 and "126" not in g.analytical_channels and "127C" not in g.analytical_channels
    assert g.complete_global_layout

    reticle = """After labeling, 14 single cells as well as 200 carrier channel cells were combined. Carrier channel cells were labeled with the 126 label of the TMTPro reagents."""
    rs = extract_design_contracts("PXDTEST", "paper", reticle)
    r = next(c for c in rs if c.carrier_channels)
    assert r.carrier_channels == ["126"] and r.expected_analytical_count == 14 and not r.complete_global_layout

    lfq = """Single HeLa cell data analysis was performed with CHIMERYS. Label-free quantification was performed using apQuant. Single cell and 40 cell samples were benchmarked. Bulk and immunoprecipitation experiments were analyzed separately."""
    b = classify_branch("PXDTEST", [lfq, lfq], [])
    assert b.modality in {"label_free_single_cell", "label_free_single_cell_in_mixed_repository"} and b.confidence == "high"

    assert artifact_priority(RepoFile("TMT6plexSingleCell-4samples-PGreports.xlsx","OTHER","x"))[0] == 5
    assert artifact_priority(RepoFile("H3_SingleCell_PTM_SureQuant.sky","OTHER","x"))[0] == 5
    assert artifact_priority(RepoFile("run.pdResult","ANALYSIS","x"))[0] == 4

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        csvp = td / "report.csv"
        csvp.write_text("RAW file,Sample,Abundance 126,Abundance 127N\nA.raw,cell1,1000,10\nB.raw,cell2,1200,12\n")
        cev = parse_tabular_structured("PXDTEST", csvp)
        assert any(e.evidence_type == "quantitative_reporter_columns" and e.channels == ["126","127N"] for e in cev)
        assert any("A.raw" in e.raw_files for e in cev)

        dbp = td / "run.pdResult"
        con = sqlite3.connect(dbp)
        con.execute("CREATE TABLE QuanChannels (SampleName TEXT, ChannelName TEXT, RawFile TEXT)")
        con.execute("INSERT INTO QuanChannels VALUES ('cell1','TMT126','A.raw')")
        con.commit(); con.close()
        sev = parse_sqlite_structured("PXDTEST", dbp)
        assert any(e.evidence_type == "sqlite_schema" and "ChannelName" in e.schema_fields for e in sev)
        assert any("A.raw" in e.raw_files for e in sev)
        assert any(
            "SampleName=cell1" in e.text and "ChannelName=TMT126" in e.text and "RawFile=A.raw" in e.text
            for e in sev if e.evidence_type == "sqlite_structured_row"
        )

        skyp = td / "test.sky"
        skyp.write_text("<srm_settings><measured_results><replicate name='cell1'><sample_file file_path='A.raw'/></replicate></measured_results></srm_settings>")
        sky = parse_skyline("PXDTEST", skyp)
        assert any("A.raw" in e.raw_files for e in sky)

    print("sdrf_multiplex_evidence_graph self-test: PASS")


def run(args: argparse.Namespace) -> int:
    accessions = read_accessions(args.accessions_file)
    wanted = set(accessions)
    out = args.output; out.mkdir(parents=True, exist_ok=True)
    pub = manifest_texts(args.publication_manifest, wanted)
    promoted_texts, promoted_sources, promotions = recover_review_publication_candidates(args.publication_candidates, wanted) if args.publication_candidates else ({}, [], [])
    for acc, rows in promoted_texts.items():
        pub[acc].extend(rows)

    all_contracts: list[DesignContract] = []
    branches: list[BranchContract] = []
    external: list[ExternalSource] = supplementary_sources(args.supplementary_links, wanted) if args.supplementary_links and args.supplementary_links.is_file() else []
    external.extend(promoted_sources)

    for acc in accessions:
        contracts = []
        texts = []
        for ref, text in pub.get(acc, []):
            texts.append(text)
            contracts.extend(extract_design_contracts(acc, ref, text))
            external.extend(extract_external_sources(acc, ref, text))
        all_contracts.extend(contracts)
        branches.append(classify_branch(acc, texts, contracts))

    # Unique external sources.
    ext_unique: list[ExternalSource] = []
    seen_ext = set()
    for e in external:
        key = (e.accession, e.source_type, e.url)
        if e.url and key not in seen_ext:
            seen_ext.add(key); ext_unique.append(e)
    external = ext_unique

    statuses: list[ArtifactStatus] = []
    structured: list[StructuredEvidence] = []
    reuse_roots = [p for p in args.reuse_root if p.is_dir()]
    for acc in accessions:
        candidates = []
        for row in repo_rows(args.snapshot, acc):
            pri, reason = artifact_priority(row)
            if pri > 0: candidates.append((pri, row, reason))
        candidates.sort(key=lambda x: (-x[0], x[1].name.lower()))
        selected = candidates[:args.max_artifacts_per_accession]
        for pri, row, reason in candidates:
            if (pri, row, reason) not in selected:
                statuses.append(ArtifactStatus(acc,row.name,row.category,row.uri,Path(row.name).suffix.lower(),pri,reason,False,"not_selected_budget","","",0))
        for pri, row, reason in selected:
            path, acq = acquire(row, out / "structured_artifacts" / acc, args.max_artifact_bytes, reuse_roots)
            if path is None:
                statuses.append(ArtifactStatus(acc,row.name,row.category,row.uri,Path(row.name).suffix.lower(),pri,reason,False,acq,"","",0)); continue
            parser, ev = parse_artifact(acc, path)
            structured.extend(ev)
            statuses.append(ArtifactStatus(acc,row.name,row.category,row.uri,Path(row.name).suffix.lower(),pri,reason,True,acq,str(path),parser,len(ev)))

    # External GitHub repositories are source-grounded because their URLs come from publication text.
    if args.fetch_external_analysis:
        for src in [e for e in external if e.source_type == "github"]:
            files = fetch_github_high_value(src, out / "external_analysis", args.max_external_files, args.max_external_bytes)
            for p in files:
                parser, ev = parse_artifact(src.accession, p)
                if not ev and p.suffix.lower() in {".r", ".py"}:
                    try:
                        txt = p.read_text(errors="replace")
                        raws = sorted(set(m.group(1) for m in RAW_TOKEN_RE.finditer(txt)))
                        chans = channels_in(txt) if re.search(r"(?i)(?:TMT|reporter|channel)", txt) else []
                        if raws or chans:
                            ev = [StructuredEvidence(src.accession,p.name,f"github:{p}","analysis_code_structured_reference",raws,chans,[],[],"",norm(txt)[:1200])]
                    except Exception: pass
                structured.extend(ev)

    # Design contracts can also emerge from downloaded structured row text, but only when the row
    # itself contains explicit reporter-role prose.  This is separate from result-table semantics.
    for ev in list(structured):
        if ev.evidence_type in {"row_mapping", "sample_schema", "channel_schema"} and re.search(r"(?i)(?:single|carrier|blank|reference).{0,120}(?:TMT|channel)|(?:TMT|channel).{0,120}(?:single|carrier|blank|reference)", ev.text):
            all_contracts.extend(extract_design_contracts(ev.accession, f"{ev.file_name}:{ev.source_location}", ev.text, source_kind="structured_artifact"))

    rels = relation_candidates(args.snapshot, accessions)

    branch_by = {b.accession: b for b in branches}
    contracts_by: dict[str,list[DesignContract]] = defaultdict(list)
    structured_by: dict[str,list[StructuredEvidence]] = defaultdict(list)
    ext_by: dict[str,list[ExternalSource]] = defaultdict(list)
    for c in all_contracts: contracts_by[c.accession].append(c)
    for e in structured: structured_by[e.accession].append(e)
    for e in external: ext_by[e.accession].append(e)

    graph_rows = []
    for acc in accessions:
        b = branch_by[acc]
        cs = contracts_by[acc]
        ss = structured_by[acc]
        es = ext_by[acc]
        status, blocker = evidence_graph_status(b, cs, ss, es)
        graph_rows.append({
            "accession":acc,"project_title":project_title(args.snapshot,acc),"raw_files":len(raw_files(args.snapshot,acc)),
            "branch_modality":b.modality,"branch_confidence":b.confidence,"design_contracts":len(cs),
            "complete_design_contracts":sum(c.complete_global_layout for c in cs),"structured_evidence_rows":len(ss),
            "structured_run_mapping_rows":sum(bool(e.raw_files and (e.channels or e.sample_tokens)) for e in ss),
            "external_analysis_sources":len(es),"graph_status":status,"next_blocker":blocker,
        })

    write_tsv(out/"branch_contracts.tsv", (asdict(x) for x in branches), list(BranchContract.__dataclass_fields__))
    write_tsv(out/"design_contracts.tsv", (asdict(x) for x in all_contracts), list(DesignContract.__dataclass_fields__))
    write_tsv(out/"artifact_inventory.tsv", (asdict(x) for x in statuses), list(ArtifactStatus.__dataclass_fields__))
    write_tsv(out/"structured_artifact_evidence.tsv", (asdict(x) for x in structured), list(StructuredEvidence.__dataclass_fields__))
    write_tsv(out/"external_analysis_sources.tsv", (asdict(x) for x in external), list(ExternalSource.__dataclass_fields__))
    write_tsv(out/"relation_candidates.tsv", (asdict(x) for x in rels), list(RelationCandidate.__dataclass_fields__))
    write_tsv(out/"review_publication_promotions.tsv", (asdict(x) for x in promotions), list(PublicationPromotion.__dataclass_fields__))
    graph_fields = ["accession","project_title","raw_files","branch_modality","branch_confidence","design_contracts","complete_design_contracts","structured_evidence_rows","structured_run_mapping_rows","external_analysis_sources","graph_status","next_blocker"]
    write_tsv(out/"multiplex_evidence_graph.tsv", graph_rows, graph_fields)

    counts = Counter(r["graph_status"] for r in graph_rows)
    mod = Counter(r["branch_modality"] for r in graph_rows)
    summary = {
        "auditor_version":VERSION,
        "accessions":len(accessions),
        "branch_modality_counts":dict(sorted(mod.items())),
        "graph_status_counts":dict(sorted(counts.items())),
        "complete_global_design_accessions":sorted({c.accession for c in all_contracts if c.complete_global_layout}),
        "structured_run_mapping_accessions":sorted({e.accession for e in structured if e.raw_files and (e.channels or e.sample_tokens)}),
        "external_analysis_source_accessions":sorted({e.accession for e in external}),
        "branch_reclassification_accessions":sorted(r["accession"] for r in graph_rows if r["graph_status"]=="branch_reclassification_ready"),
        "explicit_row_manifest_candidates":sorted(r["accession"] for r in graph_rows if r["graph_status"]=="explicit_row_manifest_candidate"),
        "relation_review_pairs":len(rels),
        "review_publication_promotions":sorted(p.accession for p in promotions if p.exact_accession_verified),
        "non_generative":True,
        "gt_metadata_used":False,
        "outputs":{
            "graph":str(out/"multiplex_evidence_graph.tsv"),"branches":str(out/"branch_contracts.tsv"),"contracts":str(out/"design_contracts.tsv"),
            "artifacts":str(out/"artifact_inventory.tsv"),"structured":str(out/"structured_artifact_evidence.tsv"),"external":str(out/"external_analysis_sources.tsv"),"relations":str(out/"relation_candidates.tsv"),"publication_promotions":str(out/"review_publication_promotions.tsv"),
        },
    }
    (out/"multiplex_evidence_graph_summary.json").write_text(json.dumps(summary,indent=2)+"\n")
    print(json.dumps(summary,indent=2))
    for r in graph_rows:
        print(f"{r['accession']} branch={r['branch_modality']}/{r['branch_confidence']} contracts={r['complete_design_contracts']}/{r['design_contracts']} structured={r['structured_evidence_rows']} runmap={r['structured_run_mapping_rows']} external={r['external_analysis_sources']} status={r['graph_status']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--accessions-file", type=Path)
    p.add_argument("--snapshot", type=Path)
    p.add_argument("--publication-manifest", type=Path)
    p.add_argument("--publication-candidates", type=Path)
    p.add_argument("--supplementary-links", type=Path)
    p.add_argument("--output", type=Path)
    p.add_argument("--reuse-root", type=Path, action="append", default=[])
    p.add_argument("--max-artifacts-per-accession", type=int, default=12)
    p.add_argument("--max-artifact-bytes", type=int, default=100*1024*1024)
    p.add_argument("--fetch-external-analysis", action="store_true")
    p.add_argument("--max-external-files", type=int, default=12)
    p.add_argument("--max-external-bytes", type=int, default=25*1024*1024)
    p.add_argument("--self-test", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()
    if args.self_test:
        self_test(); return 0
    for k in ("accessions_file","snapshot","publication_manifest","output"):
        if getattr(args,k) is None: raise SystemExit(f"ERROR: --{k.replace('_','-')} is required")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
