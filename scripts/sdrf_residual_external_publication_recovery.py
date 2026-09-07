#!/usr/bin/env python3
"""Recover publications only for active PRIDE-SCP residuals that lack local content.

This is a bounded, non-generative external source-recovery stage that follows the v0.4.9
local-first corpus reconciliation.  It never re-searches accessions with already selected local
publication text.  For the supplied residual queue it tries, in order:

1. Europe PMC lookup by an already-known publication DOI/PMID/title;
2. Europe PMC reverse search by exact PXD accession;
3. Europe PMC title search as a review-only fallback.

Candidates are accepted only when publication identity is source-grounded: an exact PXD in PMC full
text plus project-title compatibility, or a non-quarantined current publication identifier plus
compatible title.  Exact PXD mentions with strong project mismatch are rejected.  Quarantined DOI,
PMID, PMCID and title identities are hard blockers and cannot be revived by a later search mode.

Accepted PMC XML is cached and normalized to local text immediately, so downstream SDRF evidence
audits do not need a second network fetch.  Supplementary/external-data links are inventoried for a
later bounded design-material recovery step.

GT/reference resources are never read and never supply publication or SDRF values.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
STAGES = ROOT / "python" / "stages"
if str(STAGES) not in sys.path:
    sys.path.insert(0, str(STAGES))

from pride_scp_pipeline_common import (  # noqa: E402
    _europe_pmc_search,
    build_session,
    europe_pmc_lookup,
    normalize_doi,
    text_value,
)

VERSION = "pride-scp-sdrf-residual-external-publication-recovery-v0.1"
EUROPE_PMC_FULLTEXT = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
PXD_RE = re.compile(r"\bPXD\d{6,}\b", re.I)

STOPWORDS = {
    "a", "an", "and", "are", "at", "based", "by", "cell", "cells", "data", "dataset",
    "datasets", "for", "from", "in", "into", "is", "mass", "method", "methods", "of", "on",
    "proteome", "proteomic", "proteomics", "protein", "proteins", "single", "spectrometry",
    "study", "the", "this", "to", "using", "with", "without", "analysis", "approach",
}

PUBLICATION_FIELDS = [
    "accession", "dataset_title", "dataset_description", "publication_status", "publication_source",
    "publication_doi", "publication_pmid", "publication_pmcid", "publication_title",
    "publication_authors", "publication_journal", "publication_year", "publication_url",
    "publication_is_open_access", "publication_has_pdf", "pdf_status", "pdf_path", "pdf_source",
    "publication_content_status", "publication_content_kind", "publication_content_path",
    "publication_content_source", "publication_content_error", "publication_content_chars",
    "publication_content_xml_path", "publication_content_html_path", "publication_content_text_path",
    "external_recovery_status", "external_recovery_reason", "external_recovery_search_modes",
    "external_recovery_exact_accession", "external_recovery_title_overlap",
]

CANDIDATE_FIELDS = [
    "accession", "project_title", "candidate_rank", "search_modes", "status", "reason",
    "candidate_title", "candidate_doi", "candidate_pmid", "candidate_pmcid", "candidate_journal",
    "candidate_year", "candidate_authors", "exact_accession_in_fulltext", "known_identifier_match",
    "shared_title_tokens", "title_overlap", "fulltext_url", "fulltext_error", "quarantined",
]

LINK_FIELDS = ["accession", "publication_doi", "publication_pmcid", "link_type", "link", "context"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--queue-file", default="")
    p.add_argument("--local-manifest", default="")
    p.add_argument("--local-inventory", default="")
    p.add_argument("--snapshot-dir", default="data/snapshot")
    p.add_argument("--quarantine-manifest", action="append", default=[])
    p.add_argument("--output-dir", default="residual_external_publication_recovery")
    p.add_argument("--timeout", type=float, default=45.0)
    p.add_argument("--page-size", type=int, default=25)
    p.add_argument("--user-agent", default="PRIDE-SCP-residual-external-publication-recovery/0.1")
    p.add_argument("--self-test", action="store_true")
    return p.parse_args()


def norm(value: Any) -> str:
    return re.sub(r"\s+", " ", text_value(value)).strip()


def norm_title(value: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", html.unescape(norm(value)).lower()))


def title_tokens(value: Any) -> set[str]:
    return {t for t in norm_title(value).split() if len(t) >= 3 and t not in STOPWORDS}


def title_compat(project: str, candidate: str) -> tuple[list[str], float]:
    p, c = title_tokens(project), title_tokens(candidate)
    shared = sorted(p & c)
    denom = min(len(p), len(c))
    return shared, (len(shared) / denom if denom else 0.0)


def normalize_pmid(value: Any) -> str:
    return re.sub(r"\D", "", norm(value))


def normalize_pmcid(value: Any) -> str:
    v = norm(value).upper()
    if v and not v.startswith("PMC") and v.isdigit():
        v = "PMC" + v
    return v


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
            w.writerow({k: row.get(k, "") for k in fields})


def read_accessions(path: Path) -> list[str]:
    vals = []
    for line in path.read_text(errors="replace").splitlines():
        m = re.fullmatch(r"PXD\d{6,}", line.strip(), re.I)
        if m:
            vals.append(m.group(0).upper())
    return sorted(set(vals))


def json_string_values(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            if str(k).lower() in {"title", "name", "description", "projecttitle", "projectdescription"}:
                if isinstance(v, (str, int, float)):
                    out.append(norm(v))
            out.extend(json_string_values(v))
    elif isinstance(value, list):
        for x in value:
            out.extend(json_string_values(x))
    return [x for x in out if x]


def snapshot_metadata(snapshot: Path, accession: str) -> dict[str, str]:
    paths = [
        snapshot / "projects" / f"{accession}.json",
        snapshot / "project" / f"{accession}.json",
        snapshot / f"{accession}.json",
    ]
    for p in paths:
        if not p.is_file():
            continue
        try:
            obj = json.loads(p.read_text(errors="replace"))
        except Exception:
            continue
        title = ""
        desc = ""
        if isinstance(obj, dict):
            for key in ("title", "projectTitle", "name"):
                if norm(obj.get(key)):
                    title = norm(obj.get(key)); break
            for key in ("description", "projectDescription", "summary"):
                if norm(obj.get(key)):
                    desc = norm(obj.get(key)); break
        if not title:
            values = json_string_values(obj)
            title = next((x for x in values if accession.lower() not in x.lower() and 8 <= len(x) <= 400), "")
        return {"dataset_title": title, "dataset_description": desc}
    return {}


def quarantine_sets(rows: list[dict[str, str]]) -> dict[str, set[str]]:
    out = {"doi": set(), "pmid": set(), "pmcid": set(), "title": set()}
    for r in rows:
        d = normalize_doi(r.get("publication_doi")); p = normalize_pmid(r.get("publication_pmid"));
        c = normalize_pmcid(r.get("publication_pmcid")); t = norm_title(r.get("publication_title"))
        if d: out["doi"].add(d)
        if p: out["pmid"].add(p)
        if c: out["pmcid"].add(c)
        if t: out["title"].add(t)
    return out


def is_quarantined(rec: dict[str, Any], qs: dict[str, set[str]]) -> bool:
    d = normalize_doi(rec.get("doi") or rec.get("candidate_doi"))
    p = normalize_pmid(rec.get("pmid") or rec.get("candidate_pmid"))
    c = normalize_pmcid(rec.get("pmcid") or rec.get("candidate_pmcid"))
    t = norm_title(rec.get("title") or rec.get("candidate_title"))
    return bool((d and d in qs["doi"]) or (p and p in qs["pmid"]) or (c and c in qs["pmcid"]) or (t and t in qs["title"]))


def candidate_key(rec: dict[str, Any]) -> str:
    return normalize_doi(rec.get("doi")) or normalize_pmid(rec.get("pmid")) or normalize_pmcid(rec.get("pmcid")) or norm_title(rec.get("title"))


def merge_candidate(store: dict[str, dict[str, Any]], rec: dict[str, Any], mode: str) -> None:
    key = candidate_key(rec)
    if not key:
        return
    if key not in store:
        store[key] = dict(rec)
        store[key]["_modes"] = {mode}
    else:
        store[key]["_modes"].add(mode)
        for k, v in rec.items():
            if not store[key].get(k) and v:
                store[key][k] = v


def fetch_fulltext(session, pmcid: str, timeout: float) -> tuple[bytes, str, str]:
    pmcid = normalize_pmcid(pmcid)
    if not pmcid:
        return b"", "", "candidate_has_no_pmcid"
    url = EUROPE_PMC_FULLTEXT.format(pmcid=pmcid)
    try:
        r = session.get(url, timeout=timeout, headers={"Accept": "application/xml,text/xml,*/*;q=0.5"})
        r.raise_for_status()
        if len(r.content) < 512:
            return b"", url, "fulltext_too_short"
        ET.fromstring(r.content)
        return r.content, url, ""
    except Exception as exc:
        return b"", url, f"{type(exc).__name__}: {exc}"


def local_tag(elem: ET.Element) -> str:
    return elem.tag.rsplit("}", 1)[-1] if "}" in elem.tag else elem.tag


def xml_to_text(data: bytes) -> str:
    root = ET.fromstring(data)
    blocks: list[str] = []
    for elem in root.iter():
        if local_tag(elem) not in {"article-title", "title", "p", "list-item"}:
            continue
        txt = norm(" ".join(elem.itertext()))
        if txt and (not blocks or blocks[-1] != txt):
            blocks.append(txt)
    return "\n\n".join(blocks).strip() + ("\n" if blocks else "")


def supplementary_links(accession: str, doi: str, pmcid: str, data: bytes) -> list[dict[str, str]]:
    if not data:
        return []
    root = ET.fromstring(data)
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    xlink = "{http://www.w3.org/1999/xlink}href"
    for elem in root.iter():
        href = norm(elem.attrib.get(xlink) or elem.attrib.get("href"))
        txt = norm(" ".join(elem.itertext()))[:500]
        if href and href not in seen and (
            local_tag(elem) in {"supplementary-material", "media", "ext-link"}
            or "supp" in href.lower() or "zenodo" in href.lower()
        ):
            seen.add(href)
            rows.append({"accession": accession, "publication_doi": doi, "publication_pmcid": pmcid,
                         "link_type": local_tag(elem), "link": href, "context": txt})
    text = norm(" ".join(root.itertext()))
    for m in re.finditer(r"(?:https?://(?:www\.)?zenodo\.org/(?:records|record)/\d+|10\.5281/zenodo\.\d+)", text, re.I):
        link = m.group(0)
        if link in seen: continue
        seen.add(link)
        lo=max(0,m.start()-180); hi=min(len(text),m.end()+220)
        rows.append({"accession": accession, "publication_doi": doi, "publication_pmcid": pmcid,
                     "link_type": "zenodo_reference", "link": link, "context": text[lo:hi]})
    return rows


def classify(*, project_title: str, cand_title: str, exact_accession: bool,
             known_identifier_match: bool, quarantined: bool) -> tuple[str, str, list[str], float]:
    shared, overlap = title_compat(project_title, cand_title)
    if quarantined:
        return "rejected", "publication_identity_quarantined", shared, overlap
    pn, cn = norm_title(project_title), norm_title(cand_title)
    strong_identity = bool(pn and cn and (pn == cn or pn in cn or cn in pn))
    if exact_accession:
        if strong_identity or (len(shared) >= 3 and overlap >= 0.25):
            return "accepted", "exact_accession_plus_project_title_identity", shared, overlap
        if len(shared) >= 2 and overlap >= 0.15:
            return "accepted", "exact_accession_plus_project_title_compatibility", shared, overlap
        return "rejected", "exact_accession_but_project_title_mismatch", shared, overlap
    if known_identifier_match:
        if strong_identity or (len(shared) >= 2 and overlap >= 0.15):
            return "accepted", "known_publication_identifier_plus_title_compatibility", shared, overlap
        return "rejected", "known_identifier_but_project_title_mismatch", shared, overlap
    if strong_identity or (len(shared) >= 4 and overlap >= 0.40):
        return "review", "title_identity_without_accession_or_known_identifier", shared, overlap
    return "rejected", "insufficient_publication_identity", shared, overlap


def result_to_publication(accession: str, project: dict[str, str], rec: dict[str, Any],
                          status: str, reason: str, modes: list[str], exact: bool, overlap: float,
                          xml_path: Path, text_path: Path) -> dict[str, str]:
    doi=normalize_doi(rec.get("doi")); pmid=normalize_pmid(rec.get("pmid")); pmcid=normalize_pmcid(rec.get("pmcid"))
    title=norm(rec.get("title")); authors=norm(rec.get("authorString")); journal=norm(rec.get("journalTitle")); year=norm(rec.get("pubYear"))
    return {
        "accession": accession,
        "dataset_title": project.get("dataset_title", ""), "dataset_description": project.get("dataset_description", ""),
        "publication_status": "publication_found", "publication_source": "residual_external_publication_recovery",
        "publication_doi": doi, "publication_pmid": pmid, "publication_pmcid": pmcid,
        "publication_title": title, "publication_authors": authors, "publication_journal": journal,
        "publication_year": year, "publication_url": f"https://europepmc.org/articles/{pmcid}" if pmcid else "",
        "publication_is_open_access": norm(rec.get("isOpenAccess")), "publication_has_pdf": norm(rec.get("hasPDF")),
        "pdf_status": "not_applicable", "pdf_path": "", "pdf_source": "",
        "publication_content_status": "available" if text_path.is_file() else "unavailable",
        "publication_content_kind": "fulltext_xml" if text_path.is_file() else "",
        "publication_content_path": str(text_path.resolve()) if text_path.is_file() else "",
        "publication_content_source": "europe_pmc_fullTextXML_verified_residual_recovery" if text_path.is_file() else "",
        "publication_content_error": "", "publication_content_chars": str(len(text_path.read_text(errors='replace'))) if text_path.is_file() else "",
        "publication_content_xml_path": str(xml_path.resolve()) if xml_path.is_file() else "",
        "publication_content_html_path": "", "publication_content_text_path": str(text_path.resolve()) if text_path.is_file() else "",
        "external_recovery_status": status, "external_recovery_reason": reason,
        "external_recovery_search_modes": ",".join(modes), "external_recovery_exact_accession": str(exact).lower(),
        "external_recovery_title_overlap": f"{overlap:.4f}",
    }


def run(args: argparse.Namespace) -> int:
    queue = read_accessions(Path(args.queue_file))
    if not queue:
        raise SystemExit("recovery queue is empty")
    local_rows = read_tsv(Path(args.local_manifest))
    by_acc: dict[str, list[dict[str, str]]] = defaultdict(list)
    for r in local_rows:
        a=norm(r.get("accession")).upper()
        if a: by_acc[a].append(r)
    qrows: list[dict[str, str]]=[]
    for q in args.quarantine_manifest:
        qrows.extend(read_tsv(Path(q)))
    qs=quarantine_sets(qrows)
    snapshot=Path(args.snapshot_dir)
    out=Path(args.output_dir); out.mkdir(parents=True, exist_ok=True)
    xml_dir=out/"publication_content"/"xml"; text_dir=out/"publication_content"/"text"
    xml_dir.mkdir(parents=True, exist_ok=True); text_dir.mkdir(parents=True, exist_ok=True)
    session=build_session(user_agent=args.user_agent)

    candidate_rows: list[dict[str, Any]]=[]; recovered: list[dict[str,str]]=[]; links: list[dict[str,str]]=[]
    per_acc: dict[str, Any]={}

    for accession in queue:
        existing=by_acc.get(accession, [])
        project={"dataset_title":"", "dataset_description":""}
        if existing:
            project["dataset_title"] = next((norm(r.get("dataset_title")) for r in existing if norm(r.get("dataset_title"))), "")
            project["dataset_description"] = next((norm(r.get("dataset_description")) for r in existing if norm(r.get("dataset_description"))), "")
        snap=snapshot_metadata(snapshot, accession)
        project["dataset_title"] = project["dataset_title"] or snap.get("dataset_title","")
        project["dataset_description"] = project["dataset_description"] or snap.get("dataset_description","")
        known_dois={normalize_doi(r.get("publication_doi")) for r in existing if normalize_doi(r.get("publication_doi"))}
        known_pmids={normalize_pmid(r.get("publication_pmid")) for r in existing if normalize_pmid(r.get("publication_pmid"))}
        known_titles={norm(r.get("publication_title")) for r in existing if norm(r.get("publication_title"))}
        store: dict[str,dict[str,Any]]={}

        for doi in sorted(known_dois):
            rec=europe_pmc_lookup(session, doi=doi, timeout=args.timeout)
            if rec: merge_candidate(store, rec, "known_doi")
        for pmid in sorted(known_pmids):
            rec=europe_pmc_lookup(session, pmid=pmid, timeout=args.timeout)
            if rec: merge_candidate(store, rec, "known_pmid")
        for title in sorted(known_titles)[:2]:
            rec=europe_pmc_lookup(session, title=title, timeout=args.timeout)
            if rec: merge_candidate(store, rec, "known_title")

        try:
            for rec in _europe_pmc_search(session, f'"{accession}"', timeout=args.timeout, page_size=args.page_size):
                merge_candidate(store, rec, "exact_accession_search")
        except Exception:
            pass
        if project["dataset_title"]:
            try:
                for rec in _europe_pmc_search(session, f'TITLE:"{project["dataset_title"]}"', timeout=args.timeout, page_size=min(args.page_size,10)):
                    merge_candidate(store, rec, "project_title_search")
            except Exception:
                pass

        ranked=[]
        for rec in store.values():
            pmcid=normalize_pmcid(rec.get("pmcid")); data,url,err=fetch_fulltext(session, pmcid, args.timeout)
            text=xml_to_text(data) if data else ""
            exact=bool(re.search(rf"(?<![A-Z0-9]){re.escape(accession)}(?![A-Z0-9])", text, re.I)) if text else False
            d=normalize_doi(rec.get("doi")); p=normalize_pmid(rec.get("pmid"))
            known_id=bool((d and d in known_dois) or (p and p in known_pmids))
            quarantined=is_quarantined(rec, qs)
            status,reason,shared,overlap=classify(project_title=project["dataset_title"], cand_title=norm(rec.get("title")), exact_accession=exact, known_identifier_match=known_id, quarantined=quarantined)
            score=(3 if status=="accepted" else 2 if status=="review" else 1, int(exact), int(known_id), overlap, len(shared), int(bool(data)))
            ranked.append((score,rec,data,url,err,exact,known_id,quarantined,status,reason,shared,overlap))
        ranked.sort(key=lambda x:x[0], reverse=True)

        selected=None
        for rank,item in enumerate(ranked, start=1):
            _,rec,data,url,err,exact,known_id,quarantined,status,reason,shared,overlap=item
            row={
                "accession":accession,"project_title":project["dataset_title"],"candidate_rank":rank,
                "search_modes":",".join(sorted(rec.get("_modes",set()))),"status":status,"reason":reason,
                "candidate_title":norm(rec.get("title")),"candidate_doi":normalize_doi(rec.get("doi")),
                "candidate_pmid":normalize_pmid(rec.get("pmid")),"candidate_pmcid":normalize_pmcid(rec.get("pmcid")),
                "candidate_journal":norm(rec.get("journalTitle")),"candidate_year":norm(rec.get("pubYear")),
                "candidate_authors":norm(rec.get("authorString")),"exact_accession_in_fulltext":str(exact).lower(),
                "known_identifier_match":str(known_id).lower(),"shared_title_tokens":",".join(shared),
                "title_overlap":f"{overlap:.4f}","fulltext_url":url,"fulltext_error":err,"quarantined":str(quarantined).lower(),
            }
            candidate_rows.append(row)
            if selected is None and status=="accepted" and data:
                selected=(rec,data,exact,status,reason,overlap)

        if selected:
            rec,data,exact,status,reason,overlap=selected
            doi=normalize_doi(rec.get("doi")); pmcid=normalize_pmcid(rec.get("pmcid"))
            base=re.sub(r"[^A-Za-z0-9._-]+","_", doi or pmcid or accession)
            xml_path=xml_dir/f"{base}.fulltext.xml"; text_path=text_dir/f"{base}.fulltext.txt"
            xml_path.write_bytes(data); text_path.write_text(xml_to_text(data), encoding="utf-8")
            recovered.append(result_to_publication(accession, project, rec, status, reason, sorted(rec.get("_modes",set())), exact, overlap, xml_path, text_path))
            links.extend(supplementary_links(accession, doi, pmcid, data))
        per_acc[accession]={
            "project_title":project["dataset_title"],"candidates":len(ranked),"accepted":sum(x[8]=="accepted" for x in ranked),
            "review":sum(x[8]=="review" for x in ranked),"rejected":sum(x[8]=="rejected" for x in ranked),
            "recovered":bool(selected),"best_status":ranked[0][8] if ranked else "no_candidate",
            "best_title":norm(ranked[0][1].get("title")) if ranked else "",
        }

    # Merge local-first manifest with accepted external rows.  Queue accessions had no selected local
    # content, but preserve all non-queue/local rows exactly and replace only accepted queue rows.
    recovered_by={r["accession"]:r for r in recovered}
    combined=[]
    for r in local_rows:
        a=norm(r.get("accession")).upper()
        if a in recovered_by:
            continue
        combined.append(r)
    combined.extend(recovered)

    fields=list(dict.fromkeys([*PUBLICATION_FIELDS, *[k for r in local_rows for k in r.keys()]]))
    write_tsv(out/"publication_recovery_candidates.tsv", candidate_rows, CANDIDATE_FIELDS)
    write_tsv(out/"recovered_publications.tsv", recovered, PUBLICATION_FIELDS)
    write_tsv(out/"publication_supplementary_links.tsv", links, LINK_FIELDS)
    write_tsv(out/"combined_publication_manifest.tsv", combined, fields)
    unresolved=[a for a in queue if a not in recovered_by]
    (out/"unresolved_after_external_recovery.txt").write_text("\n".join(unresolved)+("\n" if unresolved else ""))
    summary={
        "auditor_version":VERSION,"queue_accessions":len(queue),"recovered_accessions":len(recovered),
        "unresolved_accessions":unresolved,"supplementary_links":len(links),"per_accession":per_acc,
        "non_generative":True,"gt_metadata_used":False,
        "outputs":{"candidates":str((out/"publication_recovery_candidates.tsv").resolve()),
                   "recovered":str((out/"recovered_publications.tsv").resolve()),
                   "combined_manifest":str((out/"combined_publication_manifest.tsv").resolve()),
                   "supplementary_links":str((out/"publication_supplementary_links.tsv").resolve())},
    }
    (out/"residual_external_publication_recovery_summary.json").write_text(json.dumps(summary,indent=2)+"\n")
    print(json.dumps(summary,indent=2))
    return 0


def self_test() -> None:
    # PXD029320-like strong exact-accession title match.
    s,r,shared,o=classify(project_title="Real-Time Search Assisted Acquisition on a Tribrid Mass Spectrometer Improves Coverage in Multiplexed Single-Cell Proteomics", cand_title="Real-Time Search-Assisted Acquisition on a Tribrid Mass Spectrometer Improves Coverage in Multiplexed Single-Cell Proteomics", exact_accession=True, known_identifier_match=False, quarantined=False)
    assert s=="accepted" and len(shared)>=3 and o>0.5, (s,r,shared,o)
    # PXD069039-style erroneous exact accession in unrelated paper must remain rejected.
    s,r,_,_=classify(project_title="Multiplexed quantitation of post-translational modified peptides in single cells using triggered MS/MS combined with super heavy tandem mass tags", cand_title="With or without a Ca2+ signal? a proteomics approach toward oxidative stress in Arabidopsis thaliana", exact_accession=True, known_identifier_match=False, quarantined=False)
    assert s=="rejected" and "mismatch" in r
    # Quarantine overrides even an otherwise matching identity.
    s,r,_,_=classify(project_title="One-Tip enables comprehensive proteome coverage in minimal cells and single zygotes", cand_title="One-Tip enables comprehensive proteome coverage in minimal cells and single zygotes", exact_accession=True, known_identifier_match=True, quarantined=True)
    assert s=="rejected" and r=="publication_identity_quarantined"
    # Known DOI may recover a compatible paper without an accession mention.
    s,r,_,_=classify(project_title="Reduced ATP turnover during hibernation in relaxed skeletal muscle", cand_title="Reduced ATP turnover during hibernation in relaxed skeletal muscle", exact_accession=False, known_identifier_match=True, quarantined=False)
    assert s=="accepted"
    # Title alone remains review only.
    s,r,_,_=classify(project_title="Example distinctive title alpha beta gamma", cand_title="Example distinctive title alpha beta gamma", exact_accession=False, known_identifier_match=False, quarantined=False)
    assert s=="review"
    print("sdrf_residual_external_publication_recovery self-test: PASS")


if __name__ == "__main__":
    args=parse_args()
    if args.self_test:
        self_test()
        raise SystemExit(0)
    raise SystemExit(run(args))
