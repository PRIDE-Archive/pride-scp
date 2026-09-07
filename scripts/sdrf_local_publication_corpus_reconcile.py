#!/usr/bin/env python3
"""Reconcile the local PRIDE-SCP publication corpus before any external recovery.

This is a bounded, non-generative source-reconciliation utility.  It makes the existing local
manuscript corpus first-class evidence for SDRF work instead of relying on the assumption that older
PDF/text caches were perfectly propagated through every later manifest.

The reconciliation is intentionally local-only:

* explicit ``manual_pdfs/manual_pdf_manifest.tsv`` mappings are indexed first;
* accession-named manual PDFs (``manual_pdfs/PXDxxxxxx.pdf``) are indexed next;
* existing validated Stage-02/legacy PDF caches are reused through publication identity;
* existing normalized publication-content text is reused through publication identity;
* trustworthy rows from historical/current manifests supply publication metadata and linkage;
* quarantine manifests are applied before a local source can be selected;
* no PRIDE/Europe-PMC/Crossref/network request is made by this script.

For every accession the utility emits one selected local-first source decision plus an all-candidate
audit.  Selected PDF-only rows are text-extracted locally into the output directory so downstream
SDRF evidence code can consume them without another network-capable Stage-03 pass.

GT/reference metadata is never read and never supplies publication or SDRF values.  The accession
cohort is supplied explicitly by the caller from an already accepted source-grounded checkpoint.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
STAGES = ROOT / "python" / "stages"
if str(STAGES) not in sys.path:
    sys.path.insert(0, str(STAGES))

from pride_scp_pipeline_common import (  # noqa: E402
    extract_pdf_text,
    normalize_doi,
    publication_filename,
    text_value,
    validate_pdf_path,
)

AUDITOR_VERSION = "pride-scp-sdrf-local-publication-corpus-reconciler-v0.1"
PXD_RE = re.compile(r"\bPXD\d{6,}\b", re.I)

MANIFEST_FIELDS = [
    "accession",
    "pride_project_url",
    "pride_api_url",
    "pride_ftp_url",
    "dataset_title",
    "dataset_description",
    "submission_date",
    "publication_date",
    "organisms",
    "instruments",
    "software",
    "experiment_types",
    "quantification_methods",
    "project_doi",
    "publication_count",
    "publication_index",
    "publication_status",
    "publication_source",
    "publication_doi",
    "publication_pmid",
    "publication_pmcid",
    "publication_title",
    "publication_authors",
    "publication_journal",
    "publication_year",
    "publication_url",
    "publication_citation",
    "publication_is_open_access",
    "publication_has_pdf",
    "publication_pdf_candidate_url",
    "project_fetch_status",
    "project_fetch_error",
    "pdf_status",
    "pdf_path",
    "pdf_source",
    "pdf_url",
    "pdf_error",
    "publication_content_status",
    "publication_content_kind",
    "publication_content_path",
    "publication_content_source",
    "publication_content_error",
    "publication_content_chars",
    "publication_content_xml_path",
    "publication_content_html_path",
    "publication_content_text_path",
    "local_corpus_source",
    "local_corpus_identity_basis",
    "local_corpus_source_priority",
    "local_corpus_manifest_sources",
    "local_corpus_pdf_sha256",
    "local_corpus_text_sha256",
    "local_corpus_selection_status",
    "local_corpus_quarantine_reason",
]

CANDIDATE_FIELDS = [
    "accession",
    "publication_key",
    "publication_doi",
    "publication_pmid",
    "publication_pmcid",
    "publication_title",
    "dataset_title",
    "local_pdf_path",
    "local_text_path",
    "pdf_sha256",
    "text_sha256",
    "source_priority",
    "source_classes",
    "identity_basis",
    "manifest_sources",
    "has_local_pdf",
    "has_local_text",
    "quarantined",
    "quarantine_reason",
    "selected",
]

INVENTORY_FIELDS = [
    "accession",
    "dataset_title",
    "selected_status",
    "selected_source",
    "selected_identity_basis",
    "selected_publication_doi",
    "selected_publication_pmid",
    "selected_publication_title",
    "selected_pdf_path",
    "selected_text_path",
    "selected_pdf_sha256",
    "selected_text_sha256",
    "manual_manifest_candidates",
    "manual_accession_pdf_candidates",
    "manifest_local_pdf_candidates",
    "cached_pdf_candidates",
    "manifest_local_text_candidates",
    "cached_text_candidates",
    "metadata_only_candidates",
    "quarantined_candidates",
    "accepted_local_content_candidates",
    "external_recovery_needed",
]

SOURCE_PRIORITY = {
    "manual_manifest": 100,
    "manual_accession_pdf": 95,
    "manifest_local_pdf": 90,
    "cached_pdf": 85,
    "manifest_local_text": 80,
    "cached_text": 75,
    "manifest_metadata": 40,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--accessions-file", required=False, default="")
    parser.add_argument("--snapshot-dir", default="data/snapshot")
    parser.add_argument("--manual-pdf-dir", default="manual_pdfs")
    parser.add_argument("--manual-pdf-manifest", default="")
    parser.add_argument("--manifest", action="append", default=[])
    parser.add_argument("--quarantine-manifest", action="append", default=[])
    parser.add_argument("--pdf-dir", action="append", default=[])
    parser.add_argument("--content-text-dir", action="append", default=[])
    parser.add_argument("--output-dir", default="local_publication_corpus_reconciliation")
    parser.add_argument("--expected-accessions", type=int, default=105)
    parser.add_argument("--no-extract-selected-pdfs", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args()


def norm(value: Any) -> str:
    return re.sub(r"\s+", " ", text_value(value)).strip()


def normalize_pmid(value: Any) -> str:
    return re.sub(r"\D", "", norm(value))


def normalize_pmcid(value: Any) -> str:
    value = norm(value).upper()
    if value and not value.startswith("PMC") and value.isdigit():
        value = "PMC" + value
    return value


def normalize_title(value: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", norm(value).lower()))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def valid_text(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 500:
        return False
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").strip()) >= 500
    except OSError:
        return False


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
            w.writerow({field: row.get(field, "") for field in fields})


def dataset_title_from_snapshot(snapshot: Path, accession: str) -> str:
    path = snapshot / "projects" / f"{accession}.json"
    if not path.is_file():
        return ""
    try:
        obj = json.loads(path.read_text(errors="replace"))
    except Exception:
        return ""
    if isinstance(obj, dict):
        return norm(obj.get("title") or obj.get("projectTitle") or obj.get("name"))
    return ""


def publication_key(row: dict[str, Any]) -> str:
    doi = normalize_doi(row.get("publication_doi") or row.get("resolved_doi"))
    if doi:
        return "doi:" + doi
    pmid = normalize_pmid(row.get("publication_pmid") or row.get("resolved_pmid"))
    if pmid:
        return "pmid:" + pmid
    pmcid = normalize_pmcid(row.get("publication_pmcid") or row.get("resolved_pmcid"))
    if pmcid:
        return "pmcid:" + pmcid
    title = normalize_title(row.get("publication_title"))
    if title:
        return "title:" + hashlib.sha1(title.encode()).hexdigest()[:16]
    path = norm(row.get("pdf_path"))
    if path:
        return "pdfpath:" + hashlib.sha1(path.encode()).hexdigest()[:16]
    text_path = norm(row.get("publication_content_text_path"))
    if text_path:
        return "textpath:" + hashlib.sha1(text_path.encode()).hexdigest()[:16]
    return "row:" + hashlib.sha1(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()[:16]


def expected_pdf_name(row: dict[str, Any]) -> str:
    try:
        return publication_filename(row)
    except Exception:
        return ""


def expected_text_name(row: dict[str, Any]) -> str:
    pdf = expected_pdf_name(row)
    return (Path(pdf).stem + ".fulltext.txt") if pdf else ""


def build_file_index(directories: list[Path], suffix: str) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = defaultdict(list)
    seen: set[Path] = set()
    for directory in directories:
        if not directory.is_dir():
            continue
        try:
            paths = directory.rglob(f"*{suffix}")
        except OSError:
            continue
        for path in paths:
            try:
                if not path.is_file():
                    continue
                resolved = path.resolve()
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            index[path.name.lower()].append(resolved)
            index[path.stem.lower()].append(resolved)
            for pxd in PXD_RE.findall(path.name.upper()):
                index[pxd.lower()].append(resolved)
                index[(pxd + suffix).lower()].append(resolved)
    return index


def choose_index_path(index: dict[str, list[Path]], keys: Iterable[str], validator) -> Path | None:
    seen: set[Path] = set()
    for key in keys:
        if not key:
            continue
        for path in index.get(key.lower(), []):
            if path in seen:
                continue
            seen.add(path)
            try:
                if validator(path):
                    return path
            except OSError:
                continue
    return None


@dataclass
class Candidate:
    accession: str
    key: str
    row: dict[str, str] = field(default_factory=dict)
    source_classes: set[str] = field(default_factory=set)
    identity_basis: set[str] = field(default_factory=set)
    manifest_sources: set[str] = field(default_factory=set)
    pdf_paths: set[Path] = field(default_factory=set)
    text_paths: set[Path] = field(default_factory=set)
    priority: int = 0
    pdf_sha256: str = ""
    text_sha256: str = ""
    quarantine_reason: str = ""

    def add_source(self, source: str, identity: str = "") -> None:
        self.source_classes.add(source)
        self.priority = max(self.priority, SOURCE_PRIORITY.get(source, 0))
        if identity:
            self.identity_basis.add(identity)

    @property
    def local_pdf(self) -> Path | None:
        valid = []
        for path in self.pdf_paths:
            try:
                if validate_pdf_path(path):
                    valid.append(path)
            except OSError:
                pass
        return sorted(valid, key=lambda p: (len(str(p)), str(p)))[0] if valid else None

    @property
    def local_text(self) -> Path | None:
        valid = [path for path in self.text_paths if valid_text(path)]
        return sorted(valid, key=lambda p: (len(str(p)), str(p)))[0] if valid else None

    @property
    def has_local_content(self) -> bool:
        return self.local_pdf is not None or self.local_text is not None


def merge_rows(base: dict[str, str], extra: dict[str, Any]) -> dict[str, str]:
    out = dict(base)
    for key, value in extra.items():
        value = norm(value)
        if value and not norm(out.get(key)):
            out[key] = value
    return out


def candidate_lookup_key(accession: str, row: dict[str, Any], path_hash: str = "") -> tuple[str, str]:
    key = publication_key(row)
    if key.startswith(("doi:", "pmid:", "pmcid:", "title:")):
        return accession, key
    if path_hash:
        return accession, "content:" + path_hash
    return accession, key


def quarantine_id_sets(paths: list[Path]) -> tuple[set[tuple[str, str]], set[str], list[dict[str, str]]]:
    ids: set[tuple[str, str]] = set()
    content_hashes: set[str] = set()
    rows_out: list[dict[str, str]] = []
    hash_cache: dict[Path, str] = {}
    for path in paths:
        for row in read_tsv(path):
            acc = norm(row.get("accession")).upper()
            if not acc:
                continue
            reason = norm(row.get("publication_quarantine_reason") or row.get("quarantine_reason") or row.get("recovery_reason")) or "quarantined_upstream"
            d = normalize_doi(row.get("publication_doi") or row.get("candidate_doi"))
            p = normalize_pmid(row.get("publication_pmid") or row.get("candidate_pmid"))
            c = normalize_pmcid(row.get("publication_pmcid") or row.get("candidate_pmcid"))
            t = normalize_title(row.get("publication_title") or row.get("candidate_title"))
            if d:
                ids.add((acc, "doi:" + d))
            if p:
                ids.add((acc, "pmid:" + p))
            if c:
                ids.add((acc, "pmcid:" + c))
            if t:
                ids.add((acc, "title:" + hashlib.sha1(t.encode()).hexdigest()[:16]))
            for field in ("pdf_path", "publication_content_path", "publication_content_text_path"):
                raw = norm(row.get(field))
                if not raw:
                    continue
                candidate = Path(raw)
                if not candidate.is_file():
                    continue
                try:
                    resolved = candidate.resolve()
                    if resolved not in hash_cache:
                        hash_cache[resolved] = sha256_file(resolved)
                    content_hashes.add(hash_cache[resolved])
                except OSError:
                    pass
            rr = dict(row)
            rr["local_corpus_quarantine_reason"] = reason
            rr["local_corpus_quarantine_source"] = str(path)
            rows_out.append(rr)
    return ids, content_hashes, rows_out


def materialize_text(candidate: Candidate, out_dir: Path, *, extract_pdf: bool) -> tuple[Path | None, str]:
    text = candidate.local_text
    if text is not None:
        suffix = hashlib.sha1(candidate.key.encode()).hexdigest()[:10]
        dest = out_dir / "content" / "text" / f"{candidate.accession}__{suffix}.txt"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if text.resolve() != dest.resolve():
            shutil.copy2(text, dest)
        return dest.resolve(), "reused_local_text"
    pdf = candidate.local_pdf
    if pdf is None or not extract_pdf:
        return None, ""
    text_value_out, backend, error = extract_pdf_text(pdf)
    if len(text_value_out.strip()) < 500:
        return None, f"pdf_text_unavailable:{backend}:{error or 'too_short'}"
    suffix = hashlib.sha1(candidate.key.encode()).hexdigest()[:10]
    dest = out_dir / "content" / "text" / f"{candidate.accession}__{suffix}.txt"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text_value_out, encoding="utf-8")
    return dest.resolve(), "extracted_local_pdf:" + backend


def self_test() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        manual = root / "manual_pdfs"
        pdfs = root / "publication_pdfs"
        texts = root / "publication_content" / "text"
        manual.mkdir(); pdfs.mkdir(); texts.mkdir(parents=True)

        def fake_pdf(path: Path, payload: str) -> None:
            # validate_pdf_path checks the PDF signature/size only.  The reconciliation unit test
            # focuses on source selection; PDF extraction is exercised separately by production Stage03.
            body = ("%PDF-1.4\n" + payload + "\n") .encode() + b"x" * 900
            path.write_bytes(body)

        fake_pdf(manual / "PXD000001.pdf", "manual one")
        fake_pdf(manual / "PXD000003.pdf", "quarantine me")
        fake_pdf(pdfs / "10.1000_cached.pdf", "cached two")
        (texts / "10.1000_cached.fulltext.txt").write_text("cached publication text " * 60)

        manifest = root / "manifest.tsv"
        write_tsv(manifest, [
            {
                "accession": "PXD000001", "publication_status": "publication_found",
                "publication_doi": "10.1000/manual", "publication_title": "Manual paper",
            },
            {
                "accession": "PXD000002", "publication_status": "publication_found",
                "publication_doi": "10.1000/cached", "publication_title": "Cached paper",
            },
            {
                "accession": "PXD000003", "publication_status": "publication_found",
                "publication_doi": "10.1000/bad", "publication_title": "Wrong paper",
                "pdf_status": "already_exists", "pdf_path": str(manual / "PXD000003.pdf"),
            },
            {
                "accession": "PXD000004", "publication_status": "publication_found",
                "publication_doi": "10.1000/meta", "publication_title": "Metadata only",
            },
        ], ["accession","publication_status","publication_doi","publication_title","pdf_status","pdf_path"])
        quarantine = root / "quarantine.tsv"
        write_tsv(quarantine, [{
            "accession":"PXD000003", "publication_doi":"10.1000/bad",
            "pdf_path":str(manual / "PXD000003.pdf"), "publication_quarantine_reason":"wrong_project",
        }], ["accession","publication_doi","pdf_path","publication_quarantine_reason"])
        accessions = root / "accessions.txt"
        accessions.write_text("PXD000001\nPXD000002\nPXD000003\nPXD000004\nPXD000005\n")

        # Exercise the same reconciliation path as main without PDF text extraction.
        args = argparse.Namespace(
            accessions_file=str(accessions), snapshot_dir=str(root / "snapshot"),
            manual_pdf_dir=str(manual), manual_pdf_manifest="", manifest=[str(manifest)],
            quarantine_manifest=[str(quarantine)], pdf_dir=[str(pdfs)], content_text_dir=[str(texts)],
            output_dir=str(root / "out"), expected_accessions=5, no_extract_selected_pdfs=True,
        )
        summary = reconcile(args)
        assert summary["accessions"] == 5
        assert summary["accessions_with_selected_local_content"] == 2, summary
        assert summary["accessions_with_quarantined_candidates"] == 1, summary
        assert summary["accessions_metadata_only"] == 1, summary
        assert summary["unresolved_accessions"] == ["PXD000003", "PXD000005"], summary
        rows = read_tsv(root / "out" / "local_publication_source_inventory.tsv")
        by = {r["accession"]: r for r in rows}
        assert by["PXD000001"]["selected_source"] == "manual_accession_pdf"
        assert by["PXD000002"]["selected_source"] in {"cached_pdf", "cached_text"}
        assert by["PXD000003"]["selected_status"] == "quarantined_only"
        assert by["PXD000004"]["selected_status"] == "manifest_metadata_only"
        assert by["PXD000005"]["selected_status"] == "unresolved"
    print("sdrf_local_publication_corpus_reconcile self-test: PASS")


def reconcile(args: argparse.Namespace) -> dict[str, Any]:
    accessions_path = Path(args.accessions_file)
    wanted = []
    seen = set()
    for raw in accessions_path.read_text().splitlines():
        acc = raw.strip().upper()
        if not acc:
            continue
        if not PXD_RE.fullmatch(acc):
            raise SystemExit(f"invalid accession in cohort file: {acc}")
        if acc not in seen:
            seen.add(acc); wanted.append(acc)
    wanted = sorted(wanted)
    if args.expected_accessions and len(wanted) != args.expected_accessions:
        raise SystemExit(f"expected {args.expected_accessions} accessions, observed {len(wanted)}")
    wanted_set = set(wanted)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    snapshot = Path(args.snapshot_dir)
    manual_dir = Path(args.manual_pdf_dir)
    manual_manifest_path = Path(args.manual_pdf_manifest) if args.manual_pdf_manifest else manual_dir / "manual_pdf_manifest.tsv"
    manifest_paths = [Path(x) for x in args.manifest if Path(x).is_file()]
    quarantine_paths = [Path(x) for x in args.quarantine_manifest if Path(x).is_file()]
    pdf_dirs = [Path(x) for x in args.pdf_dir if Path(x).is_dir()]
    content_dirs = [Path(x) for x in args.content_text_dir if Path(x).is_dir()]

    pdf_index = build_file_index(pdf_dirs, ".pdf")
    text_index = build_file_index(content_dirs, ".txt")
    manual_index = build_file_index([manual_dir], ".pdf")
    quarantine_ids, quarantine_hashes, upstream_quarantine_rows = quarantine_id_sets(quarantine_paths)

    candidates: dict[tuple[str, str], Candidate] = {}
    hash_cache: dict[Path, str] = {}

    def file_hash(path: Path) -> str:
        resolved = path.resolve()
        if resolved not in hash_cache:
            hash_cache[resolved] = sha256_file(resolved)
        return hash_cache[resolved]

    def get_candidate(acc: str, row: dict[str, Any], *, path_hash: str = "") -> Candidate:
        lookup = candidate_lookup_key(acc, row, path_hash)
        cand = candidates.get(lookup)
        if cand is None:
            cand = Candidate(accession=acc, key=lookup[1], row={k:norm(v) for k,v in row.items()})
            candidates[lookup] = cand
        else:
            cand.row = merge_rows(cand.row, row)
        return cand

    # 1) Historical/current manifests establish publication identities and existing local assets.
    for manifest_path in manifest_paths:
        for row in read_tsv(manifest_path):
            acc = norm(row.get("accession")).upper()
            if acc not in wanted_set:
                continue
            cand = get_candidate(acc, row)
            cand.add_source("manifest_metadata", "manifest_publication_identity")
            cand.manifest_sources.add(str(manifest_path))

            raw_pdf = norm(row.get("pdf_path"))
            if raw_pdf:
                path = Path(raw_pdf)
                try:
                    if validate_pdf_path(path):
                        cand.pdf_paths.add(path.resolve())
                        source = norm(row.get("pdf_source")).lower()
                        identity = "manifest_pdf_path"
                        if "manual" in source:
                            cand.add_source("manual_manifest", identity)
                        else:
                            cand.add_source("manifest_local_pdf", identity)
                except OSError:
                    pass
            raw_text = norm(row.get("publication_content_text_path"))
            if raw_text:
                path = Path(raw_text)
                if valid_text(path):
                    cand.text_paths.add(path.resolve())
                    cand.add_source("manifest_local_text", "manifest_text_path")

            # Even if a manifest path went stale, reuse current managed caches by publication identity.
            pdf_name = expected_pdf_name(row)
            pdf = choose_index_path(pdf_index, [pdf_name, Path(pdf_name).stem if pdf_name else ""], validate_pdf_path)
            if pdf:
                cand.pdf_paths.add(pdf)
                cand.add_source("cached_pdf", "publication_identity_filename")
            text_name = expected_text_name(row)
            text = choose_index_path(text_index, [text_name, Path(text_name).stem if text_name else ""], valid_text)
            if text:
                cand.text_paths.add(text)
                cand.add_source("cached_text", "publication_identity_filename")

    # 2) Explicit manual manifest mappings are authoritative local mapping instructions, subject to quarantine.
    if manual_manifest_path.is_file():
        for row in read_tsv(manual_manifest_path):
            acc = norm(row.get("accession")).upper()
            if acc not in wanted_set:
                continue
            raw_pdf = norm(row.get("pdf_path"))
            if not raw_pdf:
                continue
            path = Path(raw_pdf)
            if not path.is_absolute():
                path = manual_manifest_path.parent / path
            try:
                if not validate_pdf_path(path):
                    continue
                path = path.resolve()
            except OSError:
                continue
            enriched = dict(row)
            enriched.setdefault("publication_status", "publication_found")
            enriched.setdefault("pdf_status", "already_exists")
            enriched["pdf_path"] = str(path)
            enriched.setdefault("pdf_source", "manual_manifest")
            enriched.setdefault("dataset_title", dataset_title_from_snapshot(snapshot, acc))
            h = file_hash(path)
            cand = get_candidate(acc, enriched, path_hash=h)
            cand.pdf_paths.add(path)
            cand.add_source("manual_manifest", "explicit_manual_pdf_manifest")
            cand.manifest_sources.add(str(manual_manifest_path))

    # 3) Accession-named manual PDFs are an explicit local mapping convention used by Stage02.
    for acc in wanted:
        pdf = choose_index_path(manual_index, [acc + ".pdf", acc], validate_pdf_path)
        if not pdf:
            continue
        h = file_hash(pdf)
        # Prefer an already known candidate with the same PDF hash, which preserves DOI/title metadata.
        same = None
        for cand in candidates.values():
            if cand.accession != acc:
                continue
            for p in cand.pdf_paths:
                try:
                    if file_hash(p) == h:
                        same = cand; break
                except OSError:
                    pass
            if same:
                break
        if same is None:
            pseudo = {
                "accession": acc,
                "dataset_title": dataset_title_from_snapshot(snapshot, acc),
                "publication_status": "publication_found",
                "publication_source": "manual_pdf_accession_filename",
                "pdf_status": "already_exists",
                "pdf_path": str(pdf),
                "pdf_source": "manual_pdf",
            }
            same = get_candidate(acc, pseudo, path_hash=h)
        same.pdf_paths.add(pdf)
        same.add_source("manual_accession_pdf", "accession_named_manual_pdf")

    # Compute hashes and apply upstream quarantine by publication identity or exact local content.
    quarantine_rows: list[dict[str, Any]] = []
    for cand in candidates.values():
        pdf = cand.local_pdf
        text = cand.local_text
        if pdf:
            try:
                cand.pdf_sha256 = file_hash(pdf)
            except OSError:
                pass
        if text:
            try:
                cand.text_sha256 = file_hash(text)
            except OSError:
                pass
        qreason = ""
        if (cand.accession, cand.key) in quarantine_ids:
            qreason = "upstream_quarantined_publication_identity"
        elif cand.pdf_sha256 and cand.pdf_sha256 in quarantine_hashes:
            qreason = "upstream_quarantined_pdf_content"
        elif cand.text_sha256 and cand.text_sha256 in quarantine_hashes:
            qreason = "upstream_quarantined_text_content"
        if qreason:
            cand.quarantine_reason = qreason
            quarantine_rows.append({
                **cand.row,
                "accession": cand.accession,
                "publication_key": cand.key,
                "local_corpus_quarantine_reason": qreason,
                "local_corpus_source": ",".join(sorted(cand.source_classes)),
                "local_corpus_pdf_sha256": cand.pdf_sha256,
                "local_corpus_text_sha256": cand.text_sha256,
            })

    selected: dict[str, Candidate | None] = {}
    selected_status: dict[str, str] = {}
    for acc in wanted:
        cc = [c for c in candidates.values() if c.accession == acc and not c.quarantine_reason]
        local = [c for c in cc if c.has_local_content]
        if local:
            local.sort(key=lambda c:(c.priority, bool(c.local_text), bool(c.local_pdf), bool(normalize_doi(c.row.get("publication_doi"))), len(norm(c.row.get("publication_title")))), reverse=True)
            selected[acc] = local[0]
            selected_status[acc] = "selected_local_content"
        elif cc:
            cc.sort(key=lambda c:(c.priority, bool(normalize_doi(c.row.get("publication_doi"))), len(norm(c.row.get("publication_title")))), reverse=True)
            selected[acc] = cc[0]
            selected_status[acc] = "manifest_metadata_only"
        else:
            selected[acc] = None
            has_quarantine = any(c.accession == acc and c.quarantine_reason for c in candidates.values())
            selected_status[acc] = "quarantined_only" if has_quarantine else "unresolved"

    manifest_all: list[dict[str, Any]] = []
    selected_manifest: list[dict[str, Any]] = []
    inventory_rows: list[dict[str, Any]] = []
    candidate_rows: list[dict[str, Any]] = []
    extraction_failures: list[dict[str, str]] = []

    for cand in sorted(candidates.values(), key=lambda c:(c.accession, -c.priority, c.key)):
        row = dict(cand.row)
        pdf = cand.local_pdf
        text = cand.local_text
        row.update({
            "accession": cand.accession,
            "publication_doi": normalize_doi(row.get("publication_doi")),
            "publication_pmid": normalize_pmid(row.get("publication_pmid")),
            "publication_pmcid": normalize_pmcid(row.get("publication_pmcid")),
            "pdf_status": "already_exists" if pdf else norm(row.get("pdf_status")),
            "pdf_path": str(pdf) if pdf else norm(row.get("pdf_path")),
            "publication_content_status": "available" if text else norm(row.get("publication_content_status")),
            "publication_content_text_path": str(text) if text else norm(row.get("publication_content_text_path")),
            "local_corpus_source": ",".join(sorted(cand.source_classes)),
            "local_corpus_identity_basis": ",".join(sorted(cand.identity_basis)),
            "local_corpus_source_priority": str(cand.priority),
            "local_corpus_manifest_sources": ";".join(sorted(cand.manifest_sources)),
            "local_corpus_pdf_sha256": cand.pdf_sha256,
            "local_corpus_text_sha256": cand.text_sha256,
            "local_corpus_selection_status": "selected" if selected.get(cand.accession) is cand else "candidate",
            "local_corpus_quarantine_reason": cand.quarantine_reason,
        })
        if not cand.quarantine_reason:
            manifest_all.append(row)
        candidate_rows.append({
            "accession": cand.accession,
            "publication_key": cand.key,
            "publication_doi": normalize_doi(cand.row.get("publication_doi")),
            "publication_pmid": normalize_pmid(cand.row.get("publication_pmid")),
            "publication_pmcid": normalize_pmcid(cand.row.get("publication_pmcid")),
            "publication_title": norm(cand.row.get("publication_title")),
            "dataset_title": norm(cand.row.get("dataset_title")),
            "local_pdf_path": str(pdf) if pdf else "",
            "local_text_path": str(text) if text else "",
            "pdf_sha256": cand.pdf_sha256,
            "text_sha256": cand.text_sha256,
            "source_priority": cand.priority,
            "source_classes": ",".join(sorted(cand.source_classes)),
            "identity_basis": ",".join(sorted(cand.identity_basis)),
            "manifest_sources": ";".join(sorted(cand.manifest_sources)),
            "has_local_pdf": str(pdf is not None).lower(),
            "has_local_text": str(text is not None).lower(),
            "quarantined": str(bool(cand.quarantine_reason)).lower(),
            "quarantine_reason": cand.quarantine_reason,
            "selected": str(selected.get(cand.accession) is cand).lower(),
        })

    for acc in wanted:
        cand = selected[acc]
        status = selected_status[acc]
        selected_row: dict[str, Any] = {}
        materialized_text = None
        materialized_source = ""
        if cand is not None:
            selected_row = dict(cand.row)
            if status == "selected_local_content":
                materialized_text, materialized_source = materialize_text(
                    cand, out_dir, extract_pdf=not args.no_extract_selected_pdfs
                )
                if materialized_text is None and cand.local_pdf is not None and not args.no_extract_selected_pdfs:
                    extraction_failures.append({"accession":acc,"pdf_path":str(cand.local_pdf),"error":materialized_source})
            pdf = cand.local_pdf
            text = materialized_text or cand.local_text
            selected_row.update({
                "accession": acc,
                "dataset_title": norm(selected_row.get("dataset_title")) or dataset_title_from_snapshot(snapshot, acc),
                "publication_status": norm(selected_row.get("publication_status")) or "publication_found",
                "publication_doi": normalize_doi(selected_row.get("publication_doi")),
                "publication_pmid": normalize_pmid(selected_row.get("publication_pmid")),
                "publication_pmcid": normalize_pmcid(selected_row.get("publication_pmcid")),
                "pdf_status": "already_exists" if pdf else norm(selected_row.get("pdf_status")),
                "pdf_path": str(pdf) if pdf else norm(selected_row.get("pdf_path")),
                "pdf_source": norm(selected_row.get("pdf_source")) or ("local_corpus" if pdf else ""),
                "publication_content_status": "available" if text else norm(selected_row.get("publication_content_status")),
                "publication_content_kind": ("pdf" if pdf and materialized_source.startswith("extracted_local_pdf") else norm(selected_row.get("publication_content_kind"))),
                "publication_content_path": str(pdf or text or norm(selected_row.get("publication_content_path"))),
                "publication_content_source": materialized_source or norm(selected_row.get("publication_content_source")),
                "publication_content_text_path": str(text) if text else norm(selected_row.get("publication_content_text_path")),
                "local_corpus_source": ",".join(sorted(cand.source_classes)),
                "local_corpus_identity_basis": ",".join(sorted(cand.identity_basis)),
                "local_corpus_source_priority": str(cand.priority),
                "local_corpus_manifest_sources": ";".join(sorted(cand.manifest_sources)),
                "local_corpus_pdf_sha256": cand.pdf_sha256,
                "local_corpus_text_sha256": sha256_file(text) if text and Path(text).is_file() else cand.text_sha256,
                "local_corpus_selection_status": status,
                "local_corpus_quarantine_reason": "",
            })
            selected_manifest.append(selected_row)

        cc = [c for c in candidates.values() if c.accession == acc]
        source_counts = Counter(s for c in cc for s in c.source_classes)
        accepted_content = sum(c.has_local_content and not c.quarantine_reason for c in cc)
        inventory_rows.append({
            "accession": acc,
            "dataset_title": norm(selected_row.get("dataset_title")) if selected_row else dataset_title_from_snapshot(snapshot, acc),
            "selected_status": status,
            "selected_source": (max(cand.source_classes, key=lambda s:SOURCE_PRIORITY.get(s,0)) if cand and cand.source_classes else ""),
            "selected_identity_basis": ",".join(sorted(cand.identity_basis)) if cand else "",
            "selected_publication_doi": normalize_doi(selected_row.get("publication_doi")) if selected_row else "",
            "selected_publication_pmid": normalize_pmid(selected_row.get("publication_pmid")) if selected_row else "",
            "selected_publication_title": norm(selected_row.get("publication_title")) if selected_row else "",
            "selected_pdf_path": norm(selected_row.get("pdf_path")) if selected_row else "",
            "selected_text_path": norm(selected_row.get("publication_content_text_path")) if selected_row else "",
            "selected_pdf_sha256": norm(selected_row.get("local_corpus_pdf_sha256")) if selected_row else "",
            "selected_text_sha256": norm(selected_row.get("local_corpus_text_sha256")) if selected_row else "",
            "manual_manifest_candidates": source_counts["manual_manifest"],
            "manual_accession_pdf_candidates": source_counts["manual_accession_pdf"],
            "manifest_local_pdf_candidates": source_counts["manifest_local_pdf"],
            "cached_pdf_candidates": source_counts["cached_pdf"],
            "manifest_local_text_candidates": source_counts["manifest_local_text"],
            "cached_text_candidates": source_counts["cached_text"],
            "metadata_only_candidates": sum((not c.has_local_content) and not c.quarantine_reason for c in cc),
            "quarantined_candidates": sum(bool(c.quarantine_reason) for c in cc),
            "accepted_local_content_candidates": accepted_content,
            "external_recovery_needed": "true" if status not in {"selected_local_content"} else "false",
        })

    all_fields = list(MANIFEST_FIELDS)
    for row in manifest_all + selected_manifest:
        for key in row:
            if key not in all_fields:
                all_fields.append(key)
    write_tsv(out_dir / "local_publication_candidate_rows.tsv", candidate_rows, CANDIDATE_FIELDS)
    write_tsv(out_dir / "local_publication_manifest_all.tsv", manifest_all, all_fields)
    write_tsv(out_dir / "local_publication_manifest_selected.tsv", selected_manifest, all_fields)
    write_tsv(out_dir / "local_publication_source_inventory.tsv", inventory_rows, INVENTORY_FIELDS)

    quarantine_fields = list(dict.fromkeys(MANIFEST_FIELDS + [
        "publication_key", "local_corpus_quarantine_source", "local_corpus_quarantine_reason",
    ] + [k for r in upstream_quarantine_rows + quarantine_rows for k in r]))
    write_tsv(out_dir / "local_publication_quarantine.tsv", upstream_quarantine_rows + quarantine_rows, quarantine_fields)
    write_tsv(out_dir / "selected_text_extraction_failures.tsv", extraction_failures, ["accession","pdf_path","error"])

    unresolved = [r["accession"] for r in inventory_rows if r["selected_status"] in {"unresolved","quarantined_only"}]
    metadata_only = [r["accession"] for r in inventory_rows if r["selected_status"] == "manifest_metadata_only"]
    external_needed = [r["accession"] for r in inventory_rows if r["external_recovery_needed"] == "true"]
    (out_dir / "unresolved_accessions.txt").write_text(("\n".join(unresolved) + "\n") if unresolved else "")
    (out_dir / "metadata_only_accessions.txt").write_text(("\n".join(metadata_only) + "\n") if metadata_only else "")
    (out_dir / "external_recovery_needed.txt").write_text(("\n".join(external_needed) + "\n") if external_needed else "")

    status_counts = Counter(r["selected_status"] for r in inventory_rows)
    source_counts = Counter(r["selected_source"] or "none" for r in inventory_rows)
    summary = {
        "auditor_version": AUDITOR_VERSION,
        "accessions": len(wanted),
        "network_used": False,
        "gt_metadata_used": False,
        "manual_pdf_dir": str(manual_dir),
        "manual_pdf_manifest": str(manual_manifest_path) if manual_manifest_path.is_file() else "",
        "manifest_sources": [str(p) for p in manifest_paths],
        "quarantine_sources": [str(p) for p in quarantine_paths],
        "pdf_cache_dirs": [str(p) for p in pdf_dirs],
        "content_text_dirs": [str(p) for p in content_dirs],
        "selected_status_counts": dict(sorted(status_counts.items())),
        "selected_source_counts": dict(sorted(source_counts.items())),
        "accessions_with_selected_local_content": status_counts["selected_local_content"],
        "accessions_metadata_only": status_counts["manifest_metadata_only"],
        "accessions_with_quarantined_candidates": sum(int(r["quarantined_candidates"]) > 0 for r in inventory_rows),
        "text_extraction_failures": len(extraction_failures),
        "unresolved_accessions": unresolved,
        "metadata_only_accessions": metadata_only,
        "external_recovery_needed": external_needed,
        "outputs": {
            "inventory": str(out_dir / "local_publication_source_inventory.tsv"),
            "candidates": str(out_dir / "local_publication_candidate_rows.tsv"),
            "manifest_all": str(out_dir / "local_publication_manifest_all.tsv"),
            "manifest_selected": str(out_dir / "local_publication_manifest_selected.tsv"),
            "quarantine": str(out_dir / "local_publication_quarantine.tsv"),
        },
    }
    (out_dir / "local_publication_corpus_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main() -> None:
    args = parse_args()
    if args.self_test:
        self_test()
        return
    if not args.accessions_file:
        raise SystemExit("--accessions-file is required unless --self-test is used")
    summary = reconcile(args)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
