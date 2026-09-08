#!/usr/bin/env python3
"""Constrained small-LLM semantic claim extraction for the global SCP knowledge graph.

The model is a semantic reader, not an SDRF generator.  It receives provenance-labelled passages
from PRIDE project metadata and accession-associated publication text and may emit only a fixed
ontology of semantic claims.  File/sample/channel mapping predicates are deliberately absent from
the model schema and are rejected by the graph importer as a second line of defence.

Every emitted graph claim is reconstructed deterministically from cited passage IDs.  The model
cannot choose node types, source trust, source lineage, or free-form evidence text.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

try:
    import requests
except Exception:  # pragma: no cover - exercised on user runtime
    requests = None

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from scp_knowledge_graph import norm, read_accessions, stable_id  # noqa: E402
from sdrf_multiplex_evidence_graph import project_json  # noqa: E402

VERSION = "pride-scp-kg-small-llm-semantic-extractor-v0.1"
PROMPT_VERSION = "scp-kg-semantic-claims-v1"

# The model may emit only these source-semantic facts.  In particular, there are no RAW/file/sample
# mapping predicates here.  Reporter roles are semantic design facts and are allowed only when the
# cited source explicitly states the role/channel relationship.
PREDICATE_OBJECT_TYPES: dict[str, str] = {
    "HAS_MODALITY": "Modality",
    "USES_CHEMISTRY": "ReporterChemistry",
    "USES_TECHNOLOGY": "Technology",
    "HAS_ACQUISITION": "AcquisitionMethod",
    "HAS_ANALYTICAL_CHANNEL": "ReporterChannel",
    "HAS_CARRIER_CHANNEL": "ReporterChannel",
    "HAS_BLANK_CHANNEL": "ReporterChannel",
    "HAS_REFERENCE_CHANNEL": "ReporterChannel",
    "HAS_ISOLATION_METHOD": "IsolationMethod",
    "HAS_SAMPLE_PREPARATION_METHOD": "SamplePreparationMethod",
    "HAS_ORGANISM": "Organism",
    "HAS_ORGANISM_PART": "OrganismPart",
    "HAS_CELL_TYPE": "CellType",
    "HAS_DISEASE": "Disease",
    "HAS_INSTRUMENT": "Instrument",
    "HAS_CLEAVAGE_AGENT": "CleavageAgent",
    "HAS_SAMPLE_TYPE": "SampleType",
    "HAS_RELATION_MODE": "RelationMode",
}
LITERAL_PREDICATES = {
    "HAS_CELLS_PER_WELL": "integer",
    "HAS_MULTIPLEX_SIZE": "integer",
}
ALLOWED_PREDICATES = tuple(sorted(set(PREDICATE_OBJECT_TYPES) | set(LITERAL_PREDICATES)))

# Broad source-selection anchors.  These are retrieval hints, not scientific inference rules.
ANCHOR_GROUPS = {
    "single_cell": [
        "single-cell", "single cell", "single cells", "single-cell proteom", "single cell proteom",
        "cellenone", "cellenone", "facs", "flow cyt", "cell sort", "single neuron", "single oocyte",
        "single zygote", "single blastomere", "single fiber", "single fibre",
    ],
    "multiplex": [
        "tmt", "tmtpro", "reporter", "carrier", "reference channel", "blank channel", "plex",
        "isobaric", "labelled", "labeled",
    ],
    "acquisition": [
        "data-independent", "data independent", "dia", "data-dependent", "data dependent", "dda",
        "prm", "surequant", "dia-pasef", "diapasef", "mass spectrom", "orbitrap", "tims",
    ],
    "preparation": [
        "sample preparation", "lysis", "digestion", "trypsin", "nanopots", "npop", "mpop",
        "droplet", "microfluid", "laser capture", "microdissection", "isolation", "sorting",
    ],
    "biology": [
        "organism", "mouse", "human", "rat", "xenopus", "tissue", "cell type", "disease",
        "embryo", "zygote", "oocyte", "neuron", "cardiomyocyte",
    ],
}
SECTION_BONUS = ("method", "experimental", "sample preparation", "proteomic", "mass spectrom", "data acquisition", "materials")
REFERENCES_HEADINGS = {"references", "bibliography", "literature cited"}


@dataclass(frozen=True)
class Passage:
    passage_id: str
    source_kind: str
    source_uri: str
    source_title: str
    source_locator: str
    text: str
    content_sha256: str
    trust_class: str
    lineage_uri: str
    score: float = 0.0


@dataclass(frozen=True)
class Packet:
    accession: str
    packet_id: str
    source_kind: str
    passages: tuple[Passage, ...]


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


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def simple_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", norm(value).lower()).strip("_")


def publication_rows_by_accession(manifest: Path, accessions: set[str]) -> dict[str, list[dict[str, str]]]:
    out: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in read_tsv(manifest):
        acc = first(row, "accession", "project_accession", "pxd").upper()
        if acc in accessions:
            out[acc].append(row)
    return out


def metadata_text_fields(obj: Any, *, max_items: int = 160) -> list[tuple[str, str]]:
    """Return human-readable PRIDE metadata strings with JSON-path provenance.

    This deliberately excludes long file/checksum inventories and opaque identifiers.  It is a
    generic source-packet builder: it does not interpret the scientific meaning of any value.
    """
    rows: list[tuple[str, str]] = []
    skip_key = re.compile(r"(?i)(?:checksum|sha256|sha1|md5|filelist|file_list|files$|submission_file|ftp)")
    useful_key = re.compile(
        r"(?i)(title|description|keyword|protocol|sample|experiment|organism|species|tissue|cell|disease|instrument|"
        r"method|acquisition|label|modification|fraction|project|comment|additional|reference|publication)"
    )

    def walk(x: Any, path: str) -> None:
        if len(rows) >= max_items:
            return
        if isinstance(x, dict):
            for k, v in x.items():
                kp = f"{path}.{k}" if path else str(k)
                if skip_key.search(str(k)):
                    continue
                if isinstance(v, str):
                    val = norm(v)
                    if not val or len(val) > 4000:
                        continue
                    if len(val) < 2 or re.fullmatch(r"[A-Fa-f0-9]{24,}", val):
                        continue
                    if useful_key.search(str(k)) or len(val.split()) >= 4:
                        rows.append((kp, val))
                elif isinstance(v, (dict, list)):
                    walk(v, kp)
        elif isinstance(x, list):
            for i, v in enumerate(x[:120]):
                walk(v, f"{path}[{i}]")

    walk(obj, "project")
    # Stable dedupe by value while preserving first/best path.
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for path, value in rows:
        k = value.casefold()
        if k in seen:
            continue
        seen.add(k); out.append((path, value))
    return out


def build_metadata_passages(accession: str, snapshot: Path, *, max_chars: int = 18000) -> list[Passage]:
    obj = project_json(snapshot, accession)
    if not isinstance(obj, dict):
        return []
    fields = metadata_text_fields(obj)
    if not fields:
        return []
    source_uri = f"pride-snapshot:{accession}"
    lineage = f"repository:{accession}:{source_uri}"
    passages: list[Passage] = []
    buf: list[str] = []; locs: list[str] = []; n = 0
    for path, value in fields:
        line = f"{path}: {value}"
        if buf and n + len(line) + 1 > max_chars:
            idx = len(passages) + 1
            text = "\n".join(buf)
            passages.append(Passage(
                f"M{idx:04d}", "pride_metadata", source_uri, accession,
                ";".join(locs[:20]), text, sha256_text(text), "repository_model_extraction", lineage, 10.0,
            ))
            buf=[]; locs=[]; n=0
        buf.append(line); locs.append(path); n += len(line)+1
    if buf:
        idx=len(passages)+1; text="\n".join(buf)
        passages.append(Passage(
            f"M{idx:04d}", "pride_metadata", source_uri, accession,
            ";".join(locs[:20]), text, sha256_text(text), "repository_model_extraction", lineage, 10.0,
        ))
    return passages


def looks_like_heading(line: str) -> bool:
    s=norm(line).strip(".: ")
    if not s or len(s)>120:
        return False
    low=s.lower()
    if low in REFERENCES_HEADINGS:
        return True
    if any(low == x or low.startswith(x+" ") for x in ("methods","materials and methods","experimental procedures","results","discussion","data availability","supplementary methods")):
        return True
    return len(s.split()) <= 8 and (s.isupper() or s.istitle())


def split_publication_chunks(text: str, *, target_chars: int = 6500, overlap_paragraphs: int = 1) -> list[tuple[str, str]]:
    """Split normalized manuscript text into section-aware chunks, excluding bibliography content."""
    raw_lines=text.replace("\r\n","\n").replace("\r","\n").split("\n")
    paragraphs: list[tuple[str,str]]=[]
    section=""
    buf: list[str]=[]
    def flush() -> None:
        nonlocal buf
        if buf:
            p=norm(" ".join(buf))
            if p: paragraphs.append((section,p))
            buf=[]
    in_refs=False
    for line in raw_lines:
        s=norm(line)
        if not s:
            flush(); continue
        if looks_like_heading(s):
            flush(); section=s
            if s.lower().strip(".: ") in REFERENCES_HEADINGS:
                in_refs=True
                continue
            if in_refs:
                # A later explicit supplementary/method/data heading may leave bibliography state.
                sl=s.lower()
                if any(k in sl for k in ("supplement", "method", "data availability", "acknowledg")):
                    in_refs=False
        if in_refs:
            continue
        buf.append(s)
    flush()
    if not paragraphs:
        return []
    chunks: list[tuple[str,str]]=[]
    i=0
    while i < len(paragraphs):
        parts=[]; sections=[]; chars=0; j=i
        while j < len(paragraphs):
            sec,p=paragraphs[j]
            add=(f"[{sec}]\n" if sec and (not sections or sections[-1]!=sec) else "")+p
            if parts and chars+len(add)>target_chars:
                break
            parts.append(add); sections.append(sec); chars+=len(add)+2; j+=1
        if not parts:
            sec,p=paragraphs[i]; parts=[p[:target_chars]]; sections=[sec]; j=i+1
        label=next((s for s in sections if s), "manuscript")
        chunks.append((label, "\n\n".join(parts)))
        if j>=len(paragraphs): break
        i=max(i+1, j-overlap_paragraphs)
    return chunks


def passage_score(section: str, text: str) -> float:
    low=(section+" "+text).lower()
    score=0.0
    for terms in ANCHOR_GROUPS.values():
        hits=sum(1 for t in terms if t in low)
        score += min(5,hits)
    if any(x in section.lower() for x in SECTION_BONUS): score += 5.0
    if "data availability" in section.lower(): score += 1.0
    return score


def publication_source_identity(row: dict[str,str], manifest: Path, row_index: int) -> tuple[str,str,str,str,str]:
    doi=first(row,"doi","publication_doi").lower().rstrip(".,;")
    pmid=first(row,"pmid","publication_pmid")
    title=first(row,"title","publication_title")
    if doi:
        uri=f"doi:{doi}"; lineage=uri
    elif pmid:
        uri=f"pmid:{pmid}"; lineage=uri
    else:
        uri=f"manifest:{manifest}:{row_index}"; lineage=uri
    return uri,lineage,title,doi,pmid


def build_publication_passages(
    accession: str, rows: list[dict[str,str]], manifest: Path, *,
    target_chars: int, max_chunks: int, mode: str,
) -> tuple[list[Passage], dict[str,Any]]:
    passages: list[Passage]=[]; stats={"publications":0,"chunks_total":0,"chunks_selected":0,"text_chars_total":0,"text_chars_selected":0}
    next_id=1
    for row_index,row in enumerate(rows,start=2):
        text_path=first(row,"publication_content_text_path","text_path","content_text_path")
        p=Path(text_path) if text_path else None
        if not p or not p.is_file():
            continue
        text=p.read_text(errors="replace")
        if not norm(text): continue
        uri,lineage,title,doi,pmid=publication_source_identity(row,manifest,row_index)
        chunks=split_publication_chunks(text,target_chars=target_chars)
        scored=[(passage_score(sec,chunk),idx,sec,chunk) for idx,(sec,chunk) in enumerate(chunks)]
        stats["publications"]+=1; stats["chunks_total"]+=len(scored); stats["text_chars_total"]+=sum(len(x[3]) for x in scored)
        if mode=="full":
            selected=scored if max_chunks<=0 else scored[:max_chunks]
        elif max_chunks<=0 or len(scored)<=max_chunks:
            selected=scored
        else:
            # Relevant mode keeps highest-scoring evidence while also preserving early manuscript
            # context.  This is retrieval only; scientific interpretation remains with the model.
            priority=sorted(scored,key=lambda x:(-x[0],x[1]))[:max_chunks]
            selected=sorted(priority,key=lambda x:x[1])
        for score,idx,sec,chunk in selected:
            pid=f"P{next_id:04d}"; next_id+=1
            passages.append(Passage(
                pid,"publication",uri,title or doi or pmid,
                f"{p.name}:chunk{idx+1}:{sec}",chunk,sha256_text(text),
                "peer_reviewed_model_extraction",lineage,score,
            ))
        stats["chunks_selected"]+=len(selected); stats["text_chars_selected"]+=sum(len(x[3]) for x in selected)
    stats["coverage_fraction"]=(stats["text_chars_selected"]/stats["text_chars_total"]) if stats["text_chars_total"] else 0.0
    return passages,stats


def packetize(accession: str, passages: list[Passage], *, max_packet_chars: int = 20000, max_passages: int = 6) -> list[Packet]:
    packets=[]; buf=[]; chars=0
    for passage in passages:
        add=len(passage.text)+len(passage.source_locator)+80
        if buf and (chars+add>max_packet_chars or len(buf)>=max_passages):
            payload="|".join(x.passage_id+":"+x.content_sha256+":"+x.source_locator for x in buf)
            packets.append(Packet(accession,stable_id("packet",accession,payload),buf[0].source_kind,tuple(buf)))
            buf=[]; chars=0
        buf.append(passage); chars+=add
    if buf:
        payload="|".join(x.passage_id+":"+x.content_sha256+":"+x.source_locator for x in buf)
        packets.append(Packet(accession,stable_id("packet",accession,payload),buf[0].source_kind,tuple(buf)))
    return packets


def response_schema() -> dict[str,Any]:
    claim={
        "type":"object",
        "properties":{
            "subject_scope":{"type":"string","enum":["accession","experimental_context"]},
            "context_key":{"type":"string"},
            "context_label":{"type":"string"},
            "predicate":{"type":"string","enum":list(ALLOWED_PREDICATES)},
            "object_value":{"type":"string"},
            "evidence_refs":{"type":"array","items":{"type":"string"},"minItems":1,"maxItems":4},
            "confidence":{"type":"number","minimum":0.0,"maximum":1.0},
            "certainty":{"type":"string","enum":["explicit","strongly_implied","uncertain"]},
        },
        "required":["subject_scope","context_key","context_label","predicate","object_value","evidence_refs","confidence","certainty"],
        "additionalProperties":False,
    }
    return {"type":"object","properties":{"claims":{"type":"array","items":claim,"maxItems":80}},"required":["claims"],"additionalProperties":False}


def prompt_for(packet: Packet) -> str:
    evidence=[]
    for p in packet.passages:
        evidence.append(f"### {p.passage_id}\nSOURCE: {p.source_kind}\nLOCATOR: {p.source_locator}\nTEXT:\n{p.text}")
    predicates="\n".join(f"- {x}" for x in ALLOWED_PREDICATES)
    return f"""You are a constrained semantic evidence extractor for a single-cell-proteomics knowledge graph.

