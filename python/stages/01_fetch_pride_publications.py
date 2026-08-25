#!/usr/bin/env python3
"""
Stage 1: enumerate PRIDE projects and extract associated publications.

Outputs one TSV row per PRIDE-accession/publication pair. Projects without an
associated publication are retained with publication_status=no_publication.

The script caches raw PRIDE project JSON and is safe to resume.
"""

from __future__ import annotations

import argparse
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests

from pride_scp_pipeline_common import (
    DOI_RE,
    PXD_RE,
    build_session,
    choose_crossref_date,
    crossref_lookup,
    doi_from_text,
    europe_pmc_lookup,
    first_value,
    flatten_named_values,
    get_json,
    join_unique,
    normalize_doi,
    pmid_from_text,
    recursive_find_values,
    text_value,
    unique_nonempty,
    write_tsv,
)


PRIDE_API = "https://www.ebi.ac.uk/pride/ws/archive/v3"
PRIDE_PROJECT_PAGE = "https://www.ebi.ac.uk/pride/archive/projects/{accession}"
PRIDE_PROJECT_API = PRIDE_API + "/projects/{accession}"
PRIDE_ALL = PRIDE_API + "/projects/all"


FIELDNAMES = [
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
]


_thread_local = threading.local()


def session_for_thread(user_agent: str):
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = build_session(user_agent=user_agent)
        _thread_local.session = session
    return session


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        default="pride_publications.tsv",
        help="Output TSV (default: pride_publications.tsv)",
    )
    parser.add_argument(
        "--cache-dir",
        default=".pride_catalogue_cache",
        help="Raw PRIDE JSON cache directory.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Parallel PRIDE project-detail requests (default: 4).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=45.0,
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Only process the first N accessions; useful for testing.",
    )
    parser.add_argument(
        "--accessions-file",
        default="",
        help="Optional text file containing one PXD accession per line.",
    )
    parser.add_argument(
        "--include-rxd",
        action="store_true",
        help="Include RXD projects in addition to PXD.",
    )
    parser.add_argument(
        "--no-enrich-publications",
        action="store_true",
        help="Do not enrich publication metadata using Europe PMC/Crossref.",
    )
    parser.add_argument(
        "--contact-email",
        default="",
        help="Optional contact email for the Crossref polite pool.",
    )
    parser.add_argument(
        "--user-agent",
        default="PRIDE-SCP-catalogue/1.0",
    )
    return parser.parse_args()


def extract_all_entries(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]

    if not isinstance(data, dict):
        return []

    candidates = [
        data.get("projects"),
        data.get("content"),
        data.get("_embedded", {}).get("projects")
        if isinstance(data.get("_embedded"), dict)
        else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, list):
            return [x for x in candidate if isinstance(x, dict)]

    return []


def extract_ftp(project: dict[str, Any], accession: str) -> str:
    for key in (
        "ftp",
        "ftpUrl",
        "ftpURL",
        "projectFtpUrl",
        "projectFTP",
    ):
        values = recursive_find_values(project, {key})
        for value in values:
            value = text_value(value)
            if value.startswith(("ftp://", "https://", "http://")):
                return value

    publication_date = text_value(
        first_value(project, "publicationDate", "publication_date")
    )
    match = re.match(r"(\d{4})-(\d{2})", publication_date)
    if match:
        year, month = match.groups()
        return (
            "https://ftp.pride.ebi.ac.uk/pride/data/archive/"
            f"{year}/{month}/{accession}/"
        )

    return ""


