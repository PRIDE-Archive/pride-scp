#!/usr/bin/env python3
"""Build the first global SCP knowledge/evidence graph from existing source-grounded artifacts.

The builder imports authoritative repository metadata, publication associations, and the current
accession-agnostic SDRF evidence graph.  Runtime branch calls are preserved as hypotheses rather than
canonical facts.  This prevents the noisy v0.5.3 branch segmentation from becoming graph truth while
retaining all evidence for later canonicalization.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from scp_knowledge_graph import GraphStore, NodeRef, norm, read_accessions  # noqa: E402
from sdrf_multiplex_evidence_graph import project_json, raw_files  # noqa: E402

VERSION = "pride-scp-global-knowledge-graph-builder-v0.1"


def read_tsv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(errors="replace", newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def first(row: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = norm(row.get(key, ""))
        if value:
            return value
    return ""


def pxd_ref(accession: str) -> NodeRef:
    acc = accession.upper()
    return NodeRef("Accession", acc, acc, {"repository": "ProteomeXchange/PRIDE"})


def raw_ref(accession: str, raw: str) -> NodeRef:
    return NodeRef("RawFile", f"{accession.upper()}::{raw}", raw, {"accession": accession.upper()})


def publication_ref(doi: str, title: str) -> NodeRef:
    doi = doi.lower().strip().rstrip(".,;)")
    if doi:
        return NodeRef("Publication", f"doi:{doi}", title or doi, {"doi": doi})
    key = hashlib.sha256(norm(title).lower().encode()).hexdigest()[:20]
    return NodeRef("Publication", f"titlehash:{key}", title, {})


def project_title(obj: Any) -> str:
    if not isinstance(obj, dict):
        return ""
    for key in ("title", "projectTitle", "name"):
        if norm(obj.get(key)):
            return norm(obj[key])
    return ""


def project_date(obj: Any) -> str:
    vals: list[str] = []
    def walk(x: Any) -> None:
        if isinstance(x, dict):
            for k, v in x.items():
                if isinstance(v, str) and re.search(r"(?i)(?:announce|publish|submission|date)", str(k)) and re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T.*)?", v):
                    vals.append(v[:10])
                walk(v)
        elif isinstance(x, list):
            for y in x: walk(y)
    walk(obj)
    return min(vals) if vals else ""


def ingest_repository(store: GraphStore, accessions: list[str], snapshot: Path) -> dict[str, int]:
    counts = defaultdict(int)
    for acc in accessions:
        obj = project_json(snapshot, acc)
        title = project_title(obj)
        date = project_date(obj)
        source_uri = f"pride-snapshot:{acc}"
        src = store.source("pride_project_metadata", source_uri, title=title, trust_class="repository_primary", scope_accession=acc, metadata={"snapshot": str(snapshot)})
        aref = pxd_ref(acc); store.node(aref)
        if title:
            cid = store.claim(aref, "HAS_PROJECT_TITLE", src, literal_value=title, literal_datatype="string", scope_accession=acc, extractor="repository_metadata", confidence=1.0, evidence_locator="project.title", evidence_text=title)
            store.promote_edge([cid], confidence=1.0, resolution_method="authoritative_repository_metadata")
            counts["project_titles"] += 1
        if date:
            cid = store.claim(aref, "HAS_REPOSITORY_DATE", src, literal_value=date, literal_datatype="date", scope_accession=acc, extractor="repository_metadata", confidence=1.0, evidence_locator="project.date", evidence_text=date)
            store.promote_edge([cid], confidence=1.0, resolution_method="authoritative_repository_metadata")
            counts["project_dates"] += 1
        for raw in raw_files(snapshot, acc):
            rref = raw_ref(acc, raw)
            cid = store.claim(aref, "HAS_RAW_FILE", src, object_ref=rref, scope_accession=acc, extractor="repository_file_inventory", confidence=1.0, evidence_locator="repository RAW inventory", evidence_text=raw)
            store.promote_edge([cid], confidence=1.0, resolution_method="authoritative_repository_file_inventory")
            counts["raw_files"] += 1
        counts["accessions"] += 1
    return dict(counts)


def ingest_publications(store: GraphStore, accessions: set[str], manifest: Path) -> dict[str, int]:
    counts = defaultdict(int)
    for i, row in enumerate(read_tsv(manifest), start=2):
        acc = first(row, "accession", "project_accession", "pxd").upper()
        if acc not in accessions:
            continue
        doi = first(row, "doi", "publication_doi")
        title = first(row, "title", "publication_title")
        pmid = first(row, "pmid", "publication_pmid")
        pmcid = first(row, "pmcid", "publication_pmcid")
        text_path = first(row, "publication_content_text_path", "text_path", "content_text_path")
        pdf_path = first(row, "pdf_path", "publication_pdf_path")
        content_path = Path(text_path) if text_path else None
        content_hash = ""
        if content_path and content_path.is_file():
            content_hash = hashlib.sha256(content_path.read_bytes()).hexdigest()
        uri = f"doi:{doi.lower()}" if doi else (f"pmid:{pmid}" if pmid else f"manifest:{manifest}:{i}")
        src = store.source(
            "publication_manifest", uri, title=title, content_sha256=content_hash,
            trust_class="publication_association", scope_accession="",
            metadata={"manifest":str(manifest),"row":i,"pmid":pmid,"pmcid":pmcid,"text_path":text_path,"pdf_path":pdf_path},
        )
        pref = publication_ref(doi, title)
        cid = store.claim(pxd_ref(acc), "HAS_PUBLICATION", src, object_ref=pref, scope_accession=acc, extractor="publication_manifest", confidence=0.98, evidence_locator=f"{manifest.name}:row{i}", evidence_text=title or doi)
        store.promote_edge([cid], confidence=0.98, resolution_method="accepted_source_grounded_publication_manifest")
        if doi:
            dcid = store.claim(pref, "HAS_DOI", src, literal_value=doi.lower(), literal_datatype="doi", scope_accession=acc, extractor="publication_manifest", confidence=1.0, evidence_locator=f"{manifest.name}:row{i}", evidence_text=doi)
            store.promote_edge([dcid], confidence=1.0, resolution_method="publication_identifier")
        if title:
            tcid = store.claim(pref, "HAS_TITLE", src, literal_value=title, literal_datatype="string", scope_accession=acc, extractor="publication_manifest", confidence=0.99, evidence_locator=f"{manifest.name}:row{i}", evidence_text=title)
            store.promote_edge([tcid], confidence=0.99, resolution_method="publication_metadata")
        counts["publication_rows"] += 1
    return dict(counts)


def ingest_v053_hypotheses(store: GraphStore, accessions: set[str], audit: Path) -> dict[str, int]:
    counts = defaultdict(int)
    branch_path = audit/"branch_contracts.tsv"
    member_path = audit/"branch_file_membership.tsv"
    join_path = audit/"join_evidence.tsv"
    rel_path = audit/"relation_assessments.tsv"
    source_id = store.source("runtime_inference", f"file:{branch_path}", title="Generalized SDRF evidence graph v0.5.3", trust_class="derived_hypothesis", metadata={"audit_root":str(audit),"version":"v0.5.3"})

    for i, row in enumerate(read_tsv(branch_path), start=2):
        acc = first(row,"accession").upper()
        if acc not in accessions: continue
        bid = first(row,"branch_id")
        bref = NodeRef("ExperimentalBranchHypothesis", f"{acc}::{bid}", bid, {"accession":acc})
        store.claim(pxd_ref(acc), "HAS_BRANCH_HYPOTHESIS", source_id, object_ref=bref, scope_accession=acc, branch_scope=bid, extractor="v053_generalized_evidence_graph", confidence=0.55, status="hypothesis", evidence_locator=f"branch_contracts.tsv:row{i}", evidence_text=json.dumps(row, ensure_ascii=False)[:2500])
        for pred, key, node_type in (
            ("HAS_MODALITY_HYPOTHESIS","modality","Modality"),
            ("HAS_ACQUISITION_HYPOTHESIS","acquisition","AcquisitionMethod"),
            ("USES_CHEMISTRY_HYPOTHESIS","chemistry","ReporterChemistry"),
        ):
            for value in re.split(r"\s*[|,]\s*", first(row,key)) if first(row,key) else []:
                if value:
                    store.claim(bref, pred, source_id, object_ref=NodeRef(node_type,value.lower(),value), scope_accession=acc, branch_scope=bid, extractor="v053_generalized_evidence_graph", confidence=0.55, status="hypothesis", evidence_locator=f"branch_contracts.tsv:row{i}", evidence_text=first(row,key))
        for pred, key, role in (
            ("HAS_REPORTER_CHANNEL_HYPOTHESIS","analytical_channels","analytical"),
            ("HAS_REPORTER_CHANNEL_HYPOTHESIS","carrier_channels","carrier"),
            ("HAS_REPORTER_CHANNEL_HYPOTHESIS","blank_channels","blank"),
            ("HAS_REPORTER_CHANNEL_HYPOTHESIS","reference_channels","reference"),
        ):
            for ch in re.split(r"\s*[|,]\s*", first(row,key)) if first(row,key) else []:
                if ch:
                    store.claim(bref, pred, source_id, object_ref=NodeRef("ReporterChannel",ch.upper(),ch.upper(),{"role_hypothesis":role}), scope_accession=acc, branch_scope=bid, extractor="v053_generalized_evidence_graph", confidence=0.6, status="hypothesis", evidence_locator=f"branch_contracts.tsv:row{i}", evidence_text=f"{role}={ch}", attrs={"role":role})
        counts["branch_hypotheses"] += 1

    branch_refs: dict[tuple[str,str], NodeRef] = {}
    for row in read_tsv(branch_path):
        acc=first(row,"accession").upper(); bid=first(row,"branch_id")
        if acc in accessions and bid: branch_refs[(acc,bid)] = NodeRef("ExperimentalBranchHypothesis",f"{acc}::{bid}",bid,{"accession":acc})
    for i,row in enumerate(read_tsv(member_path), start=2):
        acc=first(row,"accession").upper(); bid=first(row,"branch_id")
        if acc not in accessions or (acc,bid) not in branch_refs: continue
        file_name=first(row,"file_name"); category=first(row,"file_category").upper(); membership=first(row,"membership")
        if not file_name: continue
        fref = raw_ref(acc,file_name) if category=="RAW" else NodeRef("RepositoryFile",f"{acc}::{file_name}",file_name,{"accession":acc,"category":category})
        conf = 0.65 if membership=="member" else 0.4
        store.claim(fref,"BELONGS_TO_BRANCH_HYPOTHESIS",source_id,object_ref=branch_refs[(acc,bid)],scope_accession=acc,branch_scope=bid,extractor="v053_file_membership",confidence=conf,status="hypothesis",evidence_locator=f"branch_file_membership.tsv:row{i}",evidence_text=json.dumps(row,ensure_ascii=False)[:1800],attrs={"membership":membership,"score":first(row,"score")})
        counts["membership_hypotheses"] += 1

    # Collapse PSM/spectrum-level repetitions before importing join evidence.  A million rows from the
    # same result table do not constitute a million independent RAW/sample mappings.
    grouped: dict[tuple[str,str,str,str,str,str], dict[str,Any]] = {}
    for row in read_tsv(join_path):
        acc=first(row,"accession").upper()
        if acc not in accessions: continue
        raw=first(row,"repository_raw"); method=first(row,"join_method"); channels=first(row,"channels"); samples=first(row,"sample_tokens"); source_file=first(row,"source_file")
        key=(acc,raw,method,channels,samples,source_file)
        g=grouped.setdefault(key,{"count":0,"confidence":first(row,"join_confidence"),"source_locations":[]})
        g["count"]+=1
        loc=first(row,"source_location")
        if loc and len(g["source_locations"])<8: g["source_locations"].append(loc)
    for (acc,raw,method,channels,samples,source_file),g in grouped.items():
        if not raw: continue
        jsrc = store.source("structured_analysis_artifact", f"v053:{acc}:{source_file}", title=source_file, trust_class="derived_structured_evidence", scope_accession=acc, metadata={"runtime":"v0.5.3"})
        rref=raw_ref(acc,raw)
        conf=0.8 if g["confidence"]=="high" else 0.6
        if channels:
            for ch in re.split(r"\s*[|,]\s*",channels):
                if ch:
                    store.claim(rref,"HAS_CHANNEL_EVIDENCE",jsrc,object_ref=NodeRef("ReporterChannel",ch.upper(),ch.upper()),scope_accession=acc,extractor="v053_join_collapser",confidence=conf,status="asserted",evidence_locator=";".join(g["source_locations"]),evidence_text=f"{source_file}: {raw} channel {ch}",attrs={"collapsed_rows":g["count"],"join_method":method})
        if samples:
            for sample in re.split(r"\s*[|,]\s*",samples):
                if sample:
                    store.claim(rref,"HAS_SAMPLE_TOKEN_EVIDENCE",jsrc,literal_value=sample,scope_accession=acc,extractor="v053_join_collapser",confidence=conf,status="asserted",evidence_locator=";".join(g["source_locations"]),evidence_text=f"{source_file}: {raw} sample {sample}",attrs={"collapsed_rows":g["count"],"join_method":method})
        counts["collapsed_join_facts"] += 1

    for i,row in enumerate(read_tsv(rel_path), start=2):
        a=first(row,"accession_a").upper(); b=first(row,"accession_b").upper()
        if a not in accessions or b not in accessions: continue
        relation=first(row,"relation_class") or "RELATED_DEPOSITION_REVIEW"
        conf_map={"high":0.9,"medium":0.65,"low":0.4}
        store.claim(pxd_ref(a),relation.upper(),source_id,object_ref=pxd_ref(b),scope_accession="",extractor="v053_relation_assessment",confidence=conf_map.get(first(row,"confidence").lower(),0.5),status="hypothesis",evidence_locator=f"relation_assessments.tsv:row{i}",evidence_text=first(row,"reason"),attrs={"raw_jaccard":first(row,"raw_jaccard"),"shared_raws":first(row,"shared_raws"),"generation_policy":first(row,"generation_policy")})
        counts["relation_hypotheses"] += 1
    return dict(counts)


def run(args: argparse.Namespace) -> int:
    accessions = read_accessions(args.accessions_file)
    out=args.output; out.mkdir(parents=True,exist_ok=True)
    db=out/"scp_knowledge_graph.sqlite"
    if args.rebuild and db.exists(): db.unlink()
    with GraphStore(db) as store:
        store.add_run("scp_kg_build",VERSION,{"accessions":len(accessions)})
        summary={
            "builder_version":VERSION,
            "accessions":len(accessions),
            "repository":ingest_repository(store,accessions,args.snapshot),
            "publications":ingest_publications(store,set(accessions),args.publication_manifest),
            "v053_hypotheses":ingest_v053_hypotheses(store,set(accessions),args.v053_audit) if args.v053_audit and args.v053_audit.is_dir() else {},
        }
        store.export_tsv(out/"exports")
        store.export_accession_views(accessions,out/"accession_views")
        summary["graph"]=store.summary()
        summary["outputs"]={"database":str(db),"exports":str(out/"exports"),"accession_views":str(out/"accession_views")}
        (out/"scp_knowledge_graph_build_summary.json").write_text(json.dumps(summary,indent=2)+"\n")
        print(json.dumps(summary,indent=2))
    return 0


def self_test() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root=Path(td); (root/"projects").mkdir(); (root/"files").mkdir()
        acc="PXD900100"
        (root/"projects"/f"{acc}.json").write_text(json.dumps({"title":"Synthetic SCP project","announcementDate":"2026-01-01"}))
        (root/"files"/f"{acc}.json").write_text(json.dumps([{"fileName":"run1.raw","fileCategory":{"name":"RAW"}}]))
        with GraphStore(root/"kg.sqlite") as store:
            counts=ingest_repository(store,[acc],root)
            assert counts["accessions"]==1 and counts["raw_files"]==1
            view=store.accession_view(acc)
            assert any(n["node_type"]=="RawFile" for n in view["nodes"])
            assert any(e["predicate"]=="HAS_RAW_FILE" for e in view["edges"])
    print("scp_kg_build self-test: PASS")


def main() -> int:
    p=argparse.ArgumentParser()
    p.add_argument("--accessions-file",type=Path)
    p.add_argument("--snapshot",type=Path)
    p.add_argument("--publication-manifest",type=Path)
    p.add_argument("--v053-audit",type=Path)
    p.add_argument("--output",type=Path)
    p.add_argument("--rebuild",action="store_true")
    p.add_argument("--self-test",action="store_true")
    args=p.parse_args()
    if args.self_test: self_test(); return 0
    if not all([args.accessions_file,args.snapshot,args.publication_manifest,args.output]):
        raise SystemExit("--accessions-file, --snapshot, --publication-manifest and --output are required")
    return run(args)

if __name__=="__main__":
    raise SystemExit(main())
