#!/usr/bin/env python3
"""Stage 3: resolve generic publication content for semantic annotation.

v0.1.7 resolution order:
1. validated Stage-02 PDF;
2. Europe PMC full-text JATS XML for a known PMCID;
3. PMC article HTML full text as a secondary PMCID fallback;
4. unavailable (candidate remains repository-only).

XML/HTML is normalized deterministically to paragraph text so the existing
Stage-04 evidence retrieval and Qwen annotator can consume it without requiring
a PDF container.
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import re
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import requests

from pride_scp_pipeline_common import (
    build_session,
    extract_pdf_text,
    normalize_doi,
    pmc_idconv_lookup,
    publication_filename,
    publication_key,
    read_tsv,
    text_value,
    validate_pdf_path,
    write_tsv,
)

CONTENT_PATCH_VERSION = "pride-scp-v0.1.8"
_thread_local = threading.local()

CONTENT_FIELDS = [
    "publication_content_status",
    "publication_content_kind",
    "publication_content_path",
    "publication_content_source",
    "publication_content_error",
    "publication_content_chars",
    "publication_content_xml_path",
    "publication_content_html_path",
    "publication_content_text_path",
    "publication_content_trace",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_tsv", help="Stage-02 publication/PDF manifest")
    parser.add_argument("--output", default="pride_publications_with_content.tsv")
    parser.add_argument("--content-dir", default="publication_content")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--contact-email", default="")
    parser.add_argument("--user-agent", default="PRIDE-SCP-publication-content/1.7")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--accessions-file",
        default="",
        help="Optional text file restricting resolution to these PXD accessions.",
    )
    return parser.parse_args()


def session_for_thread(user_agent: str) -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = build_session(user_agent=user_agent)
        _thread_local.session = session
    return session


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def local_tag(elem: ET.Element) -> str:
    return elem.tag.rsplit("}", 1)[-1] if "}" in elem.tag else elem.tag


def element_text(elem: ET.Element | None) -> str:
    return "" if elem is None else normalize_space(" ".join(elem.itertext()))


def first_descendant(root: ET.Element, tag: str) -> ET.Element | None:
    for elem in root.iter():
        if local_tag(elem) == tag:
            return elem
    return None


def iter_descendants(root: ET.Element, tags: set[str]):
    for elem in root.iter():
        if local_tag(elem) in tags:
            yield elem


def jats_xml_to_text(xml_bytes: bytes) -> tuple[str, dict[str, Any]]:
    root = ET.fromstring(xml_bytes)
    paragraphs: list[str] = []

    title_text = element_text(first_descendant(root, "article-title"))
    if title_text:
        paragraphs.append(title_text)

    for abstract in [x for x in root.iter() if local_tag(x) == "abstract"]:
        abstract_parts = [
            element_text(x)
            for x in iter_descendants(abstract, {"title", "p"})
            if element_text(x)
        ]
        if not abstract_parts:
            value = element_text(abstract)
            abstract_parts = [value] if value else []
        if abstract_parts:
            paragraphs.append("Abstract")
            paragraphs.extend(abstract_parts)

    body = first_descendant(root, "body")
    if body is not None:
        for elem in iter_descendants(body, {"title", "p", "list-item"}):
            tag = local_tag(elem)
            if tag == "list-item" and any(local_tag(x) == "p" for x in elem):
                continue
            value = element_text(elem)
            if value:
                paragraphs.append(value)

    clean: list[str] = []
    for value in paragraphs:
        value = normalize_space(value)
        if value and (not clean or clean[-1] != value):
            clean.append(value)
    text = "\n\n".join(clean).strip() + ("\n" if clean else "")
    return text, {"title": title_text, "paragraphs": len(clean), "characters": len(text)}


def validate_xml_bytes(data: bytes) -> bool:
    if len(data) < 512:
        return False
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return False
    tags = {local_tag(elem) for elem in root.iter()}
    return "article" in tags and bool(tags & {"body", "abstract"})


def validate_text_path(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 500:
        return False
    try:
        return len(path.read_text(encoding="utf-8").strip()) >= 500
    except (OSError, UnicodeError):
        return False


class _ArticleHTMLTextParser(HTMLParser):
    BLOCK_TAGS = {"h1", "h2", "h3", "h4", "h5", "p", "li"}
    SKIP_TAGS = {"script", "style", "nav", "header", "footer", "form", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.active_tag = ""
        self.active_parts: list[str] = []
        self.blocks: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in self.BLOCK_TAGS and not self.active_tag:
            self.active_tag = tag
            self.active_parts = []

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if tag == self.active_tag:
            value = normalize_space(" ".join(self.active_parts))
            if value and (not self.blocks or self.blocks[-1] != value):
                self.blocks.append(value)
            self.active_tag = ""
            self.active_parts = []

    def handle_data(self, data):
        if not self.skip_depth and self.active_tag:
            value = normalize_space(data)
            if value:
                self.active_parts.append(value)


def pmc_html_to_text(html_bytes: bytes) -> tuple[str, dict[str, Any]]:
    parser = _ArticleHTMLTextParser()
    parser.feed(html_bytes.decode("utf-8", errors="replace"))
    blocks = [html_lib.unescape(x) for x in parser.blocks if len(x) >= 2]
    text = "\n\n".join(blocks).strip() + ("\n" if blocks else "")
    return text, {"paragraphs": len(blocks), "characters": len(text)}


def content_paths(row: dict[str, Any], content_dir: Path) -> tuple[Path, Path, Path]:
    base = Path(publication_filename(row)).stem
    return (
        content_dir / "xml" / f"{base}.fulltext.xml",
        content_dir / "html" / f"{base}.fulltext.html",
        content_dir / "text" / f"{base}.fulltext.txt",
    )


def resolve_pmcid(row, *, session, timeout, contact_email):
    trace: list[dict[str, Any]] = []
    pmcid = text_value(row.get("resolved_pmcid") or row.get("publication_pmcid"))
    if pmcid:
        if not pmcid.upper().startswith("PMC"):
            pmcid = "PMC" + pmcid
        trace.append({"source": "manifest", "status": "pmcid_present", "pmcid": pmcid})
        return pmcid, trace

    doi = normalize_doi(row.get("resolved_doi") or row.get("publication_doi"))
    pmid = text_value(row.get("resolved_pmid") or row.get("publication_pmid"))
    if not doi and not pmid:
        trace.append({"source": "NCBI_PMC_IDConverter", "status": "skipped_no_identifier"})
        return "", trace
    try:
        record = pmc_idconv_lookup(
            session, doi=doi, pmid=pmid, timeout=timeout,
            tool="pride_scp_publication_content", email=contact_email,
        )
    except Exception as exc:
        trace.append({"source": "NCBI_PMC_IDConverter", "status": "error", "error": f"{type(exc).__name__}: {exc}"})
        return "", trace
    if not record:
        trace.append({"source": "NCBI_PMC_IDConverter", "status": "no_match"})
        return "", trace
    pmcid = text_value(record.get("pmcid"))
    if pmcid and not pmcid.upper().startswith("PMC"):
        pmcid = "PMC" + pmcid
    trace.append({"source": "NCBI_PMC_IDConverter", "status": "matched" if pmcid else "matched_without_pmcid", "pmcid": pmcid})
    return pmcid, trace


def download_fulltext_xml(session, pmcid: str, *, timeout: float):
    url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
    try:
        response = session.get(url, timeout=timeout, allow_redirects=True, headers={"Accept": "application/xml,text/xml,*/*;q=0.5"})
        response.raise_for_status()
        data = response.content
        if not validate_xml_bytes(data):
            return None, url, f"invalid_fulltext_xml content_type={response.headers.get('Content-Type','')} bytes={len(data)}"
        return data, url, ""
    except Exception as exc:
        return None, url, f"{type(exc).__name__}: {exc}"


def download_pmc_html(session, pmcid: str, *, timeout: float):
    url = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"
    try:
        response = session.get(url, timeout=timeout, allow_redirects=True, headers={"Accept": "text/html,*/*;q=0.5"})
        response.raise_for_status()
        data = response.content
        text, _ = pmc_html_to_text(data)
        if len(text.strip()) < 1000:
            return None, url, f"html_fulltext_too_short content_type={response.headers.get('Content-Type','')} characters={len(text)}"
        return data, url, ""
    except Exception as exc:
        return None, url, f"{type(exc).__name__}: {exc}"


def empty_result(status: str, error: str, trace: list[dict[str, Any]] | None = None):
    return {
        "publication_content_status": status,
        "publication_content_kind": "",
        "publication_content_path": "",
        "publication_content_source": "",
        "publication_content_error": error,
        "publication_content_chars": "",
        "publication_content_xml_path": "",
        "publication_content_html_path": "",
        "publication_content_text_path": "",
        "publication_content_trace": json.dumps(trace or [], ensure_ascii=False),
    }


def pdf_content_result(row, *, content_dir: Path, force: bool):
    status = text_value(row.get("pdf_status")).lower()
    path_text = text_value(row.get("pdf_path"))
    if status not in {"downloaded", "already_exists"} or not path_text:
        return None
    path = Path(path_text)
    if not validate_pdf_path(path):
        return None

    _, _, text_path = content_paths(row, content_dir)
    trace = [{"source": "Stage02_PDF", "status": "validated", "path": str(path)}]
    text = ""
    backend = ""
    extraction_error = ""

    if not force and validate_text_path(text_path):
        try:
            text = text_path.read_text(encoding="utf-8")
            backend = "cached_pdf_text"
            trace.append({
                "source": "PDF_text_cache",
                "status": "reused",
                "path": str(text_path.resolve()),
                "characters": len(text),
            })
        except (OSError, UnicodeError) as exc:
            extraction_error = f"cached PDF text unreadable: {type(exc).__name__}: {exc}"
            text = ""

    if not text:
        text, backend, extraction_error = extract_pdf_text(path)
        if len(text.strip()) >= 500:
            text_path.parent.mkdir(parents=True, exist_ok=True)
            text_path.write_text(text, encoding="utf-8")
            trace.append({
                "source": "PDF_text_extraction",
                "status": "extracted",
                "backend": backend,
                "path": str(text_path.resolve()),
                "characters": len(text),
            })
        else:
            trace.append({
                "source": "PDF_text_extraction",
                "status": "unavailable",
                "backend": backend,
                "error": extraction_error or f"extracted text too short ({len(text.strip())} chars)",
            })
            text = ""

    return {
        "publication_content_status": "available",
        "publication_content_kind": "pdf",
        "publication_content_path": str(path.resolve()),
        "publication_content_source": "pdf:" + (text_value(row.get("pdf_source")) or "resolved"),
        "publication_content_error": "" if text else extraction_error,
        "publication_content_chars": len(text) if text else "",
        "publication_content_xml_path": "",
        "publication_content_html_path": "",
        "publication_content_text_path": str(text_path.resolve()) if text else "",
        "publication_content_trace": json.dumps(trace, ensure_ascii=False),
    }


def resolve_content(row, *, content_dir, user_agent, timeout, contact_email, force):
    pdf = pdf_content_result(row, content_dir=content_dir, force=force)
    if pdf is not None:
        return pdf
    if text_value(row.get("publication_status")) != "publication_found":
        return empty_result("not_applicable", "No linked publication metadata")

    xml_path, html_path, text_path = content_paths(row, content_dir)
    if not force and text_path.is_file() and validate_text_path(text_path):
        if xml_path.is_file():
            try:
                if validate_xml_bytes(xml_path.read_bytes()):
                    return {
                        **empty_result("available", ""),
                        "publication_content_kind": "fulltext_xml",
                        "publication_content_path": str(text_path.resolve()),
                        "publication_content_source": "cached_europe_pmc_fullTextXML",
                        "publication_content_chars": len(text_path.read_text(encoding="utf-8")),
                        "publication_content_xml_path": str(xml_path.resolve()),
                        "publication_content_text_path": str(text_path.resolve()),
                    }
            except OSError:
                pass
        if html_path.is_file():
            try:
                html_text, _ = pmc_html_to_text(html_path.read_bytes())
                if len(html_text.strip()) >= 1000:
                    return {
                        **empty_result("available", ""),
                        "publication_content_kind": "fulltext_html",
                        "publication_content_path": str(text_path.resolve()),
                        "publication_content_source": "cached_pmc_html",
                        "publication_content_chars": len(text_path.read_text(encoding="utf-8")),
                        "publication_content_html_path": str(html_path.resolve()),
                        "publication_content_text_path": str(text_path.resolve()),
                    }
            except OSError:
                pass

    session = session_for_thread(user_agent)
    pmcid, trace = resolve_pmcid(row, session=session, timeout=timeout, contact_email=contact_email)
    if not pmcid:
        return empty_result("unavailable", "No PMCID available for full-text lookup", trace)

    xml_bytes, xml_url, xml_error = download_fulltext_xml(session, pmcid, timeout=timeout)
    if xml_bytes is not None:
        try:
            text, meta = jats_xml_to_text(xml_bytes)
        except Exception as exc:
            trace.append({"source": "JATS_normalizer", "status": "error", "error": f"{type(exc).__name__}: {exc}"})
            text = ""
            meta = {}
        if len(text.strip()) >= 500:
            xml_path.parent.mkdir(parents=True, exist_ok=True)
            text_path.parent.mkdir(parents=True, exist_ok=True)
            xml_path.write_bytes(xml_bytes)
            text_path.write_text(text, encoding="utf-8")
            trace.append({"source": "EuropePMC_fullTextXML", "status": "downloaded", "url": xml_url, "pmcid": pmcid, **meta})
            return {
                **empty_result("available", "", trace),
                "publication_content_kind": "fulltext_xml",
                "publication_content_path": str(text_path.resolve()),
                "publication_content_source": "europe_pmc_fullTextXML",
                "publication_content_chars": len(text),
                "publication_content_xml_path": str(xml_path.resolve()),
                "publication_content_text_path": str(text_path.resolve()),
            }
        trace.append({"source": "EuropePMC_fullTextXML", "status": "normalization_too_short", "characters": len(text)})
    else:
        trace.append({"source": "EuropePMC_fullTextXML", "status": "error", "url": xml_url, "error": xml_error})

    html_bytes, html_url, html_error = download_pmc_html(session, pmcid, timeout=timeout)
    if html_bytes is not None:
        text, meta = pmc_html_to_text(html_bytes)
        html_path.parent.mkdir(parents=True, exist_ok=True)
        text_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_bytes(html_bytes)
        text_path.write_text(text, encoding="utf-8")
        trace.append({"source": "PMC_HTML", "status": "downloaded", "url": html_url, "pmcid": pmcid, **meta})
        return {
            **empty_result("available", "", trace),
            "publication_content_kind": "fulltext_html",
            "publication_content_path": str(text_path.resolve()),
            "publication_content_source": "pmc_html",
            "publication_content_chars": len(text),
            "publication_content_html_path": str(html_path.resolve()),
            "publication_content_text_path": str(text_path.resolve()),
        }

    trace.append({"source": "PMC_HTML", "status": "error", "url": html_url, "error": html_error})
    return empty_result(
        "unavailable",
        f"Europe PMC fullTextXML: {xml_error or 'unusable'} | PMC HTML: {html_error}",
        trace,
    )


def main() -> None:
    args = parse_args()
    rows = read_tsv(Path(args.input_tsv))
    if args.accessions_file:
        allowed = {
            line.strip().upper()
            for line in Path(args.accessions_file).read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        rows = [
            row for row in rows
            if text_value(row.get("accession")).upper() in allowed
        ]
    content_dir = Path(args.content_dir)
    output = Path(args.output)

    groups: dict[str, list[int]] = {}
    for i, row in enumerate(rows):
        key = publication_key(row) if text_value(row.get("publication_status")) == "publication_found" else f"row:{i}"
        groups.setdefault(key, []).append(i)
    representative = {key: dict(rows[indexes[0]]) for key, indexes in groups.items()}

    print(f"Content resolver: {CONTENT_PATCH_VERSION}")
    print(f"Manifest rows: {len(rows):,}")
    print(f"Unique publication/content groups: {len(groups):,}")
    print(f"Workers: {args.workers}")

    resolved: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(
                resolve_content, row,
                content_dir=content_dir, user_agent=args.user_agent,
                timeout=args.timeout, contact_email=args.contact_email,
                force=args.force,
            ): key
            for key, row in representative.items()
        }
        completed = 0
        for future in as_completed(futures):
            key = futures[future]
            try:
                resolved[key] = future.result()
            except Exception as exc:
                resolved[key] = empty_result("unavailable", f"{type(exc).__name__}: {exc}", [{"source": "worker", "status": "error"}])
            completed += 1
            if completed % 50 == 0 or completed == len(futures):
                print(f"Resolved content {completed}/{len(futures)}")

    output_rows = []
    for key, indexes in groups.items():
        for index in indexes:
            merged = dict(rows[index])
            merged.update(resolved[key])
            output_rows.append(merged)

    fieldnames = list(rows[0].keys()) if rows else []
    for field in CONTENT_FIELDS:
        if field not in fieldnames:
            fieldnames.append(field)
    write_tsv(output, output_rows, fieldnames)

    statuses: dict[str, int] = {}
    kinds: dict[str, int] = {}
    for row in output_rows:
        status = text_value(row.get("publication_content_status")) or "unknown"
        kind = text_value(row.get("publication_content_kind"))
        statuses[status] = statuses.get(status, 0) + 1
        if kind:
            kinds[kind] = kinds.get(kind, 0) + 1
    print(f"Saved: {output}")
    print(json.dumps({"status_rows": statuses, "kind_rows": kinds}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
