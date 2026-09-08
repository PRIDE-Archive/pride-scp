#!/usr/bin/env python3
"""Provenance-aware global knowledge/evidence graph for single-cell proteomics.

This module is intentionally accession-agnostic.  Accessions are graph entities and query scopes,
never runtime scientific configuration.  Extractors write *claims* with provenance.  Canonical
facts/edges are a separate layer so uncertain or conflicting evidence can coexist without being
silently promoted to truth.

The SQLite representation is deliberately lightweight and dependency-free.  It can later be
exported to RDF/GraphML/Neo4j without changing the production inference contract.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

VERSION = "pride-scp-global-knowledge-graph-v0.1"
PXD_RE = re.compile(r"\bPXD\d{6,}\b", re.I)
MSV_RE = re.compile(r"\bMSV\d{6,}\b", re.I)
DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def stable_id(prefix: str, *parts: Any) -> str:
    payload = "\x1f".join(norm(x) for x in parts)
    return f"{prefix}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def json_text(value: Any) -> str:
    return json.dumps(value if value is not None else {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def split_list(value: str) -> list[str]:
    value = norm(value)
    if not value:
        return []
    if value.startswith("["):
        try:
            x = json.loads(value)
            if isinstance(x, list):
                return [norm(v) for v in x if norm(v)]
        except Exception:
            pass
    return [x.strip() for x in re.split(r"\s*[|,;]\s*", value) if x.strip()]


@dataclass(frozen=True)
class NodeRef:
    node_type: str
    canonical_key: str
    label: str = ""
    attrs: dict[str, Any] | None = None


class GraphStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def __enter__(self) -> "GraphStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.conn.commit()
        else:
            self.conn.rollback()
        self.conn.close()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS graph_run (
              run_id TEXT PRIMARY KEY,
              created_at TEXT NOT NULL,
              producer TEXT NOT NULL,
              version TEXT NOT NULL,
              metadata_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS source (
              source_id TEXT PRIMARY KEY,
              source_type TEXT NOT NULL,
              uri TEXT NOT NULL,
              title TEXT NOT NULL DEFAULT '',
              retrieved_at TEXT NOT NULL DEFAULT '',
              content_sha256 TEXT NOT NULL DEFAULT '',
              trust_class TEXT NOT NULL DEFAULT 'unclassified',
              scope_accession TEXT NOT NULL DEFAULT '',
              metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_source_uri ON source(uri);
            CREATE INDEX IF NOT EXISTS idx_source_scope ON source(scope_accession);

            CREATE TABLE IF NOT EXISTS node (
              node_id TEXT PRIMARY KEY,
              node_type TEXT NOT NULL,
              canonical_key TEXT NOT NULL,
              label TEXT NOT NULL DEFAULT '',
              attrs_json TEXT NOT NULL DEFAULT '{}',
              UNIQUE(node_type, canonical_key)
            );
            CREATE INDEX IF NOT EXISTS idx_node_type ON node(node_type);

            CREATE TABLE IF NOT EXISTS claim (
              claim_id TEXT PRIMARY KEY,
              subject_id TEXT NOT NULL REFERENCES node(node_id),
              predicate TEXT NOT NULL,
              object_id TEXT REFERENCES node(node_id),
              literal_value TEXT NOT NULL DEFAULT '',
              literal_datatype TEXT NOT NULL DEFAULT '',
              scope_accession TEXT NOT NULL DEFAULT '',
              branch_scope TEXT NOT NULL DEFAULT '',
              source_id TEXT NOT NULL REFERENCES source(source_id),
              extractor TEXT NOT NULL,
              confidence REAL NOT NULL DEFAULT 0.0,
              status TEXT NOT NULL DEFAULT 'asserted',
              evidence_locator TEXT NOT NULL DEFAULT '',
              evidence_text TEXT NOT NULL DEFAULT '',
              attrs_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_claim_subject ON claim(subject_id, predicate);
            CREATE INDEX IF NOT EXISTS idx_claim_scope ON claim(scope_accession);
            CREATE INDEX IF NOT EXISTS idx_claim_source ON claim(source_id);
            CREATE INDEX IF NOT EXISTS idx_claim_status ON claim(status);

            CREATE TABLE IF NOT EXISTS edge (
              edge_id TEXT PRIMARY KEY,
              subject_id TEXT NOT NULL REFERENCES node(node_id),
              predicate TEXT NOT NULL,
              object_id TEXT REFERENCES node(node_id),
              literal_value TEXT NOT NULL DEFAULT '',
              literal_datatype TEXT NOT NULL DEFAULT '',
              scope_accession TEXT NOT NULL DEFAULT '',
              branch_scope TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'accepted',
              confidence REAL NOT NULL DEFAULT 0.0,
              resolution_method TEXT NOT NULL DEFAULT '',
              attrs_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_edge_subject ON edge(subject_id, predicate);
            CREATE INDEX IF NOT EXISTS idx_edge_scope ON edge(scope_accession);

            CREATE TABLE IF NOT EXISTS edge_claim (
              edge_id TEXT NOT NULL REFERENCES edge(edge_id) ON DELETE CASCADE,
              claim_id TEXT NOT NULL REFERENCES claim(claim_id) ON DELETE CASCADE,
              PRIMARY KEY(edge_id, claim_id)
            );
            """
        )

    def add_run(self, producer: str, version: str, metadata: dict[str, Any] | None = None) -> str:
        run_id = stable_id("run", producer, version, utc_now(), json_text(metadata or {}))
        self.conn.execute(
            "INSERT OR IGNORE INTO graph_run(run_id,created_at,producer,version,metadata_json) VALUES(?,?,?,?,?)",
            (run_id, utc_now(), producer, version, json_text(metadata or {})),
        )
        return run_id

    def source(
        self,
        source_type: str,
        uri: str,
        *,
        title: str = "",
        content_sha256: str = "",
        trust_class: str = "unclassified",
        scope_accession: str = "",
        retrieved_at: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        # A source revision is identified by URI + content hash.  When content is unavailable the URI
        # itself remains stable, which is appropriate for metadata-only evidence.
        sid = stable_id("src", source_type, uri, content_sha256)
        self.conn.execute(
            """INSERT INTO source(source_id,source_type,uri,title,retrieved_at,content_sha256,trust_class,scope_accession,metadata_json)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source_id) DO UPDATE SET
                 title=CASE WHEN excluded.title<>'' THEN excluded.title ELSE source.title END,
                 retrieved_at=CASE WHEN excluded.retrieved_at<>'' THEN excluded.retrieved_at ELSE source.retrieved_at END,
                 trust_class=CASE WHEN excluded.trust_class<>'unclassified' THEN excluded.trust_class ELSE source.trust_class END,
                 scope_accession=CASE WHEN excluded.scope_accession<>'' THEN excluded.scope_accession ELSE source.scope_accession END,
                 metadata_json=CASE WHEN excluded.metadata_json<>'{}' THEN excluded.metadata_json ELSE source.metadata_json END
            """,
            (sid, source_type, uri, title, retrieved_at, content_sha256, trust_class, scope_accession.upper(), json_text(metadata or {})),
        )
        return sid

    def node(self, ref: NodeRef) -> str:
        key = norm(ref.canonical_key)
        nid = stable_id("node", ref.node_type, key)
        self.conn.execute(
            """INSERT INTO node(node_id,node_type,canonical_key,label,attrs_json) VALUES(?,?,?,?,?)
               ON CONFLICT(node_type,canonical_key) DO UPDATE SET
                 label=CASE WHEN excluded.label<>'' THEN excluded.label ELSE node.label END,
                 attrs_json=CASE WHEN excluded.attrs_json<>'{}' THEN excluded.attrs_json ELSE node.attrs_json END
            """,
            (nid, ref.node_type, key, norm(ref.label), json_text(ref.attrs or {})),
        )
        return nid

    def claim(
        self,
        subject: NodeRef,
        predicate: str,
        source_id: str,
        *,
        object_ref: NodeRef | None = None,
        literal_value: Any = "",
        literal_datatype: str = "",
        scope_accession: str = "",
        branch_scope: str = "",
        extractor: str,
        confidence: float,
        status: str = "asserted",
        evidence_locator: str = "",
        evidence_text: str = "",
        attrs: dict[str, Any] | None = None,
    ) -> str:
        sid = self.node(subject)
        oid = self.node(object_ref) if object_ref else None
        literal = norm(literal_value)
        cid = stable_id(
            "claim", sid, predicate, oid or "", literal, scope_accession.upper(), branch_scope,
            source_id, extractor, evidence_locator, evidence_text[:512],
        )
        self.conn.execute(
            """INSERT OR IGNORE INTO claim(
               claim_id,subject_id,predicate,object_id,literal_value,literal_datatype,scope_accession,branch_scope,
               source_id,extractor,confidence,status,evidence_locator,evidence_text,attrs_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cid, sid, predicate, oid, literal, literal_datatype, scope_accession.upper(), branch_scope,
             source_id, extractor, float(confidence), status, evidence_locator, norm(evidence_text)[:4000], json_text(attrs or {})),
        )
        return cid

    def promote_edge(
        self,
        claim_ids: Iterable[str],
        *,
        status: str = "accepted",
        confidence: float | None = None,
        resolution_method: str,
        attrs: dict[str, Any] | None = None,
    ) -> str:
        ids = sorted(set(claim_ids))
        if not ids:
            raise ValueError("at least one claim is required")
        rows = [self.conn.execute("SELECT * FROM claim WHERE claim_id=?", (cid,)).fetchone() for cid in ids]
        if any(r is None for r in rows):
            raise ValueError("unknown claim id")
        base = rows[0]
        signature = (base["subject_id"], base["predicate"], base["object_id"], base["literal_value"], base["literal_datatype"], base["scope_accession"], base["branch_scope"])
        for row in rows[1:]:
            other = (row["subject_id"], row["predicate"], row["object_id"], row["literal_value"], row["literal_datatype"], row["scope_accession"], row["branch_scope"])
            if other != signature:
                raise ValueError("claims do not assert the same fact")
        conf = float(confidence if confidence is not None else max(float(r["confidence"]) for r in rows))
        eid = stable_id("edge", *[x or "" for x in signature], status)
        self.conn.execute(
            """INSERT INTO edge(edge_id,subject_id,predicate,object_id,literal_value,literal_datatype,scope_accession,branch_scope,status,confidence,resolution_method,attrs_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(edge_id) DO UPDATE SET confidence=MAX(edge.confidence, excluded.confidence), resolution_method=excluded.resolution_method, attrs_json=excluded.attrs_json""",
            (eid, *signature, status, conf, resolution_method, json_text(attrs or {})),
        )
        self.conn.executemany("INSERT OR IGNORE INTO edge_claim(edge_id,claim_id) VALUES(?,?)", [(eid, cid) for cid in ids])
        return eid

    def export_tsv(self, output: Path) -> None:
        output.mkdir(parents=True, exist_ok=True)
        tables = {
            "sources.tsv": ("source", ["source_id","source_type","uri","title","retrieved_at","content_sha256","trust_class","scope_accession","metadata_json"]),
            "nodes.tsv": ("node", ["node_id","node_type","canonical_key","label","attrs_json"]),
            "claims.tsv": ("claim", ["claim_id","subject_id","predicate","object_id","literal_value","literal_datatype","scope_accession","branch_scope","source_id","extractor","confidence","status","evidence_locator","evidence_text","attrs_json"]),
            "edges.tsv": ("edge", ["edge_id","subject_id","predicate","object_id","literal_value","literal_datatype","scope_accession","branch_scope","status","confidence","resolution_method","attrs_json"]),
            "edge_claims.tsv": ("edge_claim", ["edge_id","claim_id"]),
        }
        for name, (table, fields) in tables.items():
            with (output/name).open("w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=fields, delimiter="\t", extrasaction="ignore")
                w.writeheader()
                for row in self.conn.execute(f"SELECT {','.join(fields)} FROM {table} ORDER BY 1"):
                    w.writerow(dict(row))

    def accession_view(self, accession: str) -> dict[str, Any]:
        acc = accession.upper()
        accession_node = self.conn.execute("SELECT node_id FROM node WHERE node_type='Accession' AND UPPER(canonical_key)=?", (acc,)).fetchone()
        node_ids: set[str] = set()
        if accession_node:
            node_ids.add(accession_node[0])
        claims = [dict(r) for r in self.conn.execute("SELECT * FROM claim WHERE scope_accession=? ORDER BY predicate,claim_id", (acc,))]
        edges = [dict(r) for r in self.conn.execute("SELECT * FROM edge WHERE scope_accession=? ORDER BY predicate,edge_id", (acc,))]
        for r in claims + edges:
            node_ids.add(r["subject_id"])
            if r.get("object_id"):
                node_ids.add(r["object_id"])
        # Include cross-accession claims/edges directly incident on the accession node.
        if accession_node:
            aid = accession_node[0]
            for table, dest in (("claim", claims), ("edge", edges)):
                seen = {r[f"{table}_id"] for r in dest}
                for r in self.conn.execute(f"SELECT * FROM {table} WHERE subject_id=? OR object_id=? ORDER BY predicate", (aid, aid)):
                    d = dict(r)
                    if d[f"{table}_id"] not in seen:
                        dest.append(d)
                    node_ids.add(d["subject_id"])
                    if d.get("object_id"):
                        node_ids.add(d["object_id"])
        nodes = []
        for nid in sorted(node_ids):
            r = self.conn.execute("SELECT * FROM node WHERE node_id=?", (nid,)).fetchone()
            if r: nodes.append(dict(r))
        source_ids = sorted({r["source_id"] for r in claims})
        sources = []
        for sid in source_ids:
            r = self.conn.execute("SELECT * FROM source WHERE source_id=?", (sid,)).fetchone()
            if r: sources.append(dict(r))
        return {"accession": acc, "nodes": nodes, "claims": claims, "edges": edges, "sources": sources}

    def export_accession_views(self, accessions: Iterable[str], output: Path) -> None:
        output.mkdir(parents=True, exist_ok=True)
        for acc in sorted({a.upper() for a in accessions if a}):
            (output/f"{acc}.json").write_text(json.dumps(self.accession_view(acc), indent=2, ensure_ascii=False)+"\n", encoding="utf-8")

    def summary(self) -> dict[str, Any]:
        count = lambda table: self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return {
            "graph_version": VERSION,
            "sources": count("source"),
            "nodes": count("node"),
            "claims": count("claim"),
            "accepted_edges": self.conn.execute("SELECT COUNT(*) FROM edge WHERE status='accepted'").fetchone()[0],
            "edges_total": count("edge"),
            "accession_scoped_claims": self.conn.execute("SELECT COUNT(*) FROM claim WHERE scope_accession<>''").fetchone()[0],
            "claim_status_counts": {r[0]: r[1] for r in self.conn.execute("SELECT status,COUNT(*) FROM claim GROUP BY status ORDER BY status")},
            "source_type_counts": {r[0]: r[1] for r in self.conn.execute("SELECT source_type,COUNT(*) FROM source GROUP BY source_type ORDER BY source_type")},
            "node_type_counts": {r[0]: r[1] for r in self.conn.execute("SELECT node_type,COUNT(*) FROM node GROUP BY node_type ORDER BY node_type")},
        }


def read_accessions(path: Path) -> list[str]:
    vals = []
    for line in path.read_text(errors="replace").splitlines():
        x = line.strip().upper()
        if x:
            vals.append(x)
    return sorted(set(vals))


def import_claim_jsonl(store: GraphStore, path: Path, default_extractor: str = "external_claim_import") -> int:
    """Import source-grounded claims emitted by a small LLM or another extractor.

    This is intentionally a transport contract, not an LLM implementation.  The model must emit
    source URI + evidence text/location; claims without provenance are rejected.
    """
    n = 0
    for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), start=1):
        if not line.strip():
            continue
        obj = json.loads(line)
        source_uri = norm(obj.get("source_uri"))
        if not source_uri:
            raise ValueError(f"{path}:{lineno}: source_uri is required")
        evidence = norm(obj.get("evidence_text"))
        locator = norm(obj.get("evidence_locator"))
        if not evidence and not locator:
            raise ValueError(f"{path}:{lineno}: evidence_text or evidence_locator is required")
        subj = obj.get("subject") or {}
        if not subj.get("type") or not subj.get("key"):
            raise ValueError(f"{path}:{lineno}: subject.type/key are required")
        object_ref = None
        if obj.get("object"):
            o = obj["object"]
            object_ref = NodeRef(o["type"], o["key"], o.get("label", ""), o.get("attrs"))
        source_id = store.source(
            obj.get("source_type", "external_claim_source"), source_uri,
            title=obj.get("source_title", ""), trust_class=obj.get("trust_class", "unclassified"),
            scope_accession=obj.get("scope_accession", ""), metadata=obj.get("source_metadata") or {},
        )
        store.claim(
            NodeRef(subj["type"], subj["key"], subj.get("label", ""), subj.get("attrs")),
            obj["predicate"], source_id, object_ref=object_ref,
            literal_value=obj.get("literal_value", ""), literal_datatype=obj.get("literal_datatype", ""),
            scope_accession=obj.get("scope_accession", ""), branch_scope=obj.get("branch_scope", ""),
            extractor=obj.get("extractor", default_extractor), confidence=float(obj.get("confidence", 0.5)),
            status=obj.get("status", "asserted"), evidence_locator=locator, evidence_text=evidence,
            attrs=obj.get("attrs") or {},
        )
        n += 1
    return n


def self_test() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        db = Path(td)/"kg.sqlite"
        with GraphStore(db) as g:
            g.add_run("fixture", VERSION)
            src = g.source("publication", "doi:10.0000/example", title="Example", trust_class="peer_reviewed", scope_accession="PXD900001")
            acc = NodeRef("Accession", "PXD900001", "PXD900001")
            tech = NodeRef("Technology", "TMTpro18", "TMTpro18")
            c1 = g.claim(acc, "USES_TECHNOLOGY", src, object_ref=tech, scope_accession="PXD900001", extractor="fixture", confidence=0.95, evidence_locator="Methods", evidence_text="single-cell samples used TMTpro18")
            c2 = g.claim(acc, "USES_TECHNOLOGY", src, object_ref=tech, scope_accession="PXD900001", extractor="fixture2", confidence=0.98, evidence_locator="Supplement", evidence_text="TMTpro18 reporter layout")
            g.promote_edge([c1,c2], confidence=0.98, resolution_method="corroborated_source_claims")
            view = g.accession_view("PXD900001")
            assert any(n["node_type"] == "Technology" for n in view["nodes"])
            assert len(view["claims"]) == 2 and len(view["edges"]) == 1
            s = g.summary(); assert s["nodes"] == 2 and s["claims"] == 2 and s["accepted_edges"] == 1
        print("scp_knowledge_graph self-test: PASS")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path)
    p.add_argument("--export", type=Path)
    p.add_argument("--accessions-file", type=Path)
    p.add_argument("--import-claims", type=Path, action="append", default=[])
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()
    if args.self_test:
        self_test(); return 0
    if not args.db:
        raise SystemExit("--db is required")
    with GraphStore(args.db) as g:
        g.add_run("scp_knowledge_graph", VERSION)
        for path in args.import_claims:
            import_claim_jsonl(g, path)
        if args.export:
            g.export_tsv(args.export)
            if args.accessions_file:
                g.export_accession_views(read_accessions(args.accessions_file), args.export/"accession_views")
        print(json.dumps(g.summary(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
