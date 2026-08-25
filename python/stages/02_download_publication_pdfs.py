#!/usr/bin/env python3
"""
Stage 2: resolve and download freely available publication PDFs.

Resolution order:
  1. Europe PMC / PMC
  2. Unpaywall, when an email is supplied

The downloader validates the PDF magic signature instead of treating an HTTP
200 HTML/paywall page as a PDF.

RESUMABILITY
------------
Each unique publication gets an independent cache JSON written atomically as
soon as resolution finishes. An interrupted run therefore resumes without
repeating completed Europe PMC / Unpaywall lookups.

Existing valid PDFs are also detected directly from disk, so PDFs downloaded
by an older version of this script are reused automatically.

Default cache:
    <pdf-dir>/.resolution_cache/

Cached statuses reused by default:
    downloaded
    already_exists
    no_open_access_pdf
    no_publication_identifier
    download_error
    worker_error

Use --retry-errors to retry cached download_error/worker_error records.
Use --force to ignore cached resolution results and resolve again.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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


_thread_local = threading.local()
CACHE_SCHEMA_VERSION = 1

TERMINAL_CACHE_STATUSES = {
    "downloaded",
    "already_exists",
    "no_open_access_pdf",
    "no_publication_identifier",
    "download_error",
    "worker_error",
}

RETRYABLE_ERROR_STATUSES = {
    "download_error",
    "worker_error",
}


def session_for_thread(user_agent: str):
    session = getattr(_thread_local, "session", None)

    if session is None:
        session = build_session(user_agent=user_agent)
        session.headers.update(
            {
                "Accept": (
                    "application/pdf,text/html;q=0.8,*/*;q=0.5"
                )
            }
        )
        _thread_local.session = session

    return session


def parse_args():
    parser = argparse.ArgumentParser()

    parser.add_argument("input_tsv")

    parser.add_argument(
        "--output",
        default="pride_publications_with_pdfs.tsv",
    )

    parser.add_argument(
        "--pdf-dir",
        default="publication_pdfs",
    )

    parser.add_argument(
        "--cache-dir",
        default="",
        help=(
            "Persistent resolution cache. "
            "Default: <pdf-dir>/.resolution_cache"
        ),
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=4,
    )

    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
    )

    parser.add_argument(
        "--unpaywall-email",
        default=os.environ.get("UNPAYWALL_EMAIL", ""),
    )

    parser.add_argument(
        "--user-agent",
        default="PRIDE-SCP-pdf-downloader/1.1",
    )

    parser.add_argument(
        "--retry-errors",
        action="store_true",
        help=(
            "Retry cached download_error/worker_error records, "
            "while still reusing other cached results."
        ),
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Ignore resolution-cache results and resolve publications "
            "again. Existing valid PDF files are still reused."
        ),
    )

    return parser.parse_args()


def publication_identity(row: dict[str, Any]) -> dict[str, str]:
    return {
        "publication_key": publication_key(row),
        "publication_doi": normalize_doi(
            row.get("publication_doi")
        ),
        "publication_pmid": text_value(
            row.get("publication_pmid")
        ),
        "publication_title": text_value(
            row.get("publication_title")
        ),
    }


def cache_file_for(
    row: dict[str, Any],
    cache_dir: Path,
) -> Path:
    key = publication_key(row)

    digest = hashlib.sha1(
        key.encode("utf-8")
    ).hexdigest()[:16]

    slug = safe_slug(
        key,
        max_len=80,
    )

    return cache_dir / f"{slug}__{digest}.json"


def read_cache(
    row: dict[str, Any],
    cache_dir: Path,
) -> dict[str, Any] | None:
    path = cache_file_for(
        row,
        cache_dir,
    )

    if not path.is_file():
        return None

    try:
        record = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )
    except Exception:
        return None

    if (
        record.get("cache_schema_version")
        != CACHE_SCHEMA_VERSION
    ):
        return None

    identity = record.get(
        "publication_identity",
        {},
    )

    if (
        identity.get("publication_key")
        != publication_key(row)
    ):
        return None

    result = record.get("result")

    return result if isinstance(result, dict) else None


def write_cache(
    row: dict[str, Any],
    cache_dir: Path,
    result: dict[str, Any],
) -> None:
    cache_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    path = cache_file_for(
        row,
        cache_dir,
    )

    record = {
        "cache_schema_version": CACHE_SCHEMA_VERSION,
        "cached_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "publication_identity": publication_identity(row),
        "result": result,
    }

    tmp = path.with_suffix(
        path.suffix + ".tmp"
    )

    tmp.write_text(
        json.dumps(
            record,
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    tmp.replace(path)


def existing_pdf_result(
    row: dict[str, Any],
    pdf_dir: Path,
) -> dict[str, Any] | None:
    pdf_path = (
        pdf_dir
        / "by_publication"
        / publication_filename(row)
    )

    if not validate_pdf_path(
        pdf_path
    ):
        return None

    return {
        "pdf_status": "already_exists",
        "pdf_path": str(pdf_path),
        "pdf_url": "",
        "pdf_source": "existing_file",
        "pdf_error": "",
        "resolved_pmcid": text_value(
            row.get("publication_pmcid")
        ),
        "resolved_is_open_access": text_value(
            row.get("publication_is_open_access")
        ),
    }


def cache_is_usable(
    cached: dict[str, Any],
    *,
    retry_errors: bool,
) -> bool:
    status = text_value(
        cached.get("pdf_status")
    )

    if status not in TERMINAL_CACHE_STATUSES:
        return False

    if (
        retry_errors
        and status in RETRYABLE_ERROR_STATUSES
    ):
        return False

    if status in {
        "downloaded",
        "already_exists",
    }:
        path_text = text_value(
            cached.get("pdf_path")
        )

        if (
            not path_text
            or not validate_pdf_path(
                Path(path_text)
            )
        ):
            return False

    return True


def download_candidate(
    session: requests.Session,
    url: str,
    path: Path,
    timeout: float,
) -> tuple[bool, str]:
    try:
        response = session.get(
            url,
            timeout=timeout,
            allow_redirects=True,
            stream=True,
        )

        response.raise_for_status()

        path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        tmp = path.with_suffix(
            path.suffix + ".part"
        )

        tmp.unlink(
            missing_ok=True
        )

        first = b""
        size = 0

        try:
            with tmp.open("wb") as handle:
                for chunk in response.iter_content(
                    chunk_size=1024 * 128
                ):
                    if not chunk:
                        continue

                    if not first:
                        first = chunk[:1024]

                        if not first.lstrip().startswith(
                            b"%PDF-"
                        ):
                            tmp.unlink(
                                missing_ok=True
                            )

                            return (
                                False,
                                (
                                    "not_pdf "
                                    "content_type="
                                    f"{response.headers.get('Content-Type','')} "
                                    "final_url="
                                    f"{response.url}"
                                ),
                            )

                    handle.write(chunk)
                    size += len(chunk)

            if size < 512:
                tmp.unlink(
                    missing_ok=True
                )

                return (
                    False,
                    f"pdf_too_small bytes={size}",
                )

            tmp.replace(path)

        except Exception:
            tmp.unlink(
                missing_ok=True
            )
            raise

        if not validate_pdf_path(path):
            path.unlink(
                missing_ok=True
            )

            return (
                False,
                "invalid_pdf_after_write",
            )

        return True, ""

    except Exception as exc:
        return (
            False,
            f"{type(exc).__name__}: {exc}",
        )


def resolve_uncached(
    row: dict[str, str],
    *,
    pdf_dir: Path,
    unpaywall_email: str,
    user_agent: str,
    timeout: float,
) -> dict[str, Any]:
    session = session_for_thread(
        user_agent
    )

    doi = normalize_doi(
        row.get("publication_doi")
    )

    pmid = text_value(
        row.get("publication_pmid")
    )

    pdf_path = (
        pdf_dir
        / "by_publication"
        / publication_filename(row)
    )

    result = {
        "pdf_status": "",
        "pdf_path": str(pdf_path),
        "pdf_url": "",
        "pdf_source": "",
        "pdf_error": "",
        "resolved_pmcid": text_value(
            row.get("publication_pmcid")
        ),
        "resolved_is_open_access": text_value(
            row.get("publication_is_open_access")
        ),
    }

    # Handles PDFs downloaded by an older interrupted run.
    if validate_pdf_path(
        pdf_path
    ):
        result["pdf_status"] = "already_exists"
        result["pdf_source"] = "existing_file"
        return result

    if not doi and not pmid:
        result["pdf_status"] = (
            "no_publication_identifier"
        )
        result["pdf_error"] = (
            "No DOI or PMID available for OA lookup"
        )
        return result

    candidates: list[
        tuple[str, str]
    ] = []

    errors: list[str] = []

    try:
        epmc = europe_pmc_lookup(
            session,
            doi=doi,
            pmid=pmid,
            timeout=timeout,
        )
    except Exception as exc:
        epmc = None
        errors.append(
            "EuropePMC lookup: "
            f"{type(exc).__name__}: {exc}"
        )

    if epmc:
        result["resolved_pmcid"] = text_value(
            epmc.get("pmcid")
        )

        result[
            "resolved_is_open_access"
        ] = text_value(
            epmc.get("isOpenAccess")
        )

        candidates.extend(
            europe_pmc_pdf_urls(
                epmc
            )
        )

    if doi and unpaywall_email:
        unpaywall = unpaywall_lookup(
            session,
            doi,
            unpaywall_email,
            timeout=timeout,
        )

        if unpaywall:
            if unpaywall.get("is_oa") is True:
                result[
                    "resolved_is_open_access"
                ] = "Y"

            candidates.extend(
                unpaywall_pdf_urls(
                    unpaywall
                )
            )

    manifest_candidate = text_value(
        row.get(
            "publication_pdf_candidate_url"
        )
    )

    if manifest_candidate:
        candidates.insert(
            0,
            (
                "manifest_candidate",
                manifest_candidate,
            ),
        )

    seen = set()
    unique_candidates = []

    for source, url in candidates:
        if not url or url in seen:
            continue

        seen.add(url)
        unique_candidates.append(
            (source, url)
        )

    for source, url in unique_candidates:
        ok, error = download_candidate(
            session,
            url,
            pdf_path,
            timeout,
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
            return result

        errors.append(
            f"{source}: {error}"
        )

    if unique_candidates:
        result["pdf_status"] = "download_error"
    else:
        result["pdf_status"] = "no_open_access_pdf"

    result["pdf_error"] = (
        " | ".join(errors)[:4000]
    )

    return result


def resolve_and_cache(
    key: str,
    row: dict[str, str],
    *,
    pdf_dir: Path,
    cache_dir: Path,
    unpaywall_email: str,
    user_agent: str,
    timeout: float,
) -> tuple[str, dict[str, Any]]:
    try:
        result = resolve_uncached(
            row,
            pdf_dir=pdf_dir,
            unpaywall_email=unpaywall_email,
            user_agent=user_agent,
            timeout=timeout,
        )

    except Exception as exc:
        result = {
            "pdf_status": "worker_error",
            "pdf_path": "",
            "pdf_url": "",
            "pdf_source": "",
            "pdf_error": (
                f"{type(exc).__name__}: {exc}"
            ),
            "resolved_pmcid": "",
            "resolved_is_open_access": "",
        }

    # Write immediately, not at the end of the 25k-publication batch.
    write_cache(
        row,
        cache_dir,
        result,
    )

    return key, result


def main():
    args = parse_args()

    rows = read_tsv(
        Path(args.input_tsv)
    )

    pdf_dir = Path(
        args.pdf_dir
    )

    cache_dir = (
        Path(args.cache_dir)
        if args.cache_dir
        else pdf_dir / ".resolution_cache"
    )

    output = Path(
        args.output
    )

    publication_rows = [
        row
        for row in rows
        if row.get("publication_status")
        == "publication_found"
    ]

    grouped: dict[
        str,
        list[int],
    ] = {}

    for index, row in enumerate(
        publication_rows
    ):
        grouped.setdefault(
            publication_key(row),
            [],
        ).append(index)

    representative = {
        key: publication_rows[
            indexes[0]
        ]
        for key, indexes
        in grouped.items()
    }

    print(
        "Publication rows: "
        f"{len(publication_rows):,}"
    )
    print(
        "Unique publications: "
        f"{len(representative):,}"
    )
    print(
        f"Workers: {args.workers}"
    )
    print(
        f"Resolution cache: {cache_dir}"
    )

    resolved: dict[
        str,
        dict[str, Any],
    ] = {}

    to_resolve: dict[
        str,
        dict[str, str],
    ] = {}

    existing_pdf_hits = 0
    cache_hits = 0
    retrying_errors = 0
    stale_cache = 0

    for key, row in representative.items():

        # First, recover any successful downloads made by the previous
        # non-caching version.
        existing = existing_pdf_result(
            row,
            pdf_dir,
        )

        if existing is not None:
            resolved[key] = existing
            existing_pdf_hits += 1

            # Seed the new persistent cache from that existing file.
            write_cache(
                row,
                cache_dir,
                existing,
            )
            continue

        if not args.force:
            cached = read_cache(
                row,
                cache_dir,
            )

            if cached is not None:
                cached_status = text_value(
                    cached.get(
                        "pdf_status"
                    )
                )

                if (
                    args.retry_errors
                    and cached_status
                    in RETRYABLE_ERROR_STATUSES
                ):
                    retrying_errors += 1

                elif cache_is_usable(
                    cached,
                    retry_errors=args.retry_errors,
                ):
                    resolved[key] = cached
                    cache_hits += 1
                    continue

                else:
                    stale_cache += 1

        to_resolve[key] = row

    print(
        "Existing valid PDFs reused: "
        f"{existing_pdf_hits:,}"
    )
    print(
        "Cached resolution results reused: "
        f"{cache_hits:,}"
    )

    if retrying_errors:
        print(
            "Cached errors selected for retry: "
            f"{retrying_errors:,}"
        )

    if stale_cache:
        print(
            "Stale/invalid cache entries ignored: "
            f"{stale_cache:,}"
        )

    print(
        "Publications requiring network resolution: "
        f"{len(to_resolve):,}"
    )

    if to_resolve:
        with ThreadPoolExecutor(
            max_workers=max(
                1,
                args.workers,
            )
        ) as executor:

            future_map = {
                executor.submit(
                    resolve_and_cache,
                    key,
                    row,
                    pdf_dir=pdf_dir,
                    cache_dir=cache_dir,
                    unpaywall_email=(
                        args.unpaywall_email
                    ),
                    user_agent=args.user_agent,
                    timeout=args.timeout,
                ): key
                for key, row
                in to_resolve.items()
            }

            completed = 0
            total = len(
                to_resolve
            )

            for future in as_completed(
                future_map
            ):
                key = future_map[
                    future
                ]

                try:
                    (
                        resolved_key,
                        result,
                    ) = future.result()

                    resolved[
                        resolved_key
                    ] = result

                except Exception as exc:
                    row = to_resolve[
                        key
                    ]

                    result = {
                        "pdf_status": "worker_error",
                        "pdf_path": "",
                        "pdf_url": "",
                        "pdf_source": "",
                        "pdf_error": (
                            f"{type(exc).__name__}: {exc}"
                        ),
                        "resolved_pmcid": "",
                        "resolved_is_open_access": "",
                    }

                    resolved[key] = result

                    write_cache(
                        row,
                        cache_dir,
                        result,
                    )

                completed += 1

                if (
                    completed % 100 == 0
                    or completed == total
                ):
                    print(
                        "Resolved this run "
                        f"{completed:,}/{total:,} "
                        "(overall "
                        f"{len(resolved):,}/"
                        f"{len(representative):,})"
                    )

    output_rows = []

    for row in rows:
        out_row = dict(row)

        if (
            row.get("publication_status")
            == "publication_found"
        ):
            out_row.update(
                resolved.get(
                    publication_key(row),
                    {
                        "pdf_status": (
                            "internal_missing_result"
                        ),
                        "pdf_path": "",
                        "pdf_url": "",
                        "pdf_source": "",
                        "pdf_error": (
                            "No resolution result was produced"
                        ),
                        "resolved_pmcid": "",
                        "resolved_is_open_access": "",
                    },
                )
            )

        else:
            out_row.update(
                {
                    "pdf_status": "not_applicable",
                    "pdf_path": "",
                    "pdf_url": "",
                    "pdf_source": "",
                    "pdf_error": "",
                    "resolved_pmcid": "",
                    "resolved_is_open_access": "",
                }
            )

        output_rows.append(
            out_row
        )

    extra = [
        "pdf_status",
        "pdf_path",
        "pdf_url",
        "pdf_source",
        "pdf_error",
        "resolved_pmcid",
        "resolved_is_open_access",
    ]

    if rows:
        fieldnames = list(
            rows[0].keys()
        ) + [
            name
            for name in extra
            if name not in rows[0]
        ]
    else:
        fieldnames = extra

    write_tsv(
        output,
        output_rows,
        fieldnames,
    )

    counts: dict[
        str,
        int,
    ] = {}

    for row in output_rows:
        status = row.get(
            "pdf_status",
            "",
        )

        counts[status] = (
            counts.get(
                status,
                0,
            )
            + 1
        )

    print(
        f"Saved: {output}"
    )

    for status, count in sorted(
        counts.items()
    ):
        print(
            f"{status:24s} "
            f"{count:8,d}"
        )


if __name__ == "__main__":
    main()