def publication_from_mapping(
    mapping: dict[str, Any],
    source: str,
) -> dict[str, str]:
    citation = text_value(
        first_value(
            mapping,
            "referenceLine",
            "reference",
            "citation",
            "referenceText",
            "description",
        )
    )
    doi = normalize_doi(
        first_value(
            mapping,
            "doi",
            "DOI",
            "publicationDoi",
            "publicationDOI",
        )
    ) or doi_from_text(citation)

    pmid = text_value(
        first_value(
            mapping,
            "pubmedId",
            "pubMedId",
            "pmid",
            "PMID",
        )
    ) or pmid_from_text(citation)

    title = text_value(
        first_value(
            mapping,
            "title",
            "publicationTitle",
            "articleTitle",
        )
    )

    url = text_value(
        first_value(
            mapping,
            "url",
            "publicationUrl",
            "link",
        )
    )

    return {
        "publication_source": source,
        "publication_doi": doi,
        "publication_pmid": pmid,
        "publication_title": title,
        "publication_url": url,
        "publication_citation": citation,
    }


def extract_publications(project: dict[str, Any]) -> list[dict[str, str]]:
    publications: list[dict[str, str]] = []

    for key in ("references", "referenceList", "publications", "publication"):
        value = project.get(key)
        if value is None:
            continue

        if isinstance(value, dict):
            # Some APIs wrap lists as {"reference": [...]}.
            nested = None
            for nested_key in ("reference", "references", "publication", "publications"):
                if isinstance(value.get(nested_key), list):
                    nested = value[nested_key]
                    break
            if nested is None:
                nested = [value]
        elif isinstance(value, list):
            nested = value
        else:
            nested = [value]

        for item in nested:
            if isinstance(item, dict):
                pub = publication_from_mapping(item, f"project.{key}")
            else:
                citation = text_value(item)
                pub = {
                    "publication_source": f"project.{key}",
                    "publication_doi": doi_from_text(citation),
                    "publication_pmid": pmid_from_text(citation),
                    "publication_title": "",
                    "publication_url": "",
                    "publication_citation": citation,
                }

            if any(
                pub.get(k)
                for k in (
                    "publication_doi",
                    "publication_pmid",
                    "publication_title",
                    "publication_citation",
                )
            ):
                pub_doi = normalize_doi(
                    pub.get("publication_doi")
                )

                if re.fullmatch(
                    r"10\.6019/(?:pxd|rxd)\d+",
                    pub_doi,
                    re.I,
                ):
                    # Dataset DOI, not a publication.
                    continue

                publications.append(pub)


    # IMPORTANT: project["doi"] is the ProteomeXchange/PRIDE DATASET DOI
    # (for example 10.6019/PXD000001), not a literature publication DOI.
    # It is already retained separately as `project_doi` by project_base_row()
    # and must never become a publication row or be sent to the PDF resolver.

    # De-duplicate. DOI wins, then PMID; only identifier-less records fall
    # back to title/citation. This prevents a structured PRIDE reference and
    # the project-level DOI field from creating duplicate rows for one paper.
    seen = set()
    deduped = []

    for pub in publications:
        doi = normalize_doi(pub.get("publication_doi"))
        pmid = text_value(pub.get("publication_pmid"))
        title = text_value(pub.get("publication_title")).lower()
        citation = text_value(pub.get("publication_citation")).lower()

        if doi:
            key = ("doi", doi)
        elif pmid:
            key = ("pmid", pmid)
        elif title:
            key = ("title", title)
        else:
            key = ("citation", citation)

        if key in seen:
            continue

        seen.add(key)
        deduped.append(pub)

    return deduped


