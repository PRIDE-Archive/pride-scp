#!/usr/bin/env python3
"""Field-directed external evidence recovery for unresolved PRIDE-SCP SDRF drafts.

This stage is intentionally evidence-first and non-generative. It reads the scientific
review TSVs produced by ``pride-scp sdrf-annotate``, keeps *error-level* issues only,
and separates them into three classes:

* evidence_search: a missing scientific field can be searched in publication sources;
* deterministic_zero_cell: empty/zero-cell rows contain leaked biological context;
* structured_mapping: heterogeneous row-level source identity needs explicit mapping.

For evidence_search issues the stage builds bounded, field-specific queries and evidence
packets. Local publication text is searched first. With ``--online`` it then uses
Europe PMC full text and supplementary files. Crossref metadata is used to discover
related identifiers/links; broad web-search queries are emitted for an optional external
provider but are never required for correctness.

The output ``targeted_publication_manifest.tsv`` contains only synthetic text rows for
recovered targeted evidence and can be passed to the existing Stage04 annotator. The
``augmented_publication_manifest.tsv`` appends those rows to the input manifest and can
be passed directly to ``pride-scp sdrf-annotate``.

No sample/file/channel mapping is generated. Missing evidence remains unresolved.
GT/reference resources are never read.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import re
import sys
import zipfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
STAGES = ROOT / "python" / "stages"
if str(STAGES) not in sys.path:
    sys.path.insert(0, str(STAGES))

from pride_scp_pipeline_common import (  # noqa: E402
    _europe_pmc_search,
    build_session,
    crossref_lookup,
    europe_pmc_lookup,
    extract_pdf_text,
    normalize_doi,
    text_value,
)

VERSION = "pride-scp-targeted-evidence-recovery-v0.1"
POLICY_VERSION = "pride-scp-field-directed-evidence-policy-v0.1"
EUROPE_PMC_FULLTEXT = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
EUROPE_PMC_SUPPLEMENTS = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/supplementaryFiles"

TARGETED_FIELDS: dict[str, tuple[str, ...]] = {
    "single_cell_isolation_method": (
        "single cell isolation", "single-cell isolation", "isolation protocol",
        "cell isolation", "cell sorting", "sorted", "sorting", "FACS",
        "fluorescence-activated", "flow cytometry", "cellenone", "cellenONE",
        "manual picking", "manually picked", "manually isolated", "micromanipulation",
        "micropipette", "capillary microsampling", "capillary sampling", "aspirat",
        "laser capture", "microdissection", "nanoPOTS", "nanowell", "dispens",
        "deposited into", "single cells were isolated", "individual cells were isolated",
    ),
    "cell_type": ("cell type", "cell line", "cell identity", "single cells"),
    "organism": ("organism", "species", "human", "mouse", "xenopus"),
    "organism_part": ("organism part", "tissue", "embryo", "brain", "tumor", "skin"),
    "disease": ("disease", "syndrome", "tumor", "cancer", "leukemia"),
    "individual": ("individual", "patient", "donor", "subject"),
    "cleavage_agent_details": ("trypsin", "lys-c", "digestion", "protease"),
}

FIELD_QUERY_LABELS = {
    "single_cell_isolation_method": "single cell isolation sorting FACS cellenONE manual picking micromanipulation",
    "cell_type": "cell type cell line",
    "organism": "species organism",
    "organism_part": "tissue organism part",
    "disease": "disease phenotype",
    "individual": "patient donor individual",
    "cleavage_agent_details": "protein digestion protease trypsin",
}

TEXT_EXTS = {".txt", ".tsv", ".csv", ".xml", ".html", ".htm", ".md"}
ARCHIVE_TEXT_EXTS = TEXT_EXTS | {".xlsx", ".docx", ".pdf"}


@dataclass(frozen=True)
class ReviewIssue:
    accession: str
    level: str
    code: str
    row: int
    column: str
    message: str
    field: str
    category: str


@dataclass(frozen=True)
class EvidenceRecord:
    accession: str
    field: str
    provider: str
    source: str
    publication_doi: str
    publication_title: str
    evidence_text: str
    evidence_sha256: str


def read_tsv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", errors="replace", newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def write_tsv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, delimiter="\t", extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for row in rows:
            w.writerow({f: row.get(f, "") for f in fields})


def read_accessions(path: Path | None) -> set[str]:
    if not path or not path.is_file():
        return set()
    out = set()
    for line in path.read_text(errors="replace").splitlines():
        cell = re.split(r"[\t,]", line.strip(), maxsplit=1)[0].strip().upper()
        if re.fullmatch(r"PXD\d{6,}", cell):
            out.add(cell)
    return out


def normalize_pmcid(value: Any) -> str:
    text = text_value(value).upper().strip()
    if text.isdigit():
        text = "PMC" + text
    return text if re.fullmatch(r"PMC\d+", text) else ""


def canonical_field(column: str, message: str) -> str:
    c = column.strip().lower()
    m = message.lower()
    if "single cell isolation protocol" in c or "isolation method" in m:
        return "single_cell_isolation_method"
    if "cell type" in c:
        return "cell_type"
    if "organism part" in c:
        return "organism_part"
    if c.endswith("[organism]") or c == "organism":
        return "organism"
    if "disease" in c:
        return "disease"
    if "individual" in c:
        return "individual"
    if "cleavage" in c:
        return "cleavage_agent_details"
    return re.sub(r"[^a-z0-9]+", "_", c).strip("_") or "unknown"


def classify_issue(column: str, message: str) -> str:
    m = message.lower()
    if "empty/zero-cell control carries" in m:
        return "deterministic_zero_cell"
    if "multiple source values" in m and ("collapses" in m or "row-level source mapping" in m):
        return "structured_mapping"
    if "sample-to-file" in m or "channel mapping" in m or "mapping may be unresolved" in m:
        return "structured_mapping"
    if "does not allow 'not available' for isolation method" in m:
        return "evidence_search"
    if "without a valid field-specific evidence reference" in m:
        return "evidence_search"
    if "required" in m or "missing" in m or "not available" in m:
        return "evidence_search"
    return "review_only"


def review_issues(annotation_results: Path, selected: set[str]) -> tuple[list[ReviewIssue], list[dict[str, str]]]:
    result_rows = read_tsv(annotation_results)
    issues: list[ReviewIssue] = []
    selected_rows: list[dict[str, str]] = []
    for result in result_rows:
        acc = (result.get("accession") or "").strip().upper()
        if selected and acc not in selected:
            continue
        if (result.get("locally_valid") or "").strip().lower() == "true":
            continue
        review_path = Path((result.get("review_path") or "").strip())
        if not review_path.is_file():
            continue
        selected_rows.append(result)
        for row in read_tsv(review_path):
            level = (row.get("level") or "").strip().lower()
            if level != "error":
                continue
            column = (row.get("column") or "").strip()
            message = (row.get("message") or "").strip()
            issues.append(ReviewIssue(
                accession=acc,
                level=level,
                code=(row.get("code") or "").strip(),
                row=int((row.get("row") or "0").strip() or 0),
                column=column,
                message=message,
                field=canonical_field(column, message),
                category=classify_issue(column, message),
            ))
    return issues, selected_rows


def publication_rows_by_accession(path: Path) -> dict[str, list[dict[str, str]]]:
    out: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in read_tsv(path):
        acc = (row.get("accession") or "").strip().upper()
        if re.fullmatch(r"PXD\d{6,}", acc):
            out[acc].append(row)
    return out


def publication_identity(rows: list[dict[str, str]]) -> tuple[str, str, str, str]:
    def first(name: str) -> str:
        return next(((r.get(name) or "").strip() for r in rows if (r.get(name) or "").strip()), "")
    return normalize_doi(first("publication_doi")), first("publication_pmid"), normalize_pmcid(first("publication_pmcid")), first("publication_title")


def content_paths(rows: list[dict[str, str]]) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()
    for row in rows:
        for key in ("publication_content_text_path", "publication_content_path", "pdf_path"):
            value = (row.get(key) or "").strip()
            if not value or value in seen:
                continue
            p = Path(value)
            if p.is_file():
                seen.add(value)
                out.append(p)
    return out


def read_text_artifact(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        text, _, _ = extract_pdf_text(path)
        return text
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def normalized_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def snippet_windows(text: str, terms: tuple[str, ...], *, radius: int = 650, max_snippets: int = 8) -> list[str]:
    clean = normalized_text(text)
    if not clean:
        return []
    low = clean.lower()
    candidates: list[tuple[int, int]] = []
    for term in terms:
        start = 0
        t = term.lower()
        while True:
            pos = low.find(t, start)
            if pos < 0:
                break
            candidates.append((max(0, pos - radius), min(len(clean), pos + len(term) + radius)))
            start = pos + max(1, len(term))
    candidates.sort()
    merged: list[tuple[int, int]] = []
    for a, b in candidates:
        if merged and a <= merged[-1][1] + 120:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    snippets = []
    seen = set()
    for a, b in merged:
        s = clean[a:b].strip()
        digest = hashlib.sha256(s.lower().encode()).hexdigest()[:20]
        if len(s) >= 80 and digest not in seen:
            seen.add(digest)
            snippets.append(s)
        if len(snippets) >= max_snippets:
            break
    return snippets


def xml_text(data: bytes) -> str:
    text = data.decode("utf-8", "replace")
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    return normalized_text(re.sub(r"<[^>]+>", " ", text))


def xlsx_text(data: bytes) -> str:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return ""
    shared: list[str] = []
    if "xl/sharedStrings.xml" in zf.namelist():
        shared_xml = zf.read("xl/sharedStrings.xml").decode("utf-8", "replace")
        for si in re.findall(r"<si\b.*?</si>", shared_xml, flags=re.S):
            shared.append(normalized_text(re.sub(r"<[^>]+>", " ", si)))
    out: list[str] = []
    for name in sorted(x for x in zf.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", x)):
        raw = zf.read(name).decode("utf-8", "replace")
        for cell in re.findall(r"<c\b[^>]*>.*?</c>", raw, flags=re.S):
            typ = re.search(r'\bt="([^"]+)"', cell)
            val = re.search(r"<v>(.*?)</v>", cell, flags=re.S)
            if not val:
                inline = re.search(r"<t[^>]*>(.*?)</t>", cell, flags=re.S)
                if inline:
                    out.append(html.unescape(inline.group(1)))
                continue
            v = html.unescape(val.group(1))
            if typ and typ.group(1) == "s" and v.isdigit() and int(v) < len(shared):
                v = shared[int(v)]
            out.append(v)
    return normalized_text(" | ".join(out))


def docx_text(data: bytes) -> str:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        raw = zf.read("word/document.xml").decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError):
        return ""
    return normalized_text(re.sub(r"<[^>]+>", " ", raw))


def member_text(name: str, data: bytes, scratch: Path) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in TEXT_EXTS:
        return xml_text(data) if suffix in {".xml", ".html", ".htm"} else normalized_text(data.decode("utf-8", "replace"))
    if suffix == ".xlsx":
        return xlsx_text(data)
    if suffix == ".docx":
        return docx_text(data)
    if suffix == ".pdf":
        scratch.mkdir(parents=True, exist_ok=True)
        p = scratch / (hashlib.sha256(data).hexdigest()[:16] + ".pdf")
        p.write_bytes(data)
        text, _, _ = extract_pdf_text(p)
        return text
    return ""


def evidence_record(acc: str, field: str, provider: str, source: str, doi: str, title: str, snippet: str) -> EvidenceRecord:
    snippet = normalized_text(snippet)
    return EvidenceRecord(acc, field, provider, source, doi, title, snippet, hashlib.sha256(snippet.encode()).hexdigest())


def add_snippets(records: list[EvidenceRecord], acc: str, field: str, provider: str, source: str, doi: str, title: str, text: str, max_snippets: int) -> None:
    terms = TARGETED_FIELDS.get(field, (field.replace("_", " "),))
    existing = {r.evidence_sha256 for r in records if r.accession == acc and r.field == field}
    for snippet in snippet_windows(text, terms, max_snippets=max_snippets):
        rec = evidence_record(acc, field, provider, source, doi, title, snippet)
        if rec.evidence_sha256 not in existing:
            records.append(rec)
            existing.add(rec.evidence_sha256)


def fetch_europe_pmc(session, acc: str, field: str, doi: str, pmid: str, pmcid: str, title: str, *, timeout: float, max_snippets: int, max_supp_bytes: int, scratch: Path) -> list[EvidenceRecord]:
    records: list[EvidenceRecord] = []
    rec = europe_pmc_lookup(session, doi=doi, pmid=pmid, title=title, timeout=timeout)
    if not rec:
        results = _europe_pmc_search(session, f'"{acc}"', timeout=timeout, page_size=5)
        rec = results[0] if results else None
    if not rec:
        return records
    rdoi = normalize_doi(rec.get("doi")) or doi
    rtitle = text_value(rec.get("title")) or title
    rpmcid = normalize_pmcid(rec.get("pmcid")) or pmcid
    if not rpmcid:
        return records

    url = EUROPE_PMC_FULLTEXT.format(pmcid=rpmcid)
    try:
        response = session.get(url, timeout=timeout, headers={"Accept": "application/xml,text/xml,*/*"})
        response.raise_for_status()
        fulltext = xml_text(response.content)
        add_snippets(records, acc, field, "europe_pmc_fulltext", url, rdoi, rtitle, fulltext, max_snippets)
    except Exception:
        pass

    supp_url = EUROPE_PMC_SUPPLEMENTS.format(pmcid=rpmcid)
    try:
        response = session.get(supp_url, timeout=timeout, headers={"Accept": "application/zip,*/*"}, stream=True)
        response.raise_for_status()
        data = response.content
        if len(data) <= max_supp_bytes and zipfile.is_zipfile(io.BytesIO(data)):
            zf = zipfile.ZipFile(io.BytesIO(data))
            for info in zf.infolist():
                if info.is_dir() or info.file_size > min(max_supp_bytes, 8 * 1024 * 1024):
                    continue
                if Path(info.filename).suffix.lower() not in ARCHIVE_TEXT_EXTS:
                    continue
                try:
                    blob = zf.read(info)
                except Exception:
                    continue
                text = member_text(info.filename, blob, scratch)
                if text:
                    add_snippets(records, acc, field, "europe_pmc_supplement", f"{supp_url}#{info.filename}", rdoi, rtitle, text, max_snippets)
    except Exception:
        pass
    return records


def crossref_related_queries(session, doi: str, title: str, *, timeout: float) -> tuple[list[str], list[str]]:
    queries: list[str] = []
    links: list[str] = []
    if doi:
        try:
            rec = crossref_lookup(session, doi, timeout=timeout)
        except Exception:
            rec = None
        if isinstance(rec, dict):
            relation = rec.get("relation") or {}
            if isinstance(relation, dict):
                for values in relation.values():
                    if isinstance(values, dict):
                        values = [values]
                    if isinstance(values, list):
                        for item in values:
                            if isinstance(item, dict):
                                rd = normalize_doi(item.get("id"))
                                if rd:
                                    queries.append(f'"{rd}"')
            for item in rec.get("link") or []:
                if isinstance(item, dict) and text_value(item.get("URL")):
                    links.append(text_value(item.get("URL")))
    if title:
        queries.append(f'"{title}"')
    return list(dict.fromkeys(queries)), list(dict.fromkeys(links))



def fetch_biorxiv_preprint(session, acc: str, field: str, doi: str, title: str, *, timeout: float, max_snippets: int) -> list[EvidenceRecord]:
    """Recover a bioRxiv/medRxiv alternate version for a published DOI when available.

    The public bioRxiv API accepts either a preprint DOI or a published DOI through
    the /pubs/{server}/{DOI}/na/json endpoint.  If a preprint is found we query its
    detail record and prefer the advertised JATS XML path; abstract text is retained
    only as a last-resort evidence source.
    """
    if not doi:
        return []
    candidates: list[tuple[str, str, str]] = []
    if doi.startswith("10.1101/"):
        candidates.extend((server, doi, title) for server in ("biorxiv", "medrxiv"))
    else:
        for server in ("biorxiv", "medrxiv"):
            url = f"https://api.biorxiv.org/pubs/{server}/{quote(doi, safe='')}/na/json"
            try:
                response = session.get(url, timeout=timeout, headers={"Accept": "application/json"})
                response.raise_for_status()
                data = response.json()
            except Exception:
                continue
            collection = data.get("collection", []) if isinstance(data, dict) else []
            if isinstance(collection, dict):
                collection = [collection]
            for item in collection or []:
                if not isinstance(item, dict):
                    continue
                preprint_doi = normalize_doi(item.get("biorxiv_doi") or item.get("doi"))
                preprint_title = text_value(item.get("preprint_title") or item.get("title")) or title
                if preprint_doi:
                    candidates.append((server, preprint_doi, preprint_title))
    seen = set()
    out: list[EvidenceRecord] = []
    for server, preprint_doi, preprint_title in candidates:
        key = (server, preprint_doi)
        if key in seen:
            continue
        seen.add(key)
        detail_url = f"https://api.biorxiv.org/details/{server}/{quote(preprint_doi, safe='')}/na/json"
        try:
            response = session.get(detail_url, timeout=timeout, headers={"Accept": "application/json"})
            response.raise_for_status()
            data = response.json()
        except Exception:
            continue
        collection = data.get("collection", []) if isinstance(data, dict) else []
        if isinstance(collection, dict):
            collection = [collection]
        for item in collection or []:
            if not isinstance(item, dict):
                continue
            item_title = text_value(item.get("title")) or preprint_title
            abstract = text_value(item.get("abstract"))
            if abstract:
                add_snippets(out, acc, field, f"{server}_abstract", detail_url, preprint_doi, item_title, abstract, max_snippets)
            jats = text_value(item.get("jatsxml") or item.get("jats_xml") or item.get("jats xml path"))
            if jats:
                if jats.startswith("//"):
                    jats = "https:" + jats
                elif jats.startswith("/"):
                    jats = f"https://www.{server}.org" + jats
                if jats.startswith("http"):
                    try:
                        full = session.get(jats, timeout=timeout, headers={"Accept": "application/xml,text/xml,*/*"})
                        full.raise_for_status()
                        add_snippets(out, acc, field, f"{server}_jats", jats, preprint_doi, item_title, xml_text(full.content), max_snippets)
                    except Exception:
                        pass
    return out

def make_queries(acc: str, field: str, doi: str, title: str) -> list[str]:
    concept = FIELD_QUERY_LABELS.get(field, field.replace("_", " "))
    out = [f'"{acc}" {concept}']
    if doi:
        out.append(f'"{doi}" {concept}')
    if title:
        out.append(f'"{title}" {concept}')
        out.append(f'"{title}" supplementary methods {concept}')
    return list(dict.fromkeys(out))


def packet_text(acc: str, field_records: dict[str, list[EvidenceRecord]], queries: dict[str, list[str]]) -> str:
    out = [
        f"PRIDE-SCP targeted evidence packet for {acc}",
        "This packet contains source excerpts only. Missing values must not be inferred.",
    ]
    eid = 1
    for field in sorted(set(field_records) | set(queries)):
        out.extend(["", f"FIELD: {field}"])
        if queries.get(field):
            out.append("SEARCH QUERIES: " + " | ".join(queries[field]))
        rows = field_records.get(field, [])
        if not rows:
            out.append("NO SOURCE-BACKED EVIDENCE RECOVERED FOR THIS FIELD.")
            continue
        for rec in rows:
            out.append(
                f"[TE{eid:03d}] provider={rec.provider}; source={rec.source}; doi={rec.publication_doi or 'NA'}; field={field}\n{rec.evidence_text}"
            )
            eid += 1
    return "\n".join(out).strip() + "\n"


def manifest_fields(rows: list[dict[str, str]]) -> list[str]:
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    required = [
        "accession", "publication_index", "publication_doi", "publication_pmid", "publication_pmcid",
        "publication_title", "publication_content_status", "publication_content_kind",
        "publication_content_path", "publication_content_text_path", "publication_content_source",
        "publication_content_chars",
    ]
    for key in required:
        if key not in fields:
            fields.append(key)
    return fields


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--annotation-results", type=Path, required=False)
    p.add_argument("--publication-manifest", type=Path, required=False)
    p.add_argument("--snapshot", type=Path, default=Path("data/snapshot"))
    p.add_argument("--accessions-file", type=Path)
    p.add_argument("--output", type=Path, default=Path("work/targeted_evidence_recovery"))
    p.add_argument("--online", action="store_true")
    p.add_argument("--timeout", type=float, default=45.0)
    p.add_argument("--max-snippets-per-source", type=int, default=6)
    p.add_argument("--max-supplement-bytes", type=int, default=50 * 1024 * 1024)
    p.add_argument("--user-agent", default="PRIDE-SCP-targeted-evidence-recovery/0.1")
    p.add_argument("--self-test", action="store_true")
    return p


def self_test() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        review = root / "PXD900001.review.tsv"
        write_tsv(review, [
            {"level":"error","code":"x","row":"1","column":"characteristics[single cell isolation protocol]","message":"single-cell 1.0.0 template does not allow 'not available' for isolation method on a study/single-cell row"},
            {"level":"warning","code":"w","row":"2","column":"characteristics[organism]","message":"PRIDE project exposes multiple source values"},
            {"level":"error","code":"z","row":"3","column":"characteristics[cell type]","message":"empty/zero-cell control carries concrete biological identity 'HeLa cell'"},
        ], ["level","code","row","column","message"])
        results = root / "results.tsv"
        write_tsv(results, [{"accession":"PXD900001","locally_valid":"false","review_path":str(review)}], ["accession","locally_valid","review_path"])
        content = root / "paper.txt"
        content.write_text("Methods. Individual cells were isolated by FACS and deposited into nanowells. " * 20)
        manifest = root / "manifest.tsv"
        write_tsv(manifest, [{"accession":"PXD900001","publication_index":"0","publication_doi":"10.1/test","publication_title":"Synthetic single cell proteomics","publication_content_status":"available","publication_content_kind":"text","publication_content_path":str(content),"publication_content_text_path":str(content)}], manifest_fields([]))
        selected = {"PXD900001"}
        issues, _ = review_issues(results, selected)
        assert len(issues) == 2
        assert Counter(x.category for x in issues) == {"evidence_search": 1, "deterministic_zero_cell": 1}
        rows = publication_rows_by_accession(manifest)
        recs: list[EvidenceRecord] = []
        add_snippets(recs, "PXD900001", "single_cell_isolation_method", "local_publication", str(content), "10.1/test", "Synthetic", content.read_text(), 6)
        assert recs and "FACS" in recs[0].evidence_text
        packet = packet_text("PXD900001", {"single_cell_isolation_method": recs}, {"single_cell_isolation_method": make_queries("PXD900001", "single_cell_isolation_method", "10.1/test", "Synthetic")})
        assert "FIELD: single_cell_isolation_method" in packet and "TE001" in packet
        assert rows["PXD900001"]
    print("sdrf_targeted_evidence_recovery self-test: PASS")


def main() -> int:
    args = build_parser().parse_args()
    if args.self_test:
        self_test()
        return 0
    if not args.annotation_results or not args.annotation_results.is_file():
        raise SystemExit("--annotation-results is required")
    if not args.publication_manifest or not args.publication_manifest.is_file():
        raise SystemExit("--publication-manifest is required")

    out = args.output.absolute()
    out.mkdir(parents=True, exist_ok=True)
    packet_dir = out / "evidence_packets"
    packet_dir.mkdir(parents=True, exist_ok=True)
    selected = read_accessions(args.accessions_file)
    issues, result_rows = review_issues(args.annotation_results, selected)
    if not selected:
        selected = {r.get("accession", "").strip().upper() for r in result_rows if r.get("accession")}

    pub_rows = read_tsv(args.publication_manifest)
    pub_by = publication_rows_by_accession(args.publication_manifest)
    session = build_session(args.user_agent)

    grouped: dict[tuple[str, str], list[ReviewIssue]] = defaultdict(list)
    for issue in issues:
        grouped[(issue.accession, issue.field)].append(issue)

    evidence: list[EvidenceRecord] = []
    query_map: dict[tuple[str, str], list[str]] = {}
    crossref_links: list[dict[str, str]] = []

    for (acc, field), field_issues in sorted(grouped.items()):
        if not any(x.category == "evidence_search" for x in field_issues):
            continue
        rows = pub_by.get(acc, [])
        doi, pmid, pmcid, title = publication_identity(rows)
        queries = make_queries(acc, field, doi, title)
        if args.online:
            related_queries, links = crossref_related_queries(session, doi, title, timeout=args.timeout)
            queries.extend(q for q in related_queries if q not in queries)
            crossref_links.extend({"accession":acc,"field":field,"url":u} for u in links)
        query_map[(acc, field)] = queries

        for path in content_paths(rows):
            text = read_text_artifact(path)
            add_snippets(evidence, acc, field, "local_publication", str(path), doi, title, text, args.max_snippets_per_source)

        if args.online:
            evidence.extend(fetch_europe_pmc(
                session, acc, field, doi, pmid, pmcid, title,
                timeout=args.timeout,
                max_snippets=args.max_snippets_per_source,
                max_supp_bytes=args.max_supplement_bytes,
                scratch=out / "scratch",
            ))
            evidence.extend(fetch_biorxiv_preprint(
                session, acc, field, doi, title,
                timeout=args.timeout,
                max_snippets=args.max_snippets_per_source,
            ))

    # Deduplicate evidence globally.
    unique: dict[tuple[str, str, str], EvidenceRecord] = {}
    for rec in evidence:
        unique[(rec.accession, rec.field, rec.evidence_sha256)] = rec
    evidence = list(unique.values())

    plan_rows: list[dict[str, Any]] = []
    category_by_acc: dict[str, set[str]] = defaultdict(set)
    for issue in issues:
        category_by_acc[issue.accession].add(issue.category)
    for (acc, field), xs in sorted(grouped.items()):
        categories = sorted(set(x.category for x in xs))
        rows = [e for e in evidence if e.accession == acc and e.field == field]
        plan_rows.append({
            "accession": acc,
            "field": field,
            "error_count": len(xs),
            "categories": ";".join(categories),
            "online_requested": str("evidence_search" in categories).lower(),
            "targeted_evidence_records": len(rows),
            "search_queries": " | ".join(query_map.get((acc, field), [])),
            "action": (
                "targeted_evidence_then_llm" if "evidence_search" in categories
                else "deterministic_zero_cell_sanitization" if "deterministic_zero_cell" in categories
                else "explicit_structured_mapping" if "structured_mapping" in categories
                else "manual_review"
            ),
        })
    write_tsv(out / "targeted_evidence_plan.tsv", plan_rows, ["accession","field","error_count","categories","online_requested","targeted_evidence_records","search_queries","action"])
    write_tsv(out / "targeted_evidence_records.tsv", (asdict(x) for x in evidence), list(EvidenceRecord.__dataclass_fields__))
    write_tsv(out / "search_queries.tsv", [
        {"accession":acc,"field":field,"query":q}
        for (acc, field), qs in sorted(query_map.items()) for q in qs
    ], ["accession","field","query"])
    write_tsv(out / "crossref_fulltext_links.tsv", crossref_links, ["accession","field","url"])

    targeted_manifest_rows: list[dict[str, str]] = []
    packet_paths: dict[str, str] = {}
    for acc in sorted(selected):
        fields_for_acc = sorted({field for (a, field) in grouped if a == acc and any(x.category == "evidence_search" for x in grouped[(a, field)])})
        if not fields_for_acc:
            continue
        field_records = {field: [r for r in evidence if r.accession == acc and r.field == field] for field in fields_for_acc}
        field_queries = {field: query_map.get((acc, field), []) for field in fields_for_acc}
        if not any(field_records.values()):
            continue
        packet = packet_text(acc, field_records, field_queries)
        path = packet_dir / f"{acc}.targeted_evidence.txt"
        path.write_text(packet, encoding="utf-8")
        packet_paths[acc] = str(path)
        rows = pub_by.get(acc, [])
        doi, pmid, pmcid, title = publication_identity(rows)
        targeted_manifest_rows.append({
            "accession": acc,
            "publication_index": "targeted_v013",
            "publication_doi": doi,
            "publication_pmid": pmid,
            "publication_pmcid": pmcid,
            "publication_title": title or f"Targeted evidence for {acc}",
            "publication_content_status": "available",
            "publication_content_kind": "text",
            "publication_content_path": str(path),
            "publication_content_text_path": str(path),
            "publication_content_source": VERSION,
            "publication_content_chars": str(len(packet)),
        })

    fields = manifest_fields(pub_rows + targeted_manifest_rows)
    write_tsv(out / "targeted_publication_manifest.tsv", targeted_manifest_rows, fields)
    write_tsv(out / "augmented_publication_manifest.tsv", [*pub_rows, *targeted_manifest_rows], fields)

    def accs_for(category: str) -> list[str]:
        return sorted(acc for acc, cats in category_by_acc.items() if category in cats)
    for name, vals in {
        "targeted_model_accessions.txt": accs_for("evidence_search"),
        "deterministic_zero_cell_accessions.txt": accs_for("deterministic_zero_cell"),
        "structured_mapping_accessions.txt": accs_for("structured_mapping"),
    }.items():
        (out / name).write_text("".join(f"{x}\n" for x in vals), encoding="utf-8")

    summary = {
        "version": VERSION,
        "policy_version": POLICY_VERSION,
        "accessions_considered": len(selected),
        "error_issues": len(issues),
        "field_plans": len(grouped),
        "category_counts": dict(sorted(Counter(x.category for x in issues).items())),
        "targeted_model_accessions": accs_for("evidence_search"),
        "deterministic_zero_cell_accessions": accs_for("deterministic_zero_cell"),
        "structured_mapping_accessions": accs_for("structured_mapping"),
        "targeted_evidence_records": len(evidence),
        "targeted_packets": len(targeted_manifest_rows),
        "online": args.online,
        "non_generative_mapping": True,
        "gt_runtime_truth_used": False,
        "outputs": {
            "plan": str(out / "targeted_evidence_plan.tsv"),
            "records": str(out / "targeted_evidence_records.tsv"),
            "queries": str(out / "search_queries.tsv"),
            "targeted_manifest": str(out / "targeted_publication_manifest.tsv"),
            "augmented_manifest": str(out / "augmented_publication_manifest.tsv"),
            "packets": str(packet_dir),
        },
    }
    (out / "targeted_evidence_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
