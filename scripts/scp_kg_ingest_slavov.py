#!/usr/bin/env python3
"""Ingest the Slavov Lab single-cell-proteomics web ecosystem into the global SCP graph.

The Slavov SCP site is treated as a community knowledge/auditing source.  It can discover and
corroborate methods, publications, repositories, protocols and analysis resources, but its claims are
not automatically promoted to accession-specific SDRF truth.

If the main site is unavailable, an optional generic GitHub-organization fallback inventories
SlavovLab repositories whose metadata/README indicate single-cell proteomics relevance.  Fallback
records retain their distinct provenance and are never represented as website content.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import deque
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urldefrag

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from scp_knowledge_graph import GraphStore, NodeRef, DOI_RE, MSV_RE, PXD_RE, norm  # noqa: E402

VERSION = "pride-scp-slavov-knowledge-source-ingest-v0.1"
DEFAULT_ROOT = "https://scp.slavovlab.net/"
TECH_PATTERNS: tuple[tuple[str,re.Pattern[str]], ...] = (
    ("SCoPE-MS", re.compile(r"(?i)\bSCoPE[- ]?MS\b")),
    ("SCoPE2", re.compile(r"(?i)\bSCoPE\s*2\b|\bSCoPE2\b")),
    ("pSCoPE", re.compile(r"(?i)\bpSCoPE\b")),
    ("plexDIA", re.compile(r"(?i)\bplexDIA\b")),
    ("nPOP", re.compile(r"(?i)\bnPOP\b")),
    ("mPOP", re.compile(r"(?i)\bmPOP\b")),
    ("SureQuant", re.compile(r"(?i)\bSureQuant\b")),
    ("TMTpro", re.compile(r"(?i)\bTMTpro\b")),
    ("TMT", re.compile(r"(?i)\bTMT\b|tandem mass tag")),
    ("DIA", re.compile(r"(?i)(?:^|[^A-Za-z])DIA(?:[^A-Za-z]|$)|data[- ]independent acquisition")),
    ("DDA", re.compile(r"(?i)(?:^|[^A-Za-z])DDA(?:[^A-Za-z]|$)|data[- ]dependent acquisition")),
    ("CellenONE", re.compile(r"(?i)\bCellenONE\b|\bCellenion\b")),
)


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str,str]]=[]
        self.text_parts: list[str]=[]
        self.title=""
        self._in_title=False
        self._skip=0
        self.meta_description=""
    def handle_starttag(self, tag: str, attrs: list[tuple[str,str|None]]) -> None:
        d={k.lower():(v or "") for k,v in attrs}
        if tag.lower()=="a" and d.get("href"):
            self.links.append((d["href"],d.get("title", "")))
        if tag.lower()=="title": self._in_title=True
        if tag.lower() in {"script","style","noscript"}: self._skip+=1
        if tag.lower()=="meta" and d.get("name","").lower()=="description":
            self.meta_description=d.get("content","")
    def handle_endtag(self, tag: str) -> None:
        if tag.lower()=="title": self._in_title=False
        if tag.lower() in {"script","style","noscript"} and self._skip: self._skip-=1
    def handle_data(self, data: str) -> None:
        if self._skip: return
        if self._in_title: self.title += data
        if norm(data): self.text_parts.append(data)
    @property
    def text(self) -> str:
        return norm(" ".join(self.text_parts))


def canonical_url(url: str) -> str:
    clean,_=urldefrag(url)
    p=urlparse(clean)
    scheme=p.scheme.lower() or "https"
    host=p.netloc.lower()
    path=re.sub(r"/{2,}","/",p.path or "/")
    if path!="/": path=path.rstrip("/")
    return f"{scheme}://{host}{path}" + (f"?{p.query}" if p.query else "")


def source_snippet(text: str, match: re.Match[str], radius: int=220) -> str:
    return norm(text[max(0,match.start()-radius):min(len(text),match.end()+radius)])


def external_resource_type(url: str) -> str:
    host=urlparse(url).netloc.lower()
    if "github.com" in host: return "AnalysisRepository"
    if "zenodo.org" in host: return "DataResource"
    if "figshare.com" in host: return "DataResource"
    if "protocols.io" in host: return "ProtocolResource"
    if "massive.ucsd.edu" in host: return "RepositoryResource"
    if "proteomexchange" in host or "proteomecentral" in host: return "RepositoryResource"
    if "drive.google.com" in host or "docs.google.com" in host: return "DataResource"
    if "youtube.com" in host or "youtu.be" in host: return "TrainingResource"
    return "ExternalResource"


def ingest_page(store: GraphStore, url: str, html: str, retrieved_at: str) -> tuple[list[str],dict[str,int]]:
    parser=PageParser(); parser.feed(html)
    text=parser.text
    sha=hashlib.sha256(html.encode("utf-8",errors="replace")).hexdigest()
    src=store.source("scp_slavovlab_web",url,title=norm(parser.title),retrieved_at=retrieved_at,content_sha256=sha,trust_class="community_resource",metadata={"description":norm(parser.meta_description),"ingestor":VERSION})
    page=NodeRef("CommunityResourcePage",url,norm(parser.title) or url,{"provider":"Slavov Lab SCP"})
    store.node(page)
    counts={"pxd_mentions":0,"msv_mentions":0,"doi_mentions":0,"external_links":0,"technology_mentions":0}

    for m in PXD_RE.finditer(text):
        acc=m.group(0).upper()
        store.claim(page,"MENTIONS_ACCESSION",src,object_ref=NodeRef("Accession",acc,acc),extractor=VERSION,confidence=0.8,status="asserted",evidence_locator="page text",evidence_text=source_snippet(text,m))
        counts["pxd_mentions"]+=1
    for m in MSV_RE.finditer(text):
        msv=m.group(0).upper()
        store.claim(page,"MENTIONS_MASSIVE_ACCESSION",src,object_ref=NodeRef("RepositoryAccession",msv,msv,{"repository":"MassIVE"}),extractor=VERSION,confidence=0.85,status="asserted",evidence_locator="page text",evidence_text=source_snippet(text,m))
        counts["msv_mentions"]+=1
    seen_doi=set()
    for m in DOI_RE.finditer(text):
        doi=m.group(0).rstrip(".,;)").lower()
        if doi in seen_doi: continue
        seen_doi.add(doi)
        store.claim(page,"MENTIONS_PUBLICATION",src,object_ref=NodeRef("Publication",f"doi:{doi}",doi,{"doi":doi}),extractor=VERSION,confidence=0.85,status="asserted",evidence_locator="page text",evidence_text=source_snippet(text,m))
        counts["doi_mentions"]+=1
    for name,pat in TECH_PATTERNS:
        m=pat.search(text)
        if not m: continue
        store.claim(page,"DESCRIBES_TECHNOLOGY",src,object_ref=NodeRef("Technology",name.lower(),name),extractor=VERSION,confidence=0.75,status="asserted",evidence_locator="page text",evidence_text=source_snippet(text,m))
        counts["technology_mentions"]+=1

    internal=[]; seen_links=set()
    for href,title in parser.links:
        absolute=canonical_url(urljoin(url,href))
        if absolute in seen_links: continue
        seen_links.add(absolute)
        p=urlparse(absolute)
        if p.scheme not in {"http","https"}: continue
        if p.netloc.lower()==urlparse(url).netloc.lower():
            internal.append(absolute)
        else:
            rtype=external_resource_type(absolute)
            store.claim(page,"LINKS_TO_RESOURCE",src,object_ref=NodeRef(rtype,absolute,norm(title) or absolute),extractor=VERSION,confidence=0.9,status="asserted",evidence_locator="anchor",evidence_text=norm(title) or absolute)
            counts["external_links"]+=1
    return internal,counts


def crawl_site(store: GraphStore, root_url: str, max_pages: int, max_depth: int, timeout: int, delay: float) -> dict[str,Any]:
    session=requests.Session()
    session.headers.update({"User-Agent":"PRIDE-SCP knowledge graph crawler/0.1 (research provenance crawler)","Accept":"text/html,application/xhtml+xml"})
    q=deque([(canonical_url(root_url),0)])
    seen=set(); pages=0; errors=[]; totals={"pxd_mentions":0,"msv_mentions":0,"doi_mentions":0,"external_links":0,"technology_mentions":0}
    while q and pages<max_pages:
        url,depth=q.popleft()
        if url in seen or depth>max_depth: continue
        seen.add(url)
        try:
            resp=session.get(url,timeout=timeout,allow_redirects=True)
            if resp.status_code!=200:
                errors.append({"url":url,"status":resp.status_code}); continue
            ctype=resp.headers.get("content-type","")
            if "html" not in ctype.lower() and "<html" not in resp.text[:1000].lower():
                continue
            retrieved=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())
            links,counts=ingest_page(store,canonical_url(resp.url),resp.text,retrieved)
            pages+=1
            for k,v in counts.items(): totals[k]+=v
            if depth<max_depth:
                for nxt in links:
                    if nxt not in seen: q.append((nxt,depth+1))
            if delay: time.sleep(delay)
        except Exception as exc:
            errors.append({"url":url,"error":f"{type(exc).__name__}:{exc}"})
    return {"root":root_url,"pages_fetched":pages,"pages_seen":len(seen),"errors":errors[:50],**totals}


def github_org_fallback(store: GraphStore, org: str, timeout: int, max_repos: int=100) -> dict[str,Any]:
    """Inventory SCP-relevant repositories from an organization when the web site cannot be crawled."""
    s=requests.Session(); s.headers.update({"User-Agent":"PRIDE-SCP knowledge graph crawler/0.1","Accept":"application/vnd.github+json"})
    url=f"https://api.github.com/orgs/{org}/repos?per_page=100&type=public"
    repos=[]; errors=[]
    try:
        r=s.get(url,timeout=timeout); r.raise_for_status(); payload=r.json()
    except Exception as exc:
        return {"organization":org,"repositories_ingested":0,"errors":[f"{type(exc).__name__}:{exc}"]}
    for repo in payload[:max_repos]:
        name=norm(repo.get("name")); desc=norm(repo.get("description")); home=norm(repo.get("homepage")); html=norm(repo.get("html_url"))
        readme=""
        try:
            rr=s.get(f"https://api.github.com/repos/{org}/{name}/readme",headers={"Accept":"application/vnd.github.raw+json"},timeout=timeout)
            if rr.status_code==200: readme=rr.text[:200000]
        except Exception:
            pass
        hay=norm(" ".join([name,desc,home,readme]))
        if not re.search(r"(?i)single[- ]cell|single[- ]nucleus|SCoPE|nPOP|plexDIA|pSCoPE|proteomic",hay):
            continue
        src=store.source("slavovlab_github",html or f"https://github.com/{org}/{name}",title=name,trust_class="analysis_repository",metadata={"description":desc,"homepage":home,"fallback":True})
        node=NodeRef("AnalysisRepository",html or f"https://github.com/{org}/{name}",name,{"organization":org})
        for tech,pat in TECH_PATTERNS:
            m=pat.search(hay)
            if m:
                store.claim(node,"DESCRIBES_TECHNOLOGY",src,object_ref=NodeRef("Technology",tech.lower(),tech),extractor=VERSION,confidence=0.8,status="asserted",evidence_locator="repository metadata/README",evidence_text=source_snippet(hay,m))
        for m in PXD_RE.finditer(hay):
            acc=m.group(0).upper(); store.claim(node,"MENTIONS_ACCESSION",src,object_ref=NodeRef("Accession",acc,acc),extractor=VERSION,confidence=0.8,status="asserted",evidence_locator="repository metadata/README",evidence_text=source_snippet(hay,m))
        for m in MSV_RE.finditer(hay):
            msv=m.group(0).upper(); store.claim(node,"MENTIONS_MASSIVE_ACCESSION",src,object_ref=NodeRef("RepositoryAccession",msv,msv,{"repository":"MassIVE"}),extractor=VERSION,confidence=0.85,status="asserted",evidence_locator="repository metadata/README",evidence_text=source_snippet(hay,m))
        if home:
            store.claim(node,"HAS_PROJECT_WEBSITE",src,object_ref=NodeRef("ExternalResource",home,home),extractor=VERSION,confidence=0.95,status="asserted",evidence_locator="GitHub homepage",evidence_text=home)
        repos.append(name)
    return {"organization":org,"repositories_ingested":len(repos),"repositories":repos,"errors":errors}


def run(args: argparse.Namespace) -> int:
    if not args.db.is_file(): raise SystemExit(f"knowledge graph database not found: {args.db}")
    with GraphStore(args.db) as store:
        store.add_run("scp_kg_ingest_slavov",VERSION,{"root":args.root_url})
        crawl=crawl_site(store,args.root_url,args.max_pages,args.max_depth,args.timeout,args.delay)
        fallback={}
        if args.github_fallback and crawl["pages_fetched"]==0:
            fallback=github_org_fallback(store,args.github_org,args.timeout,args.max_github_repos)
        store.export_tsv(args.output/"exports")
        summary={"ingestor_version":VERSION,"crawl":crawl,"github_fallback":fallback,"graph":store.summary(),"policy":"community source claims are not automatically promoted to accession-specific SDRF facts"}
        args.output.mkdir(parents=True,exist_ok=True)
        (args.output/"slavov_source_ingest_summary.json").write_text(json.dumps(summary,indent=2)+"\n")
        print(json.dumps(summary,indent=2))
    return 0


def self_test() -> None:
    import tempfile
    html='''<html><head><title>SCP example</title></head><body><p>nPOP single-cell proteomics data are in MSV000099999 and PXD900200. See doi:10.1000/example.</p><a href="https://github.com/example/repo">code</a><a href="/protocols">protocols</a></body></html>'''
    with tempfile.TemporaryDirectory() as td:
        with GraphStore(Path(td)/"kg.sqlite") as g:
            links,counts=ingest_page(g,"https://scp.slavovlab.net/example",html,"2026-01-01T00:00:00Z")
            assert links==["https://scp.slavovlab.net/protocols"]
            assert counts["pxd_mentions"]==1 and counts["msv_mentions"]==1 and counts["doi_mentions"]==1
            s=g.summary(); assert s["claims"]>=5
    print("scp_kg_ingest_slavov self-test: PASS")


def main() -> int:
    p=argparse.ArgumentParser()
    p.add_argument("--db",type=Path)
    p.add_argument("--output",type=Path)
    p.add_argument("--root-url",default=DEFAULT_ROOT)
    p.add_argument("--max-pages",type=int,default=80)
    p.add_argument("--max-depth",type=int,default=2)
    p.add_argument("--timeout",type=int,default=30)
    p.add_argument("--delay",type=float,default=0.15)
    p.add_argument("--github-fallback",action="store_true")
    p.add_argument("--github-org",default="SlavovLab")
    p.add_argument("--max-github-repos",type=int,default=100)
    p.add_argument("--self-test",action="store_true")
    args=p.parse_args()
    if args.self_test: self_test(); return 0
    if not args.db or not args.output: raise SystemExit("--db and --output are required")
    return run(args)

if __name__=="__main__":
    raise SystemExit(main())
