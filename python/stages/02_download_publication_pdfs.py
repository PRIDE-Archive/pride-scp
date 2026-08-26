#!/usr/bin/env python3
"""Stage 2: resolve, reuse, or manually supply publication PDFs.

v0.1.6 resolution order
-----------------------
1. Existing validated PDF in the current work directory.
2. Manual PDF manifest / manual PDF directory.
3. Validated PDFs from one or more legacy/reuse directories.
4. NCBI PMC ID Converter (DOI/PMID/PMCID -> PMCID) + PMC PDF fallbacks.
5. Europe PMC lookup (DOI, PMID, then exact title) as secondary metadata lookup.
6. Manifest-provided PDF candidate URL.
7. Unpaywall when an email is supplied.

Every downloaded/reused file is validated by PDF magic bytes.  Resolution is
cached per unique publication, but the v0.1.6 cache schema intentionally
invalidates the previous `no_open_access_pdf` caches produced before the NCBI
identifier-conversion fallback.  Adding a manual/reuse PDF also overrides a cached unresolved result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from pride_scp_pipeline_common import (
    build_session,
    europe_pmc_lookup,
    europe_pmc_pdf_urls,
    pmc_idconv_lookup,
    normalize_doi,
    publication_filename,
    publication_key,
    read_tsv,
    safe_slug,
    text_value,
    unpaywall_lookup,
    unpaywall_pdf_urls,
    validate_pdf_path,
    write_tsv,
)


RESOLVER_PATCH_VERSION = "pride-scp-v0.1.6"
CACHE_SCHEMA_VERSION = 3
_thread_local = threading.local()

TERMINAL_CACHE_STATUSES = {
    "downloaded",
    "already_exists",
    "no_open_access_pdf",
    "no_publication_identifier",
    "download_error",
    "worker_error",
}
ERROR_STATUSES = {"download_error", "worker_error"}
UNRESOLVED_STATUSES = {
    "no_open_access_pdf",
    "no_publication_identifier",
    "download_error",
    "worker_error",
}


def session_for_thread(user_agent: str) -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = build_session(user_agent=user_agent)
        # Individual metadata helpers override this to JSON when appropriate.
        session.headers.update(
            {"Accept": "application/pdf,text/html;q=0.8,*/*;q=0.5"}
        )
        _thread_local.session = session
    return session


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_tsv")
    parser.add_argument("--output", default="pride_publications_with_pdfs.tsv")
    parser.add_argument("--pdf-dir", default="publication_pdfs")
    parser.add_argument(
        "--cache-dir",
        default="",
        help="Persistent resolution cache. Default: <pdf-dir>/.resolution_cache",
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--unpaywall-email",
        default=os.environ.get("UNPAYWALL_EMAIL", ""),
    )
    parser.add_argument(
        "--contact-email",
        default=os.environ.get(
            "NCBI_EMAIL", os.environ.get("CONTACT_EMAIL", "")
        ),
        help=(
            "Optional contact email sent to NCBI PMC ID Converter requests. "
            "Defaults to NCBI_EMAIL or CONTACT_EMAIL."
        ),
    )
    parser.add_argument(
        "--user-agent",
        default="PRIDE-SCP-pdf-downloader/1.6",
    )
    parser.add_argument(
        "--reuse-pdf-dir",
        action="append",
        default=[],
        help=(
            "Directory containing PDFs downloaded by an older pipeline. "
            "Repeat this option for multiple directories."
        ),
    )
    parser.add_argument(
        "--manual-pdf-dir",
        default="",
        help=(
            "Directory for manually supplied PDFs. Files may be named by "
            "accession (PXDxxxxxx.pdf) or by the normal publication filename."
        ),
    )
    parser.add_argument(
        "--manual-pdf-manifest",
        default="",
        help=(
            "Optional TSV mapping manual PDFs. Supported columns: accession, "
            "publication_doi, publication_pmid, pdf_path, note."
        ),
    )
    parser.add_argument(
        "--manual-queue",
        default="",
        help="Write unresolved publication rows here for manual retrieval.",
    )
    parser.add_argument(
        "--reuse-mode",
        choices=("symlink", "hardlink", "copy"),
        default="symlink",
        help="How reused/manual PDFs are materialized under --pdf-dir.",
    )
    parser.add_argument(
        "--retry-errors",
        action="store_true",
        help="Retry cached download_error/worker_error records.",
    )
    parser.add_argument(
        "--retry-unresolved",
        action="store_true",
        help=(
            "Retry all unresolved cached records, including no_open_access_pdf "
            "and no_publication_identifier."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Ignore resolution cache and resolve again. Existing/reused/manual "
            "validated PDF files are still reused."
        ),
    )
    return parser.parse_args()


def publication_identity(row: dict[str, Any]) -> dict[str, str]:
    return {
        "publication_key": publication_key(row),
        "publication_doi": normalize_doi(row.get("publication_doi")),
        "publication_pmid": text_value(row.get("publication_pmid")),
        "publication_title": text_value(row.get("publication_title")),
    }


def cache_file_for(row: dict[str, Any], cache_dir: Path) -> Path:
    key = publication_key(row)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    slug = safe_slug(key, max_len=80)
    return cache_dir / f"{slug}__{digest}.json"


def read_cache(row: dict[str, Any], cache_dir: Path) -> dict[str, Any] | None:
    path = cache_file_for(row, cache_dir)
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if record.get("cache_schema_version") != CACHE_SCHEMA_VERSION:
        return None
    identity = record.get("publication_identity", {})
    if identity.get("publication_key") != publication_key(row):
        return None
    result = record.get("result")
    return result if isinstance(result, dict) else None


def write_cache(
    row: dict[str, Any], cache_dir: Path, result: dict[str, Any]
) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_file_for(row, cache_dir)
    record = {
        "cache_schema_version": CACHE_SCHEMA_VERSION,
        "resolver_patch_version": RESOLVER_PATCH_VERSION,
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "publication_identity": publication_identity(row),
        "result": result,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(record, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(path)


def cache_is_usable(
    cached: dict[str, Any], *, retry_errors: bool, retry_unresolved: bool
) -> bool:
    status = text_value(cached.get("pdf_status"))
    if status not in TERMINAL_CACHE_STATUSES:
        return False
    if retry_unresolved and status in UNRESOLVED_STATUSES:
        return False
    if retry_errors and status in ERROR_STATUSES:
        return False
    if status in {"downloaded", "already_exists"}:
        path_text = text_value(cached.get("pdf_path"))
        if not path_text or not validate_pdf_path(Path(path_text)):
            return False
    return True


def candidate_accessions(row: dict[str, Any]) -> list[str]:
    values: list[str] = []
    raw = row.get("_candidate_accessions")
    if isinstance(raw, list):
        values.extend(text_value(v).upper() for v in raw)
    elif raw:
        values.extend(x.strip().upper() for x in text_value(raw).split(";") if x.strip())
    accession = text_value(row.get("accession")).upper()
    if accession:
        values.append(accession)
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if re.fullmatch(r"PXD\d{6,}", value) and value not in seen:
            out.append(value)
            seen.add(value)
    return out


def build_pdf_index(directories: list[Path]) -> dict[str, Path]:
    index: dict[str, Path] = {}
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in directory.rglob("*.pdf"):
            try:
                if not validate_pdf_path(path):
                    continue
            except OSError:
                continue
            resolved = path.resolve()
            index.setdefault(path.name.lower(), resolved)
            index.setdefault(path.stem.lower(), resolved)
            for match in re.findall(r"PXD\d{6,}", path.name.upper()):
                index.setdefault(match.lower(), resolved)
                index.setdefault((match + ".pdf").lower(), resolved)
    return index


def load_manual_manifest(path: Path | None) -> dict[str, Path]:
    if path is None or not path.is_file():
        return {}
    index: dict[str, Path] = {}
    for row in read_tsv(path):
        raw_path = text_value(row.get("pdf_path"))
        if not raw_path:
            continue
        pdf = Path(raw_path)
        if not pdf.is_absolute():
            pdf = path.parent / pdf
        if not validate_pdf_path(pdf):
            continue
        pdf = pdf.resolve()
        accession = text_value(row.get("accession")).upper()
        doi = normalize_doi(row.get("publication_doi"))
        pmid = text_value(row.get("publication_pmid"))
        if accession:
            index["accession:" + accession] = pdf
        if doi:
            index["doi:" + doi] = pdf
        if pmid:
            index["pmid:" + pmid] = pdf
    return index


def find_external_pdf(
    row: dict[str, Any],
    *,
    manual_manifest: dict[str, Path],
    manual_index: dict[str, Path],
    reuse_index: dict[str, Path],
) -> tuple[Path | None, str]:
    doi = normalize_doi(row.get("publication_doi"))
    pmid = text_value(row.get("publication_pmid"))
    accessions = candidate_accessions(row)

    manifest_keys: list[str] = []
    manifest_keys.extend("accession:" + x for x in accessions)
    if doi:
        manifest_keys.append("doi:" + doi)
    if pmid:
        manifest_keys.append("pmid:" + pmid)
    for key in manifest_keys:
        path = manual_manifest.get(key)
        if path and validate_pdf_path(path):
            return path, "manual_manifest"

    names = [publication_filename(row).lower()]
    names.extend((x + ".pdf").lower() for x in accessions)
    names.extend(x.lower() for x in accessions)
    for name in names:
        path = manual_index.get(name)
        if path and validate_pdf_path(path):
            return path, "manual_pdf"
    for name in names:
        path = reuse_index.get(name)
        if path and validate_pdf_path(path):
            return path, "legacy_pdf"
    return None, ""


def materialize_pdf(source: Path, destination: Path, mode: str) -> None:
    source = source.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        if validate_pdf_path(destination):
            return
        destination.unlink(missing_ok=True)
    if destination.exists() and source == destination.resolve():
        return
    if mode == "copy":
        shutil.copy2(source, destination)
    elif mode == "hardlink":
        try:
            os.link(source, destination)
        except OSError:
            shutil.copy2(source, destination)
    else:
        relative = os.path.relpath(source, destination.parent)
        destination.symlink_to(relative)
    if not validate_pdf_path(destination):
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"reused PDF failed validation: {source}")


def base_result(row: dict[str, Any], pdf_path: Path) -> dict[str, Any]:
    doi = normalize_doi(row.get("publication_doi"))
    pmid = text_value(row.get("publication_pmid"))
    pmcid = text_value(row.get("publication_pmcid"))
    return {
        "pdf_status": "",
        "pdf_path": str(pdf_path),
        "pdf_url": "",
        "pdf_source": "",
        "pdf_error": "",
        "resolved_doi": doi,
        "resolved_pmid": pmid,
        "resolved_pmcid": pmcid,
        "resolved_is_open_access": text_value(row.get("publication_is_open_access")),
        "manual_article_url": "",
        "manual_pdf_suggested_filename": publication_filename(row),
        "pdf_resolution_trace": "[]",
    }


def external_pdf_result(
    row: dict[str, Any],
    *,
    pdf_dir: Path,
    manual_manifest: dict[str, Path],
    manual_index: dict[str, Path],
    reuse_index: dict[str, Path],
    reuse_mode: str,
) -> dict[str, Any] | None:
    pdf_path = pdf_dir / "by_publication" / publication_filename(row)
    if validate_pdf_path(pdf_path):
        result = base_result(row, pdf_path)
        result.update(
            {
                "pdf_status": "already_exists",
                "pdf_source": "existing_file",
                "pdf_resolution_trace": json.dumps(
                    [{"source": "existing_file", "status": "validated"}],
                    ensure_ascii=False,
                ),
            }
        )
        return result

    source, source_kind = find_external_pdf(
        row,
        manual_manifest=manual_manifest,
        manual_index=manual_index,
        reuse_index=reuse_index,
    )
    if source is None:
        return None
    materialize_pdf(source, pdf_path, reuse_mode)
    result = base_result(row, pdf_path)
    result.update(
        {
            "pdf_status": "already_exists",
            "pdf_source": source_kind,
            "pdf_resolution_trace": json.dumps(
                [
                    {
                        "source": source_kind,
                        "status": "reused",
                        "path": str(source),
                    }
                ],
                ensure_ascii=False,
            ),
        }
    )
    return result


def download_candidate(
    session: requests.Session, url: str, path: Path, timeout: float
) -> tuple[bool, str]:
    try:
        response = session.get(
            url,
            timeout=timeout,
            allow_redirects=True,
            stream=True,
            headers={"Accept": "application/pdf,*/*;q=0.5"},
        )
        response.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.unlink(missing_ok=True)
        first = b""
        size = 0
        try:
            with tmp.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 128):
                    if not chunk:
                        continue
                    if not first:
                        first = chunk[:1024]
                        if not first.lstrip().startswith(b"%PDF-"):
                            tmp.unlink(missing_ok=True)
                            return (
                                False,
                                "not_pdf "
                                f"content_type={response.headers.get('Content-Type','')} "
                                f"final_url={response.url}",
                            )
                    handle.write(chunk)
                    size += len(chunk)
            if size < 512:
                tmp.unlink(missing_ok=True)
                return False, f"pdf_too_small bytes={size}"
            tmp.replace(path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        if not validate_pdf_path(path):
            path.unlink(missing_ok=True)
            return False, "invalid_pdf_after_write"
        return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def article_url(doi: str, pmid: str, pmcid: str) -> str:
    if pmcid:
        pmcid = pmcid if pmcid.upper().startswith("PMC") else "PMC" + pmcid
        return f"https://europepmc.org/articles/{pmcid}"
    if doi:
        return f"https://doi.org/{doi}"
    if pmid:
        return f"https://europepmc.org/article/MED/{pmid}"
    return ""


def resolve_uncached(
    row: dict[str, Any],
    *,
    pdf_dir: Path,
    manual_manifest: dict[str, Path],
    manual_index: dict[str, Path],
    reuse_index: dict[str, Path],
    reuse_mode: str,
    unpaywall_email: str,
    contact_email: str,
    user_agent: str,
    timeout: float,
) -> dict[str, Any]:
    external = external_pdf_result(
        row,
        pdf_dir=pdf_dir,
        manual_manifest=manual_manifest,
        manual_index=manual_index,
        reuse_index=reuse_index,
        reuse_mode=reuse_mode,
    )
    if external is not None:
        return external

    session = session_for_thread(user_agent)
    doi = normalize_doi(row.get("publication_doi"))
    pmid = text_value(row.get("publication_pmid"))
    title = text_value(row.get("publication_title"))
    supplied_pmcid = text_value(row.get("publication_pmcid"))
    pdf_path = pdf_dir / "by_publication" / publication_filename(row)
    result = base_result(row, pdf_path)
    trace: list[dict[str, Any]] = []
    candidates: list[tuple[str, str]] = []
    errors: list[str] = []

    idconv = None
    if doi or pmid or supplied_pmcid:
        try:
            idconv = pmc_idconv_lookup(
                session,
                doi=doi,
                pmid=pmid,
                pmcid=supplied_pmcid,
                timeout=timeout,
                tool="pride_scp_pdf_resolver",
                email=contact_email,
            )
            if idconv:
                resolved_doi = normalize_doi(idconv.get("doi")) or doi
                resolved_pmid = text_value(idconv.get("pmid")) or pmid
                resolved_pmcid = text_value(idconv.get("pmcid")) or supplied_pmcid
                result["resolved_doi"] = resolved_doi
                result["resolved_pmid"] = resolved_pmid
                result["resolved_pmcid"] = resolved_pmcid
                trace.append(
                    {
                        "source": "NCBI_PMC_IDConverter",
                        "status": "matched",
                        "doi": resolved_doi,
                        "pmid": resolved_pmid,
                        "pmcid": resolved_pmcid,
                        "live": idconv.get("live"),
                        "release_date": text_value(idconv.get("releaseDate")),
                    }
                )
                candidates.extend(europe_pmc_pdf_urls({"pmcid": resolved_pmcid}))
            else:
                trace.append(
                    {"source": "NCBI_PMC_IDConverter", "status": "no_match"}
                )
        except Exception as exc:
            trace.append(
                {
                    "source": "NCBI_PMC_IDConverter",
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            errors.append(
                f"NCBI PMC ID Converter: {type(exc).__name__}: {exc}"
            )
    else:
        trace.append(
            {"source": "NCBI_PMC_IDConverter", "status": "skipped_no_identifier"}
        )

    epmc = None
    if not idconv and (doi or pmid or title):
        try:
            epmc = europe_pmc_lookup(
                session,
                doi=doi,
                pmid=pmid,
                title=title,
                timeout=timeout,
            )
            if epmc:
                resolved_doi = normalize_doi(epmc.get("doi")) or doi
                resolved_pmid = text_value(epmc.get("pmid")) or pmid
                resolved_pmcid = text_value(epmc.get("pmcid")) or supplied_pmcid
                result["resolved_doi"] = resolved_doi
                result["resolved_pmid"] = resolved_pmid
                result["resolved_pmcid"] = resolved_pmcid
                result["resolved_is_open_access"] = text_value(
                    epmc.get("isOpenAccess")
                ) or result["resolved_is_open_access"]
                trace.append(
                    {
                        "source": "EuropePMC",
                        "status": "matched",
                        "doi": resolved_doi,
                        "pmid": resolved_pmid,
                        "pmcid": resolved_pmcid,
                        "hasPDF": text_value(epmc.get("hasPDF")),
                    }
                )
                candidates.extend(europe_pmc_pdf_urls(epmc))
            else:
                trace.append({"source": "EuropePMC", "status": "no_match"})
                errors.append("EuropePMC: no matching record")
        except Exception as exc:
            trace.append(
                {
                    "source": "EuropePMC",
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            errors.append(f"EuropePMC lookup: {type(exc).__name__}: {exc}")
    elif idconv:
        trace.append(
            {
                "source": "EuropePMC",
                "status": "skipped_idconv_already_resolved",
            }
        )
    else:
        trace.append({"source": "EuropePMC", "status": "skipped_no_metadata"})
        errors.append("EuropePMC: no DOI, PMID, or publication title")

    # Even if the lookup failed or hasPDF is absent, a supplied PMCID is enough
    # to try the official render endpoints.
    effective_pmcid = text_value(result.get("resolved_pmcid")) or supplied_pmcid
    if effective_pmcid and not epmc and not idconv:
        candidates.extend(europe_pmc_pdf_urls({"pmcid": effective_pmcid}))
        trace.append(
            {
                "source": "PMCID_fallback",
                "status": "candidate_urls_added",
                "pmcid": effective_pmcid,
            }
        )

    manifest_candidate = text_value(row.get("publication_pdf_candidate_url"))
    if manifest_candidate:
        candidates.insert(0, ("manifest_candidate", manifest_candidate))
        trace.append({"source": "manifest_candidate", "status": "present"})
    else:
        trace.append({"source": "manifest_candidate", "status": "absent"})

    effective_doi = normalize_doi(result.get("resolved_doi")) or doi
    if effective_doi and unpaywall_email:
        try:
            unpaywall = unpaywall_lookup(
                session,
                effective_doi,
                unpaywall_email,
                timeout=timeout,
            )
            if unpaywall:
                trace.append(
                    {
                        "source": "Unpaywall",
                        "status": "matched",
                        "is_oa": unpaywall.get("is_oa"),
                    }
                )
                if unpaywall.get("is_oa") is True:
                    result["resolved_is_open_access"] = "Y"
                candidates.extend(unpaywall_pdf_urls(unpaywall))
            else:
                trace.append({"source": "Unpaywall", "status": "no_match"})
                errors.append("Unpaywall: no matching OA record")
        except Exception as exc:
            trace.append(
                {
                    "source": "Unpaywall",
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            errors.append(f"Unpaywall lookup: {type(exc).__name__}: {exc}")
    elif effective_doi:
        trace.append({"source": "Unpaywall", "status": "disabled_no_email"})
        errors.append("Unpaywall: disabled because no email was supplied")
    else:
        trace.append({"source": "Unpaywall", "status": "skipped_no_doi"})

    seen: set[str] = set()
    unique_candidates: list[tuple[str, str]] = []
    for source, url in candidates:
        if url and url not in seen:
            seen.add(url)
            unique_candidates.append((source, url))

    for source, url in unique_candidates:
        ok, error = download_candidate(session, url, pdf_path, timeout)
        trace.append(
            {
                "source": source,
                "status": "downloaded" if ok else "download_failed",
                "url": url,
                "error": error,
            }
        )
        if ok:
            result.update(
                {
                    "pdf_status": "downloaded",
                    "pdf_url": url,
                    "pdf_source": source,
                    "pdf_error": "",
                }
            )
            result["manual_article_url"] = article_url(
                normalize_doi(result.get("resolved_doi")),
                text_value(result.get("resolved_pmid")),
                text_value(result.get("resolved_pmcid")),
            )
            result["pdf_resolution_trace"] = json.dumps(trace, ensure_ascii=False)
            return result
        errors.append(f"{source}: {error}")

    if unique_candidates:
        result["pdf_status"] = "download_error"
    elif not doi and not pmid and not title:
        result["pdf_status"] = "no_publication_identifier"
    else:
        result["pdf_status"] = "no_open_access_pdf"

    result["manual_article_url"] = article_url(
        normalize_doi(result.get("resolved_doi")) or doi,
        text_value(result.get("resolved_pmid")) or pmid,
        text_value(result.get("resolved_pmcid")) or supplied_pmcid,
    )
    if not errors:
        errors.append("No validated PDF candidate was discovered")
    result["pdf_error"] = " | ".join(errors)[:8000]
    result["pdf_resolution_trace"] = json.dumps(trace, ensure_ascii=False)
    return result


def resolve_and_cache(
    key: str,
    row: dict[str, Any],
    *,
    pdf_dir: Path,
    cache_dir: Path,
    manual_manifest: dict[str, Path],
    manual_index: dict[str, Path],
    reuse_index: dict[str, Path],
    reuse_mode: str,
    unpaywall_email: str,
    contact_email: str,
    user_agent: str,
    timeout: float,
) -> tuple[str, dict[str, Any]]:
    try:
        result = resolve_uncached(
            row,
            pdf_dir=pdf_dir,
            manual_manifest=manual_manifest,
            manual_index=manual_index,
            reuse_index=reuse_index,
            reuse_mode=reuse_mode,
            unpaywall_email=unpaywall_email,
            contact_email=contact_email,
            user_agent=user_agent,
            timeout=timeout,
        )
    except Exception as exc:
        result = base_result(row, pdf_dir / "by_publication" / publication_filename(row))
        result.update(
            {
                "pdf_status": "worker_error",
                "pdf_path": "",
                "pdf_error": f"{type(exc).__name__}: {exc}",
                "pdf_resolution_trace": json.dumps(
                    [
                        {
                            "source": "worker",
                            "status": "error",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    ],
                    ensure_ascii=False,
                ),
            }
        )
    write_cache(row, cache_dir, result)
    return key, result


def write_manual_queue(
    path: Path,
    representative: dict[str, dict[str, Any]],
    resolved: dict[str, dict[str, Any]],
) -> None:
    rows: list[dict[str, Any]] = []
    for key, row in representative.items():
        result = resolved.get(key, {})
        status = text_value(result.get("pdf_status"))
        if status not in UNRESOLVED_STATUSES:
            continue
        accessions = candidate_accessions(row)
        rows.append(
            {
                "accessions": ";".join(accessions),
                "publication_key": key,
                "publication_title": text_value(row.get("publication_title")),
                "publication_doi": normalize_doi(row.get("publication_doi")),
                "publication_pmid": text_value(row.get("publication_pmid")),
                "resolved_doi": text_value(result.get("resolved_doi")),
                "resolved_pmid": text_value(result.get("resolved_pmid")),
                "resolved_pmcid": text_value(result.get("resolved_pmcid")),
                "pdf_status": status,
                "pdf_error": text_value(result.get("pdf_error")),
                "article_url": text_value(result.get("manual_article_url")),
                "suggested_filename": text_value(
                    result.get("manual_pdf_suggested_filename")
                ),
                "pdf_resolution_trace": text_value(result.get("pdf_resolution_trace")),
            }
        )
    rows.sort(key=lambda x: (x["accessions"], x["publication_key"]))
    fieldnames = [
        "accessions",
        "publication_key",
        "publication_title",
        "publication_doi",
        "publication_pmid",
        "resolved_doi",
        "resolved_pmid",
        "resolved_pmcid",
        "pdf_status",
        "pdf_error",
        "article_url",
        "suggested_filename",
        "pdf_resolution_trace",
    ]
    write_tsv(path, rows, fieldnames)


def main() -> None:
    args = parse_args()
    rows = read_tsv(Path(args.input_tsv))
    pdf_dir = Path(args.pdf_dir)
    cache_dir = Path(args.cache_dir) if args.cache_dir else pdf_dir / ".resolution_cache"
    output = Path(args.output)

    manual_dir = Path(args.manual_pdf_dir) if args.manual_pdf_dir else None
    reuse_dirs = [Path(x).expanduser() for x in args.reuse_pdf_dir if x]
    manual_dirs = [manual_dir] if manual_dir and manual_dir.is_dir() else []
    manual_index = build_pdf_index(manual_dirs)
    reuse_index = build_pdf_index(reuse_dirs)
    manual_manifest = load_manual_manifest(
        Path(args.manual_pdf_manifest) if args.manual_pdf_manifest else None
    )

    publication_rows = [
        row for row in rows if row.get("publication_status") == "publication_found"
    ]
    grouped: dict[str, list[int]] = {}
    for index, row in enumerate(publication_rows):
        grouped.setdefault(publication_key(row), []).append(index)

    representative: dict[str, dict[str, Any]] = {}
    for key, indexes in grouped.items():
        rep = dict(publication_rows[indexes[0]])
        rep["_candidate_accessions"] = [
            text_value(publication_rows[i].get("accession")).upper() for i in indexes
        ]
        representative[key] = rep

    print(f"Resolver patch: {RESOLVER_PATCH_VERSION}")
    print(f"Publication rows: {len(publication_rows):,}")
    print(f"Unique publications: {len(representative):,}")
    print(f"Workers: {args.workers}")
    print(f"Resolution cache: {cache_dir}")
    print(f"Manual PDFs indexed: {len(manual_index):,} lookup keys")
    print(f"Legacy/reuse PDFs indexed: {len(reuse_index):,} lookup keys")

    resolved: dict[str, dict[str, Any]] = {}
    to_resolve: dict[str, dict[str, Any]] = {}
    external_hits = 0
    cache_hits = 0
    retrying = 0
    stale_cache = 0

    for key, row in representative.items():
        # Manual/reuse PDFs always override unresolved cache, so a PDF added
        # after a previous run is picked up without --force.
        external = external_pdf_result(
            row,
            pdf_dir=pdf_dir,
            manual_manifest=manual_manifest,
            manual_index=manual_index,
            reuse_index=reuse_index,
            reuse_mode=args.reuse_mode,
        )
        if external is not None:
            resolved[key] = external
            external_hits += 1
            write_cache(row, cache_dir, external)
            continue

        if not args.force:
            cached = read_cache(row, cache_dir)
            if cached is not None:
                status = text_value(cached.get("pdf_status"))
                if (
                    (args.retry_unresolved and status in UNRESOLVED_STATUSES)
                    or (args.retry_errors and status in ERROR_STATUSES)
                ):
                    retrying += 1
                elif cache_is_usable(
                    cached,
                    retry_errors=args.retry_errors,
                    retry_unresolved=args.retry_unresolved,
                ):
                    resolved[key] = cached
                    cache_hits += 1
                    continue
                else:
                    stale_cache += 1
        to_resolve[key] = row

    print(f"Existing/manual/reused PDFs: {external_hits:,}")
    print(f"Cached resolution results reused: {cache_hits:,}")
    if retrying:
        print(f"Cached unresolved/errors selected for retry: {retrying:,}")
    if stale_cache:
        print(f"Stale/old-schema cache entries ignored: {stale_cache:,}")
    print(f"Publications requiring network resolution: {len(to_resolve):,}")

    if to_resolve:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            future_map = {
                executor.submit(
                    resolve_and_cache,
                    key,
                    row,
                    pdf_dir=pdf_dir,
                    cache_dir=cache_dir,
                    manual_manifest=manual_manifest,
                    manual_index=manual_index,
                    reuse_index=reuse_index,
                    reuse_mode=args.reuse_mode,
                    unpaywall_email=args.unpaywall_email,
                    contact_email=args.contact_email,
                    user_agent=args.user_agent,
                    timeout=args.timeout,
                ): key
                for key, row in to_resolve.items()
            }
            total = len(future_map)
            for completed, future in enumerate(as_completed(future_map), start=1):
                key = future_map[future]
                try:
                    resolved_key, result = future.result()
                    resolved[resolved_key] = result
                except Exception as exc:
                    row = to_resolve[key]
                    result = base_result(
                        row, pdf_dir / "by_publication" / publication_filename(row)
                    )
                    result.update(
                        {
                            "pdf_status": "worker_error",
                            "pdf_path": "",
                            "pdf_error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    resolved[key] = result
                    write_cache(row, cache_dir, result)
                if completed % 25 == 0 or completed == total:
                    print(
                        f"Resolved this run {completed:,}/{total:,} "
                        f"(overall {len(resolved):,}/{len(representative):,})"
                    )

    output_rows: list[dict[str, Any]] = []
    extra = [
        "pdf_status",
        "pdf_path",
        "pdf_url",
        "pdf_source",
        "pdf_error",
        "resolved_doi",
        "resolved_pmid",
        "resolved_pmcid",
        "resolved_is_open_access",
        "manual_article_url",
        "manual_pdf_suggested_filename",
        "pdf_resolution_trace",
    ]
    for row in rows:
        out = dict(row)
        if row.get("publication_status") == "publication_found":
            out.update(
                resolved.get(
                    publication_key(row),
                    {
                        "pdf_status": "internal_missing_result",
                        "pdf_error": "No resolution result was produced",
                    },
                )
            )
        else:
            out.update(
                {
                    "pdf_status": "not_applicable",
                    "pdf_path": "",
                    "pdf_url": "",
                    "pdf_source": "",
                    "pdf_error": "",
                    "resolved_doi": "",
                    "resolved_pmid": "",
                    "resolved_pmcid": "",
                    "resolved_is_open_access": "",
                    "manual_article_url": "",
                    "manual_pdf_suggested_filename": "",
                    "pdf_resolution_trace": "[]",
                }
            )
        output_rows.append(out)

    fieldnames = list(rows[0].keys()) if rows else []
    fieldnames.extend(name for name in extra if name not in fieldnames)
    write_tsv(output, output_rows, fieldnames)

    if args.manual_queue:
        manual_queue = Path(args.manual_queue)
    else:
        manual_queue = output.parent / "manual_pdf_queue.tsv"
    write_manual_queue(manual_queue, representative, resolved)

    counts: dict[str, int] = {}
    for row in output_rows:
        status = text_value(row.get("pdf_status"))
        counts[status] = counts.get(status, 0) + 1

    print(f"Saved: {output}")
    print(f"Manual PDF queue: {manual_queue}")
    for status, count in sorted(counts.items()):
        print(f"{status:24s} {count:8,d}")


if __name__ == "__main__":
    main()
