#!/usr/bin/env python3
"""Shared helpers for the PRIDE SCP catalogue pipeline."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
PXD_RE = re.compile(r"\bPXD\d{6,}\b", re.I)
PMID_RE = re.compile(r"\bPMID\s*:?\s*(\d{5,10})\b", re.I)

DEFAULT_UA = (
    "PRIDE-SCP-catalogue/1.0 "
    "(academic metadata curation; https://www.ebi.ac.uk/pride/)"
)


def build_session(
    user_agent: str = DEFAULT_UA,
    retries: int = 5,
    backoff: float = 0.8,
) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=32, pool_maxsize=32)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "User-Agent": user_agent,
            "Accept": "application/json",
        }
    )
    return session


def get_json(
    session: requests.Session,
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    timeout: float = 45.0,
    delay: float = 0.0,
    headers: Optional[dict[str, str]] = None,
) -> Any:
    if delay > 0:
        time.sleep(delay)
    response = session.get(
        url,
        params=params,
        timeout=timeout,
        headers=headers,
    )
    response.raise_for_status()
    return response.json()


def text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip()
    return str(value).strip()


def normalize_doi(value: Any) -> str:
    text = text_value(value)
    if not text:
        return ""
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.I)
    match = DOI_RE.search(text)
    if not match:
        return ""
    return match.group(0).rstrip(".,;)]}").lower()


def doi_from_text(value: Any) -> str:
    return normalize_doi(value)


def pmid_from_text(value: Any) -> str:
    text = text_value(value)
    match = PMID_RE.search(text)
    return match.group(1) if match else ""


def unique_nonempty(values: Iterable[Any]) -> list[str]:
    seen = set()
    out = []
    for value in values:
        value = text_value(value)
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def join_unique(values: Iterable[Any], sep: str = "; ") -> str:
    return sep.join(unique_nonempty(values))


def flatten_named_values(value: Any) -> list[str]:
    """Extract human-readable names from common PRIDE metadata shapes."""
    out: list[str] = []

    if value is None:
        return out

    if isinstance(value, str):
        if value.strip():
            out.append(value.strip())
        return out

    if isinstance(value, list):
        for item in value:
            out.extend(flatten_named_values(item))
        return unique_nonempty(out)

    if isinstance(value, dict):
        preferred = (
            "name",
            "value",
            "label",
            "title",
            "accession",
        )
        for key in preferred:
            if key in value and value[key] not in (None, "", [], {}):
                out.extend(flatten_named_values(value[key]))
                if out:
                    return unique_nonempty(out)

        for item in value.values():
            if isinstance(item, (dict, list)):
                out.extend(flatten_named_values(item))

    return unique_nonempty(out)


def first_value(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, "", [], {}):
            return mapping[key]
    return None


def recursive_find_values(obj: Any, key_names: set[str]) -> list[Any]:
    found: list[Any] = []
    lowered = {x.lower() for x in key_names}

    if isinstance(obj, dict):
        for key, value in obj.items():
            if key.lower() in lowered:
                found.append(value)
            if isinstance(value, (dict, list)):
                found.extend(recursive_find_values(value, key_names))

    elif isinstance(obj, list):
        for value in obj:
            found.extend(recursive_find_values(value, key_names))

    return found


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            delimiter="\t",
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _csv_value(row.get(k)) for k in fieldnames})


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _csv_value(row.get(k)) for k in fieldnames})


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def safe_slug(value: str, max_len: int = 120) -> str:
    value = value.strip()
    value = re.sub(r"^https?://", "", value, flags=re.I)
    value = re.sub(r"[^A-Za-z0-9._+-]+", "_", value)
    value = value.strip("._")
    if not value:
        value = "unknown"
    if len(value) > max_len:
        digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]
        value = f"{value[:max_len-13]}_{digest}"
    return value


def publication_key(row: dict[str, Any]) -> str:
    doi = normalize_doi(row.get("publication_doi"))
    if doi:
        return f"doi:{doi}"
    pmid = text_value(row.get("publication_pmid"))
    if pmid:
        return f"pmid:{pmid}"
    title = text_value(row.get("publication_title")).lower()
    if title:
        digest = hashlib.sha1(title.encode("utf-8")).hexdigest()[:16]
        return f"title:{digest}"
    accession = text_value(row.get("accession"))
    index = text_value(row.get("publication_index"))
    return f"row:{accession}:{index}"


def publication_filename(row: dict[str, Any]) -> str:
    doi = normalize_doi(row.get("publication_doi"))
    if doi:
        return safe_slug(doi) + ".pdf"
    pmid = text_value(row.get("publication_pmid"))
    if pmid:
        return f"PMID_{safe_slug(pmid)}.pdf"
    title = text_value(row.get("publication_title"))
    if title:
        digest = hashlib.sha1(title.encode("utf-8")).hexdigest()[:12]
        return f"title_{digest}.pdf"
    return safe_slug(publication_key(row)) + ".pdf"


def looks_like_pdf_bytes(data: bytes) -> bool:
    return data.lstrip().startswith(b"%PDF-")


def validate_pdf_path(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 512:
        return False
    with path.open("rb") as handle:
        head = handle.read(1024)
    return looks_like_pdf_bytes(head)


_PDF_LIGATURES = str.maketrans({
    "\ufb00": "ff",
    "\ufb01": "fi",
    "\ufb02": "fl",
    "\ufb03": "ffi",
    "\ufb04": "ffl",
})


def normalize_pdf_text(text: str) -> str:
    """Normalize PDF-extracted text while preserving paragraph/page structure."""
    text = (text or "").translate(_PDF_LIGATURES).replace("\x00", "")
    lines = []
    for line in text.splitlines():
        line = re.sub(r"[ \t]+", " ", line).rstrip()
        lines.append(line)
    return "\n".join(lines).strip()


def extract_pdf_text(path: Path) -> tuple[str, str, str]:
    """Extract PDF text with deterministic local fallbacks.

    Returns ``(text, backend, error)``.  This helper deliberately performs only
    local extraction; OCR remains a separate/manual concern.  It is shared by
    publication-content materialization so downstream Rust consumers can scan
    normalized text rather than implementing PDF parsing themselves.
    """
    errors: list[str] = []

    try:
        import fitz  # type: ignore

        pages = []
        with fitz.open(path) as doc:
            for page in doc:
                pages.append(normalize_pdf_text(page.get_text("text", sort=True) or ""))
        text = "\n\n".join(x for x in pages if x).strip()
        if text:
            return text + "\n", "pymupdf", ""
    except Exception as exc:
        errors.append(f"PyMuPDF: {type(exc).__name__}: {exc}")

    exe = shutil.which("pdftotext")
    if exe:
        try:
            proc = subprocess.run(
                [exe, "-enc", "UTF-8", "-layout", str(path), "-"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if proc.returncode == 0:
                raw = proc.stdout.decode("utf-8", "replace")
                pages = [normalize_pdf_text(x) for x in raw.split("\f")]
                text = "\n\n".join(x for x in pages if x).strip()
                if text:
                    return text + "\n", "pdftotext", ""
            else:
                errors.append(
                    f"pdftotext: exit={proc.returncode}: "
                    + proc.stderr.decode("utf-8", "replace")[:500]
                )
        except Exception as exc:
            errors.append(f"pdftotext: {type(exc).__name__}: {exc}")
    else:
        errors.append("pdftotext: command unavailable")

    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(str(path))
        pages = [normalize_pdf_text(page.extract_text() or "") for page in reader.pages]
        text = "\n\n".join(x for x in pages if x).strip()
        if text:
            return text + "\n", "pypdf", ""
    except Exception as exc:
        errors.append(f"pypdf: {type(exc).__name__}: {exc}")

    return "", "", " | ".join(errors)



def pmc_idconv_lookup(
    session: requests.Session,
    *,
    doi: str = "",
    pmid: str = "",
    pmcid: str = "",
    timeout: float = 45.0,
    tool: str = "pride_scp",
    email: str = "",
) -> Optional[dict[str, Any]]:
    """Resolve DOI/PMID/PMCID with NCBI's PMC ID Converter API.

    Unlike Europe PMC's free-text search endpoint, this service accepts article
    identifiers directly and is therefore a robust primary DOI -> PMID/PMCID
    resolver for articles represented in PubMed Central.  It only returns
    related IDs for articles that exist in PMC; callers should retain Europe
    PMC / Crossref / Unpaywall fallbacks for publications outside PMC.
    """
    identifiers: list[str] = []
    norm_doi = normalize_doi(doi)
    norm_pmid = text_value(pmid)
    norm_pmcid = text_value(pmcid)
    if norm_doi:
        identifiers.append(norm_doi)
    if norm_pmcid:
        if not norm_pmcid.upper().startswith("PMC"):
            norm_pmcid = "PMC" + norm_pmcid
        identifiers.append(norm_pmcid)
    if norm_pmid:
        identifiers.append(norm_pmid)
    if not identifiers:
        return None

    base = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/"
    for identifier in identifiers:
        params: dict[str, Any] = {
            "ids": identifier,
            "format": "json",
            "tool": re.sub(r"\s+", "_", tool.strip()) or "pride_scp",
        }
        if email:
            params["email"] = email
        data = get_json(
            session,
            base,
            params=params,
            timeout=timeout,
            headers={"Accept": "application/json"},
        )
        if not isinstance(data, dict) or data.get("status") not in {None, "ok"}:
            continue
        records = data.get("records", []) or []
        if isinstance(records, dict):
            records = [records]
        for record in records:
            if not isinstance(record, dict) or record.get("error"):
                continue
            resolved = {
                "doi": normalize_doi(record.get("doi")) or norm_doi,
                "pmid": text_value(record.get("pmid")) or norm_pmid,
                "pmcid": text_value(record.get("pmcid")) or norm_pmcid,
                "live": record.get("live"),
                "releaseDate": text_value(
                    record.get("release-date") or record.get("releaseDate")
                ),
                "requestedId": text_value(
                    record.get("requested-id") or record.get("requestedId")
                ),
            }
            return resolved
    return None

def _europe_pmc_search(
    session: requests.Session,
    query: str,
    *,
    timeout: float = 45.0,
    page_size: int = 10,
) -> list[dict[str, Any]]:
    """Run a Europe PMC JSON search regardless of the session Accept header."""
    base = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    data = get_json(
        session,
        base,
        params={
            "query": query,
            "format": "json",
            "resultType": "core",
            "pageSize": page_size,
        },
        timeout=timeout,
        headers={"Accept": "application/json"},
    )
    if not isinstance(data, dict):
        return []
    results = data.get("resultList", {}).get("result", []) or []
    if isinstance(results, dict):
        results = [results]
    return [item for item in results if isinstance(item, dict)]


def europe_pmc_lookup(
    session: requests.Session,
    *,
    doi: str = "",
    pmid: str = "",
    title: str = "",
    timeout: float = 45.0,
) -> Optional[dict[str, Any]]:
    """Resolve a publication in Europe PMC using progressively broader keys.

    The legacy pipeline used one quoted DOI query.  In practice Europe PMC
    records can be recovered more reliably by trying DOI, PMID and title forms
    independently, then selecting an exact identifier match when possible.
    """
    norm_doi = normalize_doi(doi)
    norm_pmid = text_value(pmid)
    norm_title = text_value(title)

    queries: list[str] = []
    if norm_doi:
        queries.extend(
            [
                f"DOI:{norm_doi}",
                f'DOI:"{norm_doi}"',
            ]
        )
    if norm_pmid:
        queries.extend(
            [
                f"EXT_ID:{norm_pmid} AND SRC:MED",
                f"EXT_ID:{norm_pmid}",
            ]
        )
    if norm_title:
        escaped = norm_title.replace('"', " ")
        queries.append(f'TITLE:"{escaped}"')

    if not queries:
        return None

    seen_queries: set[str] = set()
    fallback: Optional[dict[str, Any]] = None

    def normalized_title(value: Any) -> str:
        text = text_value(value).lower()
        return re.sub(r"[^a-z0-9]+", " ", text).strip()

    wanted_title = normalized_title(norm_title)

    for query in queries:
        if query in seen_queries:
            continue
        seen_queries.add(query)
        results = _europe_pmc_search(
            session,
            query,
            timeout=timeout,
            page_size=10,
        )
        for result in results:
            if norm_doi and normalize_doi(result.get("doi")) == norm_doi:
                return result
            if norm_pmid and text_value(result.get("pmid")) == norm_pmid:
                return result
            if norm_title:
                candidate_title = normalized_title(result.get("title"))
                if candidate_title and candidate_title == wanted_title:
                    return result
            # A non-exact fallback is acceptable only when an identifier was
            # supplied.  Title-only lookup must never silently attach a
            # different paper to an identifier-less PRIDE record.
            if (norm_doi or norm_pmid) and fallback is None:
                fallback = result

    return fallback


def europe_pmc_pdf_urls(record: Optional[dict[str, Any]]) -> list[tuple[str, str]]:
    if not record:
        return []

    candidates: list[tuple[str, str]] = []
    ft = record.get("fullTextUrlList", {}).get("fullTextUrl", []) or []

    if isinstance(ft, dict):
        ft = [ft]

    for item in ft:
        if not isinstance(item, dict):
            continue
        style = text_value(item.get("documentStyle")).lower()
        url = text_value(item.get("url"))
        if style == "pdf" and url:
            candidates.append(("europe_pmc_fullTextUrl", url))

    # Do not require the historically brittle hasPDF == Y field.  A known
    # PMCID is enough to try the official PMC/Europe-PMC render endpoints; the
    # downloader still validates the PDF magic bytes before accepting a file.
    pmcid = text_value(record.get("pmcid"))
    if pmcid:
        if not pmcid.upper().startswith("PMC"):
            pmcid = "PMC" + pmcid
        candidates.extend(
            [
                (
                    "europe_pmc_render",
                    f"https://europepmc.org/articles/{pmcid}?pdf=render",
                ),
                (
                    "pmc_pdf",
                    f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/",
                ),
                (
                    "pmc_pdf_legacy",
                    f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/pdf/",
                ),
            ]
        )

    seen = set()
    out = []
    for source, url in candidates:
        if url and url not in seen:
            out.append((source, url))
            seen.add(url)
    return out

def crossref_lookup(
    session: requests.Session,
    doi: str,
    *,
    mailto: str = "",
    timeout: float = 45.0,
) -> Optional[dict[str, Any]]:
    doi = normalize_doi(doi)
    if not doi:
        return None

    url = "https://api.crossref.org/works/" + quote(doi, safe="")
    params = {"mailto": mailto} if mailto else None

    try:
        data = get_json(session, url, params=params, timeout=timeout)
    except requests.RequestException:
        return None

    return data.get("message") if isinstance(data, dict) else None


def choose_crossref_date(record: Optional[dict[str, Any]]) -> str:
    if not record:
        return ""

    for key in ("published-print", "published-online", "published", "created"):
        value = record.get(key)
        if not isinstance(value, dict):
            continue
        parts = value.get("date-parts")
        if (
            isinstance(parts, list)
            and parts
            and isinstance(parts[0], list)
            and parts[0]
        ):
            nums = parts[0]
            if len(nums) >= 3:
                return f"{nums[0]:04d}-{nums[1]:02d}-{nums[2]:02d}"
            if len(nums) == 2:
                return f"{nums[0]:04d}-{nums[1]:02d}"
            return str(nums[0])

    return ""


def unpaywall_lookup(
    session: requests.Session,
    doi: str,
    email: str,
    *,
    timeout: float = 45.0,
) -> Optional[dict[str, Any]]:
    doi = normalize_doi(doi)
    if not doi or not email:
        return None

    url = "https://api.unpaywall.org/v2/" + quote(doi, safe="")
    try:
        return get_json(
            session,
            url,
            params={"email": email},
            timeout=timeout,
        )
    except requests.RequestException:
        return None


def unpaywall_pdf_urls(record: Optional[dict[str, Any]]) -> list[tuple[str, str]]:
    if not record:
        return []

    candidates: list[tuple[str, str]] = []

    best = record.get("best_oa_location")
    locations = []
    if isinstance(best, dict):
        locations.append(best)

    for item in record.get("oa_locations", []) or []:
        if isinstance(item, dict):
            locations.append(item)

    seen = set()
    for location in locations:
        url = text_value(location.get("url_for_pdf"))
        if url and url not in seen:
            candidates.append(("unpaywall", url))
            seen.add(url)

    return candidates
