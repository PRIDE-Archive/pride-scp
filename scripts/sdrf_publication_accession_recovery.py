#!/usr/bin/env python3
"""Recover publication links for residual PRIDE SCP datasets by reverse accession lookup.

This is a bounded, non-generative source-recovery utility.  It addresses a concrete failure mode
observed in SDRF v0.4.7: several residual multiplex datasets have no usable publication text in the
local PRIDE-derived manifest even though their publications are publicly available in Europe PMC.

The recovery contract is deliberately conservative:

* query Europe PMC by the exact PXD accession;
* require the exact accession to occur in the candidate article's PMC full text;
* require the article to be compatible with the current PRIDE project title/description, or to match
  a publication DOI/PMID currently returned by PRIDE;
* never attach a full-text article solely because it contains the accession.

The last rule protects against corrected or erroneous accession citations.  In particular, a paper
that mentions a PXD accession but is semantically unrelated to the PRIDE project is retained as a
rejected/review diagnostic instead of becoming SDRF field evidence.

GT/reference metadata is never read and never supplies publication or SDRF values.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
STAGES = ROOT / "python" / "stages"
if str(STAGES) not in sys.path:
    sys.path.insert(0, str(STAGES))

from pride_scp_pipeline_common import (  # noqa: E402
    _europe_pmc_search,
    build_session,
    normalize_doi,
    text_value,
)

AUDITOR_VERSION = "pride-scp-sdrf-publication-accession-recovery-v0.1"
EUROPE_PMC_FULLTEXT = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"

STOPWORDS = {
    "about", "after", "against", "among", "analysis", "analyses", "approach", "based", "cell",
    "cells", "data", "dataset", "datasets", "during", "from", "into", "mass", "method", "methods",
    "multiple", "protein", "proteins", "proteome", "proteomic", "proteomics", "profiling", "single",
    "spectrometry", "study", "studies", "through", "using", "with", "without", "their", "this", "that",
    "the", "and", "for", "of", "in", "on", "to", "a", "an", "by", "at", "is", "are",
}

RECOVERY_FIELDS = [
    "accession",
    "project_title",
    "project_description",
    "candidate_rank",
    "recovery_status",
    "recovery_reason",
    "exact_accession_in_fulltext",
    "current_pride_publication_match",
    "shared_title_tokens",
    "title_overlap_coefficient",
    "candidate_title",
    "candidate_doi",
    "candidate_pmid",
    "candidate_pmcid",
    "candidate_journal",
    "candidate_year",
    "candidate_authors",
    "candidate_is_open_access",
    "candidate_has_pdf",
    "europe_pmc_query",
    "fulltext_url",
    "fulltext_error",
]

# Keep this compatible with Stage-01/Stage-03 publication manifests.  Stage-03 tolerates extra fields,
# but these are the fields it and downstream merging logic care about most.
PUBLICATION_FIELDS = [
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
    "publication_recovery_status",
    "publication_recovery_reason",
    "publication_recovery_title_overlap",
    "publication_recovery_exact_accession",
]


@dataclass
class Candidate:
    accession: str
    project_title: str
    project_description: str
    candidate_rank: int
    recovery_status: str
    recovery_reason: str
    exact_accession_in_fulltext: bool
    current_pride_publication_match: bool
    shared_title_tokens: list[str]
    title_overlap_coefficient: float
    candidate_title: str
    candidate_doi: str
    candidate_pmid: str
    candidate_pmcid: str
    candidate_journal: str
    candidate_year: str
    candidate_authors: str
    candidate_is_open_access: str
    candidate_has_pdf: str
    europe_pmc_query: str
    fulltext_url: str
    fulltext_error: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--accessions-file", required=False, default="")
    parser.add_argument("--pride-publications", required=False, default="")
    parser.add_argument("--output-dir", required=False, default="publication_accession_recovery")
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--page-size", type=int, default=25)
    parser.add_argument("--user-agent", default="PRIDE-SCP-publication-accession-recovery/0.1")
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args()


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(errors="replace") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def normalize_space(value: Any) -> str:
    return re.sub(r"\s+", " ", text_value(value)).strip()


def title_tokens(value: str) -> set[str]:
    tokens = re.findall(r"[a-z0-9]+", (value or "").lower())
    return {token for token in tokens if len(token) >= 3 and token not in STOPWORDS}


def title_compatibility(project_title: str, candidate_title: str) -> tuple[list[str], float]:
    project = title_tokens(project_title)
    candidate = title_tokens(candidate_title)
    shared = sorted(project & candidate)
    denom = min(len(project), len(candidate))
    overlap = (len(shared) / denom) if denom else 0.0
    return shared, overlap


def normalize_title(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (value or "").lower()))


def current_publication_ids(rows: list[dict[str, str]]) -> tuple[set[str], set[str]]:
    dois = {
        normalize_doi(row.get("publication_doi"))
        for row in rows
        if normalize_doi(row.get("publication_doi"))
        and normalize_space(row.get("publication_status")) == "publication_found"
    }
    pmids = {
        normalize_space(row.get("publication_pmid"))
        for row in rows
        if normalize_space(row.get("publication_pmid"))
        and normalize_space(row.get("publication_status")) == "publication_found"
    }
    return dois, pmids


def classify_candidate(
    *,
    project_title: str,
    candidate_title: str,
    candidate_doi: str,
    candidate_pmid: str,
    current_dois: set[str],
    current_pmids: set[str],
    exact_accession_in_fulltext: bool,
) -> tuple[str, str, bool, list[str], float]:
    shared, overlap = title_compatibility(project_title, candidate_title)
    pride_match = bool(
        (candidate_doi and candidate_doi in current_dois)
        or (candidate_pmid and candidate_pmid in current_pmids)
    )
    if not exact_accession_in_fulltext:
        return "rejected", "accession_not_verified_in_fulltext", pride_match, shared, overlap

    pnorm = normalize_title(project_title)
    cnorm = normalize_title(candidate_title)
    if pnorm and cnorm and (pnorm == cnorm or pnorm in cnorm or cnorm in pnorm):
        return "accepted", "exact_accession_plus_title_identity", pride_match, shared, overlap

    # Three distinctive shared title terms with substantial overlap is deliberately stricter than
    # generic semantic similarity.  It accepts the known multi-accession gastruloid and CHIMERYS
    # studies while rejecting accession mentions in biologically unrelated papers.
    if len(shared) >= 3 and overlap >= 0.30:
        reason = (
            "exact_accession_plus_current_pride_and_project_title_compatibility"
            if pride_match
            else "exact_accession_plus_project_title_compatibility"
        )
        return "accepted", reason, pride_match, shared, overlap

    # A current PRIDE DOI/PMID is corroborating evidence, not an override for a biologically
    # incompatible title.  This is crucial for corrected/erroneous accession associations.
    if pride_match and len(shared) >= 2 and overlap >= 0.20:
        return "accepted", "exact_accession_plus_current_pride_and_partial_title_compatibility", pride_match, shared, overlap

    if pride_match:
        return "rejected", "exact_accession_and_pride_match_but_project_title_mismatch", pride_match, shared, overlap

    if len(shared) >= 2 and overlap >= 0.20:
        return "review", "exact_accession_but_project_match_requires_review", pride_match, shared, overlap

    return "rejected", "exact_accession_but_project_title_mismatch", pride_match, shared, overlap


def xml_text(xml_bytes: bytes) -> str:
    root = ET.fromstring(xml_bytes)
    return normalize_space(" ".join(root.itertext()))


def fetch_fulltext(session, pmcid: str, accession: str, timeout: float) -> tuple[bool, str, str]:
    pmcid = normalize_space(pmcid)
    if not pmcid:
        return False, "", "candidate_has_no_pmcid"
    if not pmcid.upper().startswith("PMC"):
        pmcid = "PMC" + pmcid
    url = EUROPE_PMC_FULLTEXT.format(pmcid=pmcid)
    try:
        response = session.get(
            url,
            timeout=timeout,
            allow_redirects=True,
            headers={"Accept": "application/xml,text/xml,*/*;q=0.5"},
        )
        response.raise_for_status()
        text = xml_text(response.content)
        exact = bool(re.search(rf"(?<![A-Z0-9]){re.escape(accession)}(?![A-Z0-9])", text, re.I))
        return exact, url, ""
    except Exception as exc:  # network diagnostics belong in the audit output
        return False, url, f"{type(exc).__name__}: {exc}"


def project_metadata(rows: list[dict[str, str]]) -> dict[str, str]:
    if not rows:
        return {}
    preferred = next(
        (r for r in rows if normalize_space(r.get("project_fetch_status")) == "ok"),
        rows[0],
    )
    return dict(preferred)


def candidate_to_publication_row(candidate: Candidate, project: dict[str, str]) -> dict[str, str]:
    row = {field: normalize_space(project.get(field)) for field in PUBLICATION_FIELDS}
    row.update(
        {
            "accession": candidate.accession,
            "dataset_title": normalize_space(project.get("dataset_title")) or candidate.project_title,
            "dataset_description": normalize_space(project.get("dataset_description")) or candidate.project_description,
            "publication_count": "1",
            "publication_index": "1",
            "publication_status": "publication_found",
            "publication_source": "europe_pmc_accession_reverse_lookup",
            "publication_doi": candidate.candidate_doi,
            "publication_pmid": candidate.candidate_pmid,
            "publication_pmcid": candidate.candidate_pmcid,
            "publication_title": candidate.candidate_title,
            "publication_authors": candidate.candidate_authors,
            "publication_journal": candidate.candidate_journal,
            "publication_year": candidate.candidate_year,
            "publication_url": (
                f"https://europepmc.org/articles/{candidate.candidate_pmcid}"
                if candidate.candidate_pmcid
                else ""
            ),
            "publication_is_open_access": candidate.candidate_is_open_access,
            "publication_has_pdf": candidate.candidate_has_pdf,
            "publication_recovery_status": candidate.recovery_status,
            "publication_recovery_reason": candidate.recovery_reason,
            "publication_recovery_title_overlap": f"{candidate.title_overlap_coefficient:.4f}",
            "publication_recovery_exact_accession": "true" if candidate.exact_accession_in_fulltext else "false",
        }
    )
    return row


def recover_accession(
    *,
    accession: str,
    pride_rows: list[dict[str, str]],
    session,
    timeout: float,
    page_size: int,
) -> tuple[list[Candidate], list[dict[str, str]]]:
    project = project_metadata(pride_rows)
    project_title = normalize_space(project.get("dataset_title"))
    project_description = normalize_space(project.get("dataset_description"))
    current_dois, current_pmids = current_publication_ids(pride_rows)
    query = accession
    results = _europe_pmc_search(session, query, timeout=timeout, page_size=page_size)

    candidates: list[Candidate] = []
    for rank, result in enumerate(results, start=1):
        title = normalize_space(result.get("title"))
        doi = normalize_doi(result.get("doi"))
        pmid = normalize_space(result.get("pmid"))
        pmcid = normalize_space(result.get("pmcid"))
        exact, fulltext_url, fulltext_error = fetch_fulltext(session, pmcid, accession, timeout)
        status, reason, pride_match, shared, overlap = classify_candidate(
            project_title=project_title,
            candidate_title=title,
            candidate_doi=doi,
            candidate_pmid=pmid,
            current_dois=current_dois,
            current_pmids=current_pmids,
            exact_accession_in_fulltext=exact,
        )
        candidates.append(
            Candidate(
                accession=accession,
                project_title=project_title,
                project_description=project_description,
                candidate_rank=rank,
                recovery_status=status,
                recovery_reason=reason,
                exact_accession_in_fulltext=exact,
                current_pride_publication_match=pride_match,
                shared_title_tokens=shared,
                title_overlap_coefficient=overlap,
                candidate_title=title,
                candidate_doi=doi,
                candidate_pmid=pmid,
                candidate_pmcid=pmcid,
                candidate_journal=normalize_space(result.get("journalTitle")),
                candidate_year=normalize_space(result.get("pubYear")),
                candidate_authors=normalize_space(result.get("authorString")),
                candidate_is_open_access=normalize_space(result.get("isOpenAccess")),
                candidate_has_pdf=normalize_space(result.get("hasPDF")),
                europe_pmc_query=query,
                fulltext_url=fulltext_url,
                fulltext_error=fulltext_error,
            )
        )

    # Keep the strongest accepted source per accession.  Multiple compatible papers can mention a
    # reused dataset; choosing only the strongest title/current-PRIDE match prevents downstream
    # source packets from drifting into secondary reanalysis papers.
    accepted = [c for c in candidates if c.recovery_status == "accepted"]
    accepted.sort(
        key=lambda c: (
            1 if c.current_pride_publication_match else 0,
            c.title_overlap_coefficient,
            len(c.shared_title_tokens),
            -c.candidate_rank,
        ),
        reverse=True,
    )
    recovered_rows = [candidate_to_publication_row(accepted[0], project)] if accepted else []
    return candidates, recovered_rows


def run_self_test() -> None:
    # Exact-title recovery.
    status, reason, _, shared, overlap = classify_candidate(
        project_title="Real-Time Search Assisted Acquisition on a Tribrid Mass Spectrometer Improves Coverage in Multiplexed Single-Cell Proteomics",
        candidate_title="Real-Time Search-Assisted Acquisition on a Tribrid Mass Spectrometer Improves Coverage in Multiplexed Single-Cell Proteomics",
        candidate_doi="10.1016/j.mcpro.2022.100219",
        candidate_pmid="",
        current_dois=set(),
        current_pmids=set(),
        exact_accession_in_fulltext=True,
    )
    assert status == "accepted", (status, reason, shared, overlap)

    # Multi-accession project/publication title compatibility.
    status, reason, _, shared, overlap = classify_candidate(
        project_title="Deciphering lineage specification during early embryogenesis using multi-layered proteomics",
        candidate_title="Deciphering lineage specification during early embryogenesis in mouse gastruloids using multilayered proteomics",
        candidate_doi="",
        candidate_pmid="",
        current_dois=set(),
        current_pmids=set(),
        exact_accession_in_fulltext=True,
    )
    assert status == "accepted", (status, reason, shared, overlap)

    # PXD069039-style corrected/erroneous accession citation: exact accession alone is insufficient.
    status, reason, _, shared, overlap = classify_candidate(
        project_title="Multiplexed quantitation of post-translational modified peptides in single cells using triggered MS/MS combined with super heavy tandem mass tags",
        candidate_title="With or without a Ca2+ signal? a proteomics approach toward Ca2+-dependent and -independent changes in response to oxidative stress in Arabidopsis thaliana",
        candidate_doi="10.1007/s00425-025-04891-y",
        candidate_pmid="",
        current_dois=set(),
        current_pmids=set(),
        exact_accession_in_fulltext=True,
    )
    assert status == "rejected" and "mismatch" in reason, (status, reason, shared, overlap)

    # Even a currently attached PRIDE DOI cannot override a strong biological/title mismatch.
    status, reason, pride_match, shared, overlap = classify_candidate(
        project_title="Multiplexed quantitation of post-translational modified peptides in single cells using triggered MS/MS combined with super heavy tandem mass tags",
        candidate_title="With or without a Ca2+ signal? a proteomics approach toward Ca2+-dependent and -independent changes in response to oxidative stress in Arabidopsis thaliana",
        candidate_doi="10.1007/s00425-025-04891-y",
        candidate_pmid="41348131",
        current_dois={"10.1007/s00425-025-04891-y"},
        current_pmids={"41348131"},
        exact_accession_in_fulltext=True,
    )
    assert status == "rejected" and pride_match and "mismatch" in reason, (status, reason, shared, overlap)

    # A current PRIDE publication identifier can rescue a semantically broader article title, but
    # only after exact full-text accession verification.
    status, reason, pride_match, _, _ = classify_candidate(
        project_title="Benchmarking CHIMERYS using Wide Window Acquisition and Classical Narrow Isolation Window Data Dependent Acquisition",
        candidate_title="Micropillar arrays, wide window acquisition and AI-based data analysis improve comprehensiveness in multiple proteomic applications",
        candidate_doi="10.1038/s41467-024-45391-z",
        candidate_pmid="",
        current_dois={"10.1038/s41467-024-45391-z"},
        current_pmids=set(),
        exact_accession_in_fulltext=True,
    )
    assert status == "accepted" and pride_match and "pride" in reason, (status, reason)

    status, reason, _, _, _ = classify_candidate(
        project_title="Anything",
        candidate_title="Anything",
        candidate_doi="",
        candidate_pmid="",
        current_dois=set(),
        current_pmids=set(),
        exact_accession_in_fulltext=False,
    )
    assert status == "rejected" and reason == "accession_not_verified_in_fulltext"
    print("sdrf_publication_accession_recovery self-test: PASS")


def main() -> None:
    args = parse_args()
    if args.self_test:
        run_self_test()
        return
    if not args.accessions_file or not args.pride_publications:
        raise SystemExit("--accessions-file and --pride-publications are required unless --self-test is used")

    accessions = [
        line.strip().upper()
        for line in Path(args.accessions_file).read_text().splitlines()
        if line.strip()
    ]
    pride_rows = read_tsv(Path(args.pride_publications))
    by_accession: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in pride_rows:
        by_accession[normalize_space(row.get("accession")).upper()].append(row)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    session = build_session(user_agent=args.user_agent)

    all_candidates: list[Candidate] = []
    recovered: list[dict[str, str]] = []
    per_accession: dict[str, dict[str, Any]] = {}
    for accession in accessions:
        candidates, rows = recover_accession(
            accession=accession,
            pride_rows=by_accession.get(accession, []),
            session=session,
            timeout=args.timeout,
            page_size=args.page_size,
        )
        all_candidates.extend(candidates)
        recovered.extend(rows)
        accepted = [c for c in candidates if c.recovery_status == "accepted"]
        review = [c for c in candidates if c.recovery_status == "review"]
        rejected = [c for c in candidates if c.recovery_status == "rejected"]
        per_accession[accession] = {
            "search_results": len(candidates),
            "accepted_candidates": len(accepted),
            "review_candidates": len(review),
            "rejected_candidates": len(rejected),
            "recovered_publication": bool(rows),
            "best_status": (
                "accepted" if accepted else "review" if review else "rejected_or_no_candidate"
            ),
            "best_title": (
                accepted[0].candidate_title if accepted else review[0].candidate_title if review else ""
            ),
            "best_doi": (
                accepted[0].candidate_doi if accepted else review[0].candidate_doi if review else ""
            ),
        }

    candidate_rows = []
    for candidate in all_candidates:
        row = asdict(candidate)
        row["shared_title_tokens"] = ",".join(candidate.shared_title_tokens)
        row["title_overlap_coefficient"] = f"{candidate.title_overlap_coefficient:.4f}"
        row["exact_accession_in_fulltext"] = "true" if candidate.exact_accession_in_fulltext else "false"
        row["current_pride_publication_match"] = "true" if candidate.current_pride_publication_match else "false"
        candidate_rows.append(row)

    candidates_path = out_dir / "publication_accession_recovery_candidates.tsv"
    recovered_path = out_dir / "recovered_publications.tsv"
    summary_path = out_dir / "publication_accession_recovery_summary.json"
    write_tsv(candidates_path, candidate_rows, RECOVERY_FIELDS)
    write_tsv(recovered_path, recovered, PUBLICATION_FIELDS)

    summary = {
        "auditor_version": AUDITOR_VERSION,
        "accessions": len(accessions),
        "recovered_publication_accessions": sum(1 for value in per_accession.values() if value["recovered_publication"]),
        "unresolved_accessions": [acc for acc in accessions if not per_accession[acc]["recovered_publication"]],
        "per_accession": per_accession,
        "non_generative": True,
        "gt_metadata_used": False,
        "outputs": {
            "candidates": str(candidates_path.resolve()),
            "recovered_publications": str(recovered_path.resolve()),
        },
    }
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