def enrich_publication(
    pub: dict[str, str],
    *,
    session: requests.Session,
    contact_email: str,
    timeout: float,
) -> dict[str, str]:
    out = dict(pub)
    doi = normalize_doi(out.get("publication_doi"))
    pmid = text_value(out.get("publication_pmid"))

    epmc = None
    try:
        epmc = europe_pmc_lookup(
            session,
            doi=doi,
            pmid=pmid,
            timeout=timeout,
        )
    except requests.RequestException:
        epmc = None

    if epmc:
        out["publication_doi"] = normalize_doi(epmc.get("doi")) or doi
        out["publication_pmid"] = text_value(epmc.get("pmid")) or pmid
        out["publication_pmcid"] = text_value(epmc.get("pmcid"))
        out["publication_title"] = (
            text_value(epmc.get("title"))
            or out.get("publication_title", "")
        )
        out["publication_authors"] = text_value(epmc.get("authorString"))
        out["publication_year"] = text_value(epmc.get("pubYear"))
        out["publication_is_open_access"] = text_value(epmc.get("isOpenAccess"))
        out["publication_has_pdf"] = text_value(epmc.get("hasPDF"))

        journal = text_value(epmc.get("journalTitle"))
        if not journal:
            jinfo = epmc.get("journalInfo")
            if isinstance(jinfo, dict):
                journal_obj = jinfo.get("journal")
                if isinstance(journal_obj, dict):
                    journal = text_value(journal_obj.get("title"))
        out["publication_journal"] = journal

        ft = epmc.get("fullTextUrlList", {}).get("fullTextUrl", []) or []
        if isinstance(ft, dict):
            ft = [ft]
        for item in ft:
            if (
                isinstance(item, dict)
                and text_value(item.get("documentStyle")).lower() == "pdf"
                and text_value(item.get("url"))
            ):
                out["publication_pdf_candidate_url"] = text_value(item.get("url"))
                break

    if doi:
        crossref = crossref_lookup(
            session,
            doi,
            mailto=contact_email,
            timeout=timeout,
        )
        if crossref:
            titles = crossref.get("title") or []
            containers = crossref.get("container-title") or []
            if not out.get("publication_title") and titles:
                out["publication_title"] = text_value(titles[0])
            if not out.get("publication_journal") and containers:
                out["publication_journal"] = text_value(containers[0])
            if not out.get("publication_year"):
                date = choose_crossref_date(crossref)
                out["publication_year"] = date[:4] if date else ""

    doi = normalize_doi(out.get("publication_doi"))
    pmid = text_value(out.get("publication_pmid"))
    pmcid = text_value(out.get("publication_pmcid"))

    if doi:
        out["publication_url"] = f"https://doi.org/{doi}"
    elif pmcid:
        out["publication_url"] = f"https://europepmc.org/articles/{pmcid}"
    elif pmid:
        out["publication_url"] = f"https://europepmc.org/article/MED/{pmid}"

    return out


def project_base_row(project: dict[str, Any], accession: str) -> dict[str, Any]:
    def names(*keys):
        vals = []
        for key in keys:
            if key in project:
                vals.extend(flatten_named_values(project.get(key)))
        return join_unique(vals)

    return {
        "accession": accession,
        "pride_project_url": PRIDE_PROJECT_PAGE.format(accession=accession),
        "pride_api_url": PRIDE_PROJECT_API.format(accession=accession),
        "pride_ftp_url": extract_ftp(project, accession),
        "dataset_title": text_value(project.get("title")),
        "dataset_description": text_value(
            first_value(project, "projectDescription", "description")
        ),
        "submission_date": text_value(
            first_value(project, "submissionDate", "submission_date")
        ),
        "publication_date": text_value(
            first_value(project, "publicationDate", "publication_date")
        ),
        "organisms": names("organisms"),
        "instruments": names("instruments"),
        "software": names("software", "softwares"),
        "experiment_types": names("experimentTypes", "experimentType"),
        "quantification_methods": names(
            "quantificationMethods",
            "quantificationMethod",
        ),
        "project_doi": normalize_doi(project.get("doi")),
    }