ACCESSION SCOPE: {packet.accession}

Your job is NOT to generate an SDRF and NOT to guess missing metadata. Extract only claims that are explicitly stated or unambiguously supported by the supplied evidence passages.

ALLOWED PREDICATES:
{predicates}

STRICT RULES:
1. Every claim MUST cite one or more supplied passage IDs in evidence_refs. Never cite an ID not present below.
2. Use subject_scope='experimental_context' for experimental design/method/biology facts unless the source explicitly states the fact is dataset-wide. Give the same concise context_key to facts that clearly belong to the same source-described experiment. Context keys are source-local evidence labels, not canonical branch IDs.
3. Do not create a new context merely because a new sentence uses a different synonym. Conversely, do not merge experiments that the source explicitly distinguishes (for example DDA vs DIA, TMT vs label-free, comparator vs primary single-cell experiment).
4. RAW/file/sample/cell/run mappings are FORBIDDEN. Do not emit file names, sample IDs, well IDs, or RAW-to-channel assignments. Reporter-role facts such as '126 was the carrier channel' are allowed only when the cited source explicitly states that role.
5. Do not infer from general scientific knowledge. If the passage says 'mouse', object_value='mouse' is fine; do not invent strain, tissue, disease, or cell type.
6. Do not treat software/search engines as instruments, isolation methods, or acquisition methods.
7. HAS_MODALITY should describe the source experiment using a concise value such as label_free_single_cell, reporter_multiplexed_single_cell, targeted_reporter_single_cell, label_free, or modality_unresolved only when directly supported.
8. USES_CHEMISTRY is for reporter chemistry such as TMT, TMT6, TMT10, TMTpro16, TMTpro18. plexDIA is a technology, not an isobaric reporter chemistry.
9. HAS_ACQUISITION is for MS acquisition such as DDA, DIA, PRM, diaPASEF. Analysis software is not acquisition.
10. For channel predicates emit ONE claim per reporter channel. Do not put a comma-separated channel list in one object_value.
11. References/citations to other studies are not evidence that this accession used those methods. Extract only statements describing the current study/experiment.
12. If evidence is ambiguous, omit the claim. Uncertainty is preferable to hallucination.
13. confidence is your confidence that the cited passage supports the exact claim, not confidence that the method is generally plausible.