def process_project(
    accession: str,
    *,
    cache_dir: Path,
    user_agent: str,
    timeout: float,
    enrich: bool,
    contact_email: str,
) -> list[dict[str, Any]]:
    cache_file = cache_dir / "projects" / f"{accession}.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)

    project = None
    error = ""

    try:
        if cache_file.exists():
            project = json.loads(cache_file.read_text(encoding="utf-8"))
        else:
            session = session_for_thread(user_agent)
            project = get_json(
                session,
                PRIDE_PROJECT_API.format(accession=accession),
                timeout=timeout,
            )
            cache_file.write_text(
                json.dumps(project, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    if not isinstance(project, dict):
        return [
            {
                "accession": accession,
                "pride_project_url": PRIDE_PROJECT_PAGE.format(accession=accession),
                "pride_api_url": PRIDE_PROJECT_API.format(accession=accession),
                "publication_count": 0,
                "publication_index": 0,
                "publication_status": "project_fetch_error",
                "project_fetch_status": "error",
                "project_fetch_error": error,
            }
        ]

    base = project_base_row(project, accession)
    publications = extract_publications(project)

    if not publications:
        return [
            {
                **base,
                "publication_count": 0,
                "publication_index": 0,
                "publication_status": "no_publication",
                "project_fetch_status": "ok",
                "project_fetch_error": "",
            }
        ]

    rows = []
    session = session_for_thread(user_agent)

    for index, pub in enumerate(publications, start=1):
        if enrich:
            pub = enrich_publication(
                pub,
                session=session,
                contact_email=contact_email,
                timeout=timeout,
            )

        rows.append(
            {
                **base,
                **pub,
                "publication_count": len(publications),
                "publication_index": index,
                "publication_status": "publication_found",
                "project_fetch_status": "ok",
                "project_fetch_error": "",
            }
        )

    return rows


def main():
    args = parse_args()
    output = Path(args.output)
    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    if args.accessions_file:
        accessions = [
            line.strip().upper()
            for line in Path(args.accessions_file).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    else:
        all_cache = cache_dir / "projects_all.json"
        session = build_session(user_agent=args.user_agent)
        if all_cache.exists():
            data = json.loads(all_cache.read_text(encoding="utf-8"))
        else:
            data = get_json(session, PRIDE_ALL, timeout=args.timeout)
            all_cache.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

        entries = extract_all_entries(data)
        accessions = []
        for entry in entries:
            accession = text_value(entry.get("accession")).upper()
            if re.fullmatch(r"PXD\d{6,}", accession):
                accessions.append(accession)
            elif args.include_rxd and re.fullmatch(r"RXD\d{6,}", accession):
                accessions.append(accession)

    accessions = sorted(set(accessions))

    if args.limit > 0:
        accessions = accessions[: args.limit]

    print(f"Projects to process: {len(accessions):,}")
    print(f"Cache: {cache_dir}")
    print(f"Workers: {args.workers}")

    all_rows: list[dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(
                process_project,
                accession,
                cache_dir=cache_dir,
                user_agent=args.user_agent,
                timeout=args.timeout,
                enrich=not args.no_enrich_publications,
                contact_email=args.contact_email,
            ): accession
            for accession in accessions
        }

        completed = 0
        for future in as_completed(futures):
            accession = futures[future]
            try:
                rows = future.result()
            except Exception as exc:
                rows = [
                    {
                        "accession": accession,
                        "publication_status": "worker_error",
                        "project_fetch_status": "error",
                        "project_fetch_error": f"{type(exc).__name__}: {exc}",
                    }
                ]
            all_rows.extend(rows)
            completed += 1
            if completed % 100 == 0 or completed == len(accessions):
                print(f"Completed {completed:,}/{len(accessions):,}")

    all_rows.sort(
        key=lambda row: (
            text_value(row.get("accession")),
            int(text_value(row.get("publication_index")) or 0),
        )
    )

    write_tsv(output, all_rows, FIELDNAMES)

    n_pub = sum(1 for row in all_rows if row.get("publication_status") == "publication_found")
    n_no_pub = sum(1 for row in all_rows if row.get("publication_status") == "no_publication")
    n_err = sum(1 for row in all_rows if row.get("project_fetch_status") == "error")

    print(f"Saved: {output}")
    print(f"Publication rows: {n_pub:,}")
    print(f"Projects without publication metadata: {n_no_pub:,}")
    print(f"Project fetch errors: {n_err:,}")


if __name__ == "__main__":
    main()