EVIDENCE PASSAGES:
{chr(10).join(evidence)}
"""


def post_ollama(url: str, model: str, packet: Packet, *, timeout: int, num_ctx: int, retries: int) -> tuple[dict[str,Any],dict[str,Any]]:
    if requests is None:
        raise RuntimeError("requests is required for Ollama semantic extraction")
    payload={
        "model":model,
        "prompt":prompt_for(packet),
        "stream":False,
        "format":response_schema(),
        "options":{"temperature":0.0,"num_ctx":num_ctx},
    }
    last=None; started=time.monotonic()
    for attempt in range(retries+1):
        try:
            r=requests.post(url,json=payload,timeout=timeout)
            body=r.json()
            if not r.ok:
                raise RuntimeError(f"Ollama HTTP {r.status_code}: {body}")
            raw=body.get("response") or ""
            obj=json.loads(raw)
            if not isinstance(obj,dict) or not isinstance(obj.get("claims"),list):
                raise ValueError("structured response missing claims[]")
            return obj,{"attempts":attempt+1,"wall_seconds":round(time.monotonic()-started,3),"eval_count":body.get("eval_count"),"prompt_eval_count":body.get("prompt_eval_count")}
        except Exception as exc:
            last=exc
            if attempt>=retries: break
    raise RuntimeError(f"Ollama extraction failed after {retries+1} attempt(s): {type(last).__name__}: {last}")


def normalized_contains(haystack: str, needle: str) -> bool:
    h=re.sub(r"[^a-z0-9]+","",haystack.lower())
    n=re.sub(r"[^a-z0-9]+","",needle.lower())
    return bool(n) and n in h


def evidence_grounding(predicate: str, object_value: str, passages: list[Passage]) -> str:
    text=" ".join(p.text for p in passages)
    if normalized_contains(text,object_value):
        return "lexical_exact_or_normalized"
    ov=object_value.lower().strip()
    aliases={
        "mus musculus":["mouse","mice"], "homo sapiens":["human","humans"],
        "tmtpro18":["tmtpro 18","tmt pro 18","18-plex"],
        "tmtpro16":["tmtpro 16","tmt pro 16","16-plex"],
        "label_free_single_cell":["label-free single-cell","label free single cell","single-cell label-free"],
        "reporter_multiplexed_single_cell":["single-cell tmt","single cell tmt","multiplexed single-cell","multiplexed single cell"],
        "targeted_reporter_single_cell":["surequant","triggered ms/ms","targeted single-cell","targeted single cell"],
        "cellenone":["cellenone","cellenone"],
    }
    if any(a in text.lower() for a in aliases.get(ov,[])):
        return "known_alias_in_source"
    # Reporter role claims require the channel token itself to be in the cited source.
    if predicate in {"HAS_ANALYTICAL_CHANNEL","HAS_CARRIER_CHANNEL","HAS_BLANK_CHANNEL","HAS_REFERENCE_CHANNEL"}:
        return "ungrounded_channel"
    return "citation_supported_no_lexical_alias"


def context_ref(accession: str, source_uri: str, context_key: str, context_label: str) -> dict[str,Any]:
    key=simple_key(context_key or context_label or "source_context") or "source_context"
    source_hash=hashlib.sha256(source_uri.encode()).hexdigest()[:12]
    return {
        "type":"ExperimentalContext",
        "key":f"{accession.upper()}::{source_hash}::{key}",
        "label":context_label or context_key or key,
        "attrs":{"accession":accession.upper(),"source_local_context_key":context_key},
    }


def claim_records_from_response(packet: Packet, response: dict[str,Any], model: str) -> tuple[list[dict[str,Any]],list[dict[str,Any]]]:
    passage_map={p.passage_id:p for p in packet.passages}
    accepted=[]; rejected=[]; seen=set(); context_link_seen=set()
    for idx,c in enumerate(response.get("claims") or [],start=1):
        reason=""
        pred=norm(c.get("predicate")).upper(); obj=norm(c.get("object_value")); refs=[norm(x) for x in (c.get("evidence_refs") or []) if norm(x)]
        if pred not in ALLOWED_PREDICATES: reason="predicate_not_allowed"
        elif not obj: reason="empty_object_value"
        elif not refs or any(r not in passage_map for r in refs): reason="invalid_evidence_ref"
        elif c.get("certainty")=="uncertain": reason="model_marked_uncertain"
        cited=[passage_map[r] for r in refs if r in passage_map]
        grounding=evidence_grounding(pred,obj,cited) if cited else ""
        if pred in {"HAS_ANALYTICAL_CHANNEL","HAS_CARRIER_CHANNEL","HAS_BLANK_CHANNEL","HAS_REFERENCE_CHANNEL"}:
            channel_token=re.sub(r"(?i)^TMT", "", obj).strip().upper().replace(" ", "")
            if not re.fullmatch(r"(?:12[6-9]|13[0-5])(?:[NC])?", channel_token):
                reason="reporter_channel_must_be_single_canonical_token"
            elif grounding=="ungrounded_channel":
                reason="reporter_channel_not_present_in_cited_source"
        try: conf=float(c.get("confidence",0.0))
        except Exception: conf=0.0; reason=reason or "invalid_confidence"
        conf=max(0.0,min(1.0,conf))
        if c.get("certainty")=="strongly_implied": conf=min(conf,0.82)
        if grounding=="citation_supported_no_lexical_alias": conf=min(conf,0.76)
        if conf<0.50: reason=reason or "confidence_below_floor"
        if reason:
            rejected.append({"packet_id":packet.packet_id,"claim_index":idx,"reason":reason,"predicate":pred,"object_value":obj,"evidence_refs":refs,"grounding":grounding})
            continue
        # All cited passages in one model claim must come from the same raw source lineage.  This
        # prevents one model call from synthesizing a fact across unrelated manuscripts/sources.
        source_uris={p.source_uri for p in cited}
        if len(source_uris)!=1:
            rejected.append({"packet_id":packet.packet_id,"claim_index":idx,"reason":"cross_source_claim_forbidden","predicate":pred,"object_value":obj,"evidence_refs":refs})
            continue
        p0=cited[0]
        evidence_text="\n\n".join(f"[{p.passage_id}] {p.text}" for p in cited)
        evidence_locator=";".join(f"{p.passage_id}:{p.source_locator}" for p in cited)
        source_meta={"lineage_uri":p0.lineage_uri,"prompt_version":PROMPT_VERSION,"model":model,"packet_id":packet.packet_id,"grounding":grounding,"cited_passages":refs}
        if norm(c.get("subject_scope"))=="accession":
            subject={"type":"Accession","key":packet.accession,"label":packet.accession,"attrs":{}}
            branch_scope=""
        else:
            subject=context_ref(packet.accession,p0.source_uri,norm(c.get("context_key")),norm(c.get("context_label")))
            branch_scope=subject["key"]
            # Link the source-local context to the accession.  This relation remains semantic and is
            # resolved under the same provenance as its first claim.
            link_sig=(subject["key"],p0.source_uri,evidence_locator)
            if link_sig not in context_link_seen:
                context_link_seen.add(link_sig)
                accepted.append({
                    "scope_accession":packet.accession,
                    "subject":{"type":"Accession","key":packet.accession,"label":packet.accession},
                    "predicate":"HAS_EXPERIMENTAL_CONTEXT",
                    "object":subject,
                    "source_uri":p0.source_uri,"source_type":f"llm_{p0.source_kind}_semantic",
                    "source_title":p0.source_title,"source_content_sha256":p0.content_sha256,
                    "trust_class":p0.trust_class,"source_metadata":source_meta,
                    "evidence_locator":evidence_locator,"evidence_text":evidence_text,
                    "extractor":f"small_llm:{model}:{PROMPT_VERSION}","confidence":conf,"status":"asserted",
                    "attrs":{"evidence_validation":grounding,"claim_kind":"experimental_context_link"},
                })
        record={
            "scope_accession":packet.accession,"branch_scope":branch_scope,
            "subject":subject,"predicate":pred,
            "source_uri":p0.source_uri,"source_type":f"llm_{p0.source_kind}_semantic",
            "source_title":p0.source_title,"source_content_sha256":p0.content_sha256,
            "trust_class":p0.trust_class,"source_metadata":source_meta,
            "evidence_locator":evidence_locator,"evidence_text":evidence_text,
            "extractor":f"small_llm:{model}:{PROMPT_VERSION}","confidence":conf,"status":"asserted",
            "attrs":{"evidence_validation":grounding,"certainty":c.get("certainty"),"model_raw_context_key":c.get("context_key","")},
        }
        if pred in PREDICATE_OBJECT_TYPES:
            typ=PREDICATE_OBJECT_TYPES[pred]
            record["object"]={"type":typ,"key":obj,"label":obj}
        else:
            dtype=LITERAL_PREDICATES[pred]
            if dtype=="integer":
                m=re.search(r"\d+",obj)
                if not m:
                    rejected.append({"packet_id":packet.packet_id,"claim_index":idx,"reason":"integer_literal_missing","predicate":pred,"object_value":obj})
                    continue
                obj=m.group(0)
            record["literal_value"]=obj; record["literal_datatype"]=dtype
        sig=json.dumps({k:record.get(k) for k in ("scope_accession","branch_scope","subject","predicate","object","literal_value")},sort_keys=True)
        if sig in seen: continue
        seen.add(sig); accepted.append(record)
    return accepted,rejected


def write_jsonl(path: Path, rows: Iterable[dict[str,Any]]) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("w",encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row,ensure_ascii=False,sort_keys=True)+"\n")


def run(args: argparse.Namespace) -> int:
    accessions=read_accessions(args.accessions_file); wanted=set(accessions)
    by_pub=publication_rows_by_accession(args.publication_manifest,wanted)
    out=args.output; out.mkdir(parents=True,exist_ok=True); cache=args.cache_dir or out/"cache"; cache.mkdir(parents=True,exist_ok=True)
    packets_dir=out/"packets"; packets_dir.mkdir(exist_ok=True)
    claims=[]; rejects=[]; packet_stats=[]; coverage={}
    source_counts=Counter(); response_claims=0; cache_hits=0; failures=[]

    for acc in accessions:
        passages=build_metadata_passages(acc,args.snapshot,max_chars=args.metadata_packet_chars)
        pub_passages,pstats=build_publication_passages(acc,by_pub.get(acc,[]),args.publication_manifest,target_chars=args.chunk_chars,max_chunks=args.max_publication_chunks,mode=args.publication_mode)
        passages += pub_passages; coverage[acc]=pstats
        # Packetize separately per raw source URI so one model response can never synthesize across
        # unrelated manuscripts or repository metadata.
        by_source: dict[tuple[str,str],list[Passage]]=defaultdict(list)
        for p in passages: by_source[(p.source_kind,p.source_uri)].append(p)
        acc_packets=[]
        for _,ps in sorted(by_source.items()):
            acc_packets.extend(packetize(acc,ps,max_packet_chars=args.max_packet_chars,max_passages=args.max_passages_per_packet))
        for packet in acc_packets:
            pjson={"accession":packet.accession,"packet_id":packet.packet_id,"source_kind":packet.source_kind,"passages":[p.__dict__ for p in packet.passages]}
            (packets_dir/f"{packet.packet_id}.json").write_text(json.dumps(pjson,indent=2,ensure_ascii=False)+"\n")
            source_counts[packet.source_kind]+=1
            cache_key=hashlib.sha256((PROMPT_VERSION+"\n"+args.model+"\n"+json.dumps(pjson,sort_keys=True)).encode()).hexdigest()
            cache_path=cache/f"{cache_key}.json"
            stats={"accession":acc,"packet_id":packet.packet_id,"source_kind":packet.source_kind,"cache":False,"claims":0,"accepted":0,"rejected":0,"error":""}
            try:
                if cache_path.is_file() and not args.force:
                    cached=json.loads(cache_path.read_text())
                    response=cached["response"]; model_stats=cached.get("model_stats",{}); cache_hits+=1; stats["cache"]=True
                elif args.dry_run:
                    stats["error"]="dry_run_no_model_call"; packet_stats.append(stats); continue
                else:
                    response,model_stats=post_ollama(args.ollama_url,args.model,packet,timeout=args.timeout,num_ctx=args.num_ctx,retries=args.retries)
                    cache_path.write_text(json.dumps({"prompt_version":PROMPT_VERSION,"model":args.model,"packet":pjson,"response":response,"model_stats":model_stats},indent=2,ensure_ascii=False)+"\n")
                response_claims += len(response.get("claims") or []); stats["claims"]=len(response.get("claims") or [])
                a,r=claim_records_from_response(packet,response,args.model); claims.extend(a); rejects.extend(r); stats["accepted"]=len(a); stats["rejected"]=len(r); stats.update({f"model_{k}":v for k,v in model_stats.items()})
            except Exception as exc:
                stats["error"]=f"{type(exc).__name__}: {exc}"; failures.append(stats.copy())
                if args.fail_fast: raise
            packet_stats.append(stats)

    # Stable graph-claim dedupe across overlapping publication chunks.
    dedup={}
    for row in claims:
        sig=json.dumps({k:row.get(k) for k in ("scope_accession","branch_scope","subject","predicate","object","literal_value","source_uri")},sort_keys=True)
        old=dedup.get(sig)
        if old is None or float(row.get("confidence",0))>float(old.get("confidence",0)):
            dedup[sig]=row
    claims=list(dedup.values())
    write_jsonl(out/"semantic_claims.jsonl",claims)
    write_jsonl(out/"rejected_claims.jsonl",rejects)
    with (out/"packet_results.tsv").open("w",newline="",encoding="utf-8") as fh:
        fields=sorted({k for r in packet_stats for k in r}) if packet_stats else ["accession","packet_id"]
        w=csv.DictWriter(fh,fieldnames=fields,delimiter="\t",extrasaction="ignore"); w.writeheader(); w.writerows(packet_stats)
    summary={
        "extractor_version":VERSION,"prompt_version":PROMPT_VERSION,"model":args.model,"accessions":len(accessions),
        "publication_mode":args.publication_mode,"packets":len(packet_stats),"packet_source_counts":dict(source_counts),"cache_hits":cache_hits,
        "model_response_claims":response_claims,"graph_claims_after_validation_dedupe":len(claims),"rejected_claims":len(rejects),"packet_failures":len(failures),
        "publication_coverage":coverage,
        "predicate_counts":dict(Counter(r["predicate"] for r in claims)),
        "source_type_counts":dict(Counter(r["source_type"] for r in claims)),
        "policies":{
            "model_generates_sdrf":False,"model_mapping_predicates_allowed":False,"provenance_refs_required":True,
            "cross_source_claim_synthesis_allowed":False,"uncertain_claims_rejected":True,"citation_only_confidence_capped":True,
        },
        "outputs":{"claims":str(out/"semantic_claims.jsonl"),"rejected":str(out/"rejected_claims.jsonl"),"packets":str(packets_dir),"packet_results":str(out/"packet_results.tsv")},
    }
    (out/"semantic_claim_extraction_summary.json").write_text(json.dumps(summary,indent=2,ensure_ascii=False)+"\n")
    print(json.dumps(summary,indent=2,ensure_ascii=False))
    if failures and args.require_all_packets:
        raise SystemExit(f"{len(failures)} semantic extraction packet(s) failed")
    return 0


def self_test() -> None:
    import tempfile
    acc="PXD900301"
    text="Single cells were isolated using CellenONE and labelled with TMTpro18. Channel 126 was the carrier and channel 127C was left blank. Data were acquired by DIA."
    p=Passage("P0001","publication","doi:10.0000/fixture","Fixture","fixture:Methods",text,sha256_text(text),"peer_reviewed_model_extraction","doi:10.0000/fixture",10)
    packet=Packet(acc,"packet:test","publication",(p,))
    response={"claims":[
        {"subject_scope":"experimental_context","context_key":"single_cell_tmt","context_label":"single-cell TMT experiment","predicate":"USES_CHEMISTRY","object_value":"TMTpro18","evidence_refs":["P0001"],"confidence":0.98,"certainty":"explicit"},
        {"subject_scope":"experimental_context","context_key":"single_cell_tmt","context_label":"single-cell TMT experiment","predicate":"HAS_CARRIER_CHANNEL","object_value":"126","evidence_refs":["P0001"],"confidence":0.99,"certainty":"explicit"},
        {"subject_scope":"experimental_context","context_key":"single_cell_tmt","context_label":"single-cell TMT experiment","predicate":"HAS_BLANK_CHANNEL","object_value":"127C","evidence_refs":["P0001"],"confidence":0.99,"certainty":"explicit"},
        {"subject_scope":"experimental_context","context_key":"single_cell_tmt","context_label":"single-cell TMT experiment","predicate":"HAS_ACQUISITION","object_value":"DIA","evidence_refs":["P0001"],"confidence":0.95,"certainty":"explicit"},
        {"subject_scope":"experimental_context","context_key":"single_cell_tmt","context_label":"single-cell TMT experiment","predicate":"HAS_ANALYTICAL_CHANNEL","object_value":"135N","evidence_refs":["P0001"],"confidence":0.9,"certainty":"explicit"},
    ]}
    accepted,rejected=claim_records_from_response(packet,response,"fixture:3b")
    preds=Counter(x["predicate"] for x in accepted)
    assert preds["HAS_EXPERIMENTAL_CONTEXT"]==1
    assert preds["USES_CHEMISTRY"]==1 and preds["HAS_CARRIER_CHANNEL"]==1 and preds["HAS_BLANK_CHANNEL"]==1 and preds["HAS_ACQUISITION"]==1
    assert any(r["reason"]=="reporter_channel_not_present_in_cited_source" for r in rejected)
    # Publication chunking must remove the bibliography after an explicit References heading.
    chunks=split_publication_chunks("Methods\nSingle-cell TMTpro18 was used.\n\nReferences\nOther study used TMT6.")
    assert chunks and all("Other study used TMT6" not in x[1] for x in chunks)
    # Metadata extraction is accession-agnostic and retains source paths.
    with tempfile.TemporaryDirectory() as td:
        root=Path(td); (root/"projects").mkdir(); (root/"files").mkdir()
        (root/"projects"/f"{acc}.json").write_text(json.dumps({"title":"Synthetic SCP","description":"Single cells acquired by DDA","checksum":"abcdef"}))
        pp=build_metadata_passages(acc,root)
        assert pp and "Single cells acquired by DDA" in pp[0].text and "abcdef" not in pp[0].text
    print("scp_kg_extract_semantic_claims self-test: PASS")


def main() -> int:
    p=argparse.ArgumentParser()
    p.add_argument("--accessions-file",type=Path)
    p.add_argument("--snapshot",type=Path)
    p.add_argument("--publication-manifest",type=Path)
    p.add_argument("--output",type=Path)
    p.add_argument("--cache-dir",type=Path)
    p.add_argument("--model",default="qwen2.5:3b")
    p.add_argument("--ollama-url",default="http://localhost:11434/api/generate")
    p.add_argument("--timeout",type=int,default=1200)
    p.add_argument("--num-ctx",type=int,default=32768)
    p.add_argument("--retries",type=int,default=1)
    p.add_argument("--chunk-chars",type=int,default=6500)
    p.add_argument("--max-publication-chunks",type=int,default=24)
    p.add_argument("--publication-mode",choices=["full","relevant"],default="full")
    p.add_argument("--metadata-packet-chars",type=int,default=18000)
    p.add_argument("--max-packet-chars",type=int,default=20000)
    p.add_argument("--max-passages-per-packet",type=int,default=6)
    p.add_argument("--force",action="store_true")
    p.add_argument("--dry-run",action="store_true")
    p.add_argument("--fail-fast",action="store_true")
    p.add_argument("--require-all-packets",action="store_true")
    p.add_argument("--self-test",action="store_true")
    args=p.parse_args()
    if args.self_test: self_test(); return 0
    for name in ("accessions_file","snapshot","publication_manifest","output"):
        if getattr(args,name) is None: raise SystemExit(f"--{name.replace('_','-')} is required")
    return run(args)


if __name__=="__main__":
    raise SystemExit(main())
