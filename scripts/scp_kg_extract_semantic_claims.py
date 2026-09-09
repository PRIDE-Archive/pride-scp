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
from datetime import datetime
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

VERSION = "pride-scp-kg-small-llm-semantic-extractor-v0.3"
PROMPT_VERSION = "scp-kg-semantic-claims-v3"
DEFAULT_SEED = 42

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
REPORTER_CHANNEL_PREDICATES = {
    "HAS_ANALYTICAL_CHANNEL",
    "HAS_CARRIER_CHANNEL",
    "HAS_BLANK_CHANNEL",
    "HAS_REFERENCE_CHANNEL",
}

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


def semantic_anchor_features(section: str, text: str) -> set[str]:
    low=(section+" "+text).lower()
    out={name for name,terms in ANCHOR_GROUPS.items() if any(t in low for t in terms)}
    sl=section.lower()
    if any(x in sl for x in SECTION_BONUS): out.add("methods_section")
    if "data availability" in sl: out.add("data_availability")
    return out


def semantic_excerpt(text: str, *, max_chars: int) -> str:
    """Keep high-recall ontology-anchored paragraphs from a selected source chunk.

    The source document stays available in full and the chunk locator remains unchanged.  This only
    reduces the amount of narrative prose sent to the CPU LLM.  No scientific fact is inferred here.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    paras=[norm(x) for x in re.split(r"\n\s*\n", text) if norm(x)]
    if len(paras) <= 1:
        return text[:max_chars]
    scored=[]
    for i,para in enumerate(paras):
        score=passage_score("",para)
        # Role-bearing reporter sentences and explicit single-cell wording get retrieval priority.
        low=para.lower()
        if any(x in low for x in ("carrier", "blank", "reference channel", "reporter channel")): score += 4
        if "single cell" in low or "single-cell" in low: score += 2
        scored.append((score,i,para))
    chosen=[]; used=0
    for score,i,para in sorted(scored,key=lambda x:(-x[0],x[1])):
        if score <= 0 and chosen:
            continue
        need=len(para)+(2 if chosen else 0)
        if chosen and used+need>max_chars:
            continue
        if not chosen and len(para)>max_chars:
            para=para[:max_chars]; need=len(para)
        chosen.append((i,para)); used += need
        if used >= max_chars*0.85:
            break
    if not chosen:
        return text[:max_chars]
    chosen.sort()
    return "\n\n".join(x[1] for x in chosen)[:max_chars]


def select_semantic_chunks(scored: list[tuple[float,int,str,str]], *, max_chunks: int) -> tuple[list[tuple[float,int,str,str]],dict[str,Any]]:
    """High-recall, diversity-aware source retrieval for SDRF-relevant semantics."""
    if not scored:
        return [],{"feature_groups_found":[],"feature_groups_selected":[],"candidate_chunks":0}
    cap=max_chunks if max_chunks>0 else len(scored)
    features={idx:semantic_anchor_features(sec,chunk) for _,idx,sec,chunk in scored}
    candidates=[x for x in scored if x[0]>0 or features[x[1]]]
    if not candidates:
        candidates=scored[:min(2,len(scored))]
    found=set().union(*(features[x[1]] for x in candidates)) if candidates else set()
    selected=[]; selected_idx=set(); covered=set()
    # Greedy feature coverage first, then score.  This avoids spending all model budget on one long
    # methods subsection while missing biology/sample-preparation/acquisition evidence elsewhere.
    remaining=list(candidates)
    while remaining and len(selected)<cap:
        best=max(remaining,key=lambda x:(len(features[x[1]]-covered),x[0],-x[1]))
        gain=features[best[1]]-covered
        if not gain and selected:
            break
        selected.append(best); selected_idx.add(best[1]); covered |= features[best[1]]
        remaining=[x for x in remaining if x[1]!=best[1]]
    for x in sorted(candidates,key=lambda x:(-x[0],x[1])):
        if len(selected)>=cap: break
        if x[1] not in selected_idx:
            selected.append(x); selected_idx.add(x[1]); covered |= features[x[1]]
    selected.sort(key=lambda x:x[1])
    return selected,{
        "candidate_chunks":len(candidates),
        "feature_groups_found":sorted(found),
        "feature_groups_selected":sorted(covered),
        "feature_group_coverage_fraction":(len(covered)/len(found)) if found else 1.0,
    }


def build_publication_passages(
    accession: str, rows: list[dict[str,str]], manifest: Path, *,
    target_chars: int, max_chunks: int, mode: str, semantic_excerpt_chars: int = 2800,
) -> tuple[list[Passage], dict[str,Any]]:
    passages: list[Passage]=[]
    stats={"publications":0,"chunks_total":0,"chunks_selected":0,"text_chars_total":0,"text_chars_selected":0,
           "model_chars_selected":0,"semantic_candidate_chunks":0,"semantic_feature_groups_found":[],
           "semantic_feature_groups_selected":[]}
    next_id=1; feature_found=set(); feature_selected=set()
    for row_index,row in enumerate(rows,start=2):
        text_path=first(row,"publication_content_text_path","text_path","content_text_path")
        p=Path(text_path) if text_path else None
        # Portable manifests may store publication content paths relative to the manifest itself.
        # Existing absolute local manifests continue to work unchanged.
        if p is not None and not p.is_absolute():
            p = manifest.parent / p
        if not p or not p.is_file():
            continue
        text=p.read_text(errors="replace")
        if not norm(text): continue
        uri,lineage,title,doi,pmid=publication_source_identity(row,manifest,row_index)
        chunks=split_publication_chunks(text,target_chars=target_chars)
        scored=[(passage_score(sec,chunk),idx,sec,chunk) for idx,(sec,chunk) in enumerate(chunks)]
        stats["publications"]+=1; stats["chunks_total"]+=len(scored); stats["text_chars_total"]+=sum(len(x[3]) for x in scored)
        retrieval={}
        if mode=="full":
            selected=scored if max_chunks<=0 else scored[:max_chunks]
        elif mode=="semantic":
            selected,retrieval=select_semantic_chunks(scored,max_chunks=max_chunks)
            stats["semantic_candidate_chunks"] += int(retrieval.get("candidate_chunks",0))
            feature_found.update(retrieval.get("feature_groups_found",[])); feature_selected.update(retrieval.get("feature_groups_selected",[]))
        elif max_chunks<=0 or len(scored)<=max_chunks:
            selected=scored
        else:
            priority=sorted(scored,key=lambda x:(-x[0],x[1]))[:max_chunks]
            selected=sorted(priority,key=lambda x:x[1])
        for score,idx,sec,chunk in selected:
            model_text=semantic_excerpt(chunk,max_chars=semantic_excerpt_chars) if mode=="semantic" else chunk
            pid=f"P{next_id:04d}"; next_id+=1
            passages.append(Passage(
                pid,"publication",uri,title or doi or pmid,
                f"{p.name}:chunk{idx+1}:{sec}",model_text,sha256_text(text),
                "peer_reviewed_model_extraction",lineage,score,
            ))
            stats["model_chars_selected"] += len(model_text)
        stats["chunks_selected"]+=len(selected); stats["text_chars_selected"]+=sum(len(x[3]) for x in selected)
    stats["coverage_fraction"]=(stats["text_chars_selected"]/stats["text_chars_total"]) if stats["text_chars_total"] else 0.0
    stats["model_character_fraction"]=(stats["model_chars_selected"]/stats["text_chars_total"]) if stats["text_chars_total"] else 0.0
    stats["semantic_feature_groups_found"]=sorted(feature_found)
    stats["semantic_feature_groups_selected"]=sorted(feature_selected)
    stats["semantic_feature_group_coverage_fraction"]=(len(feature_selected)/len(feature_found)) if feature_found else 1.0
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


def response_schema(max_claims: int = 24, *, evidence_ids: Iterable[str] = ()) -> dict[str,Any]:
    evidence_ids = tuple(dict.fromkeys(norm(x).upper() for x in evidence_ids if norm(x)))
    evidence_item: dict[str, Any] = {"type": "string"}
    # This is intentionally packet-specific.  Structured generation should make it impossible for
    # a current Ollama response to cite a passage that was not supplied to that model request.
    if evidence_ids:
        evidence_item["enum"] = list(evidence_ids)
    claim={
        "type":"object",
        "properties":{
            "subject_scope":{"type":"string","enum":["accession","experimental_context"]},
            "context_key":{"type":"string"},
            "context_label":{"type":"string"},
            "predicate":{"type":"string","enum":list(ALLOWED_PREDICATES)},
            "object_value":{"type":"string"},
            "evidence_refs":{"type":"array","items":evidence_item,"minItems":1,"maxItems":4},
            "confidence":{"type":"number","minimum":0.0,"maximum":1.0},
            "certainty":{"type":"string","enum":["explicit","strongly_implied","uncertain"]},
        },
        "required":["subject_scope","context_key","context_label","predicate","object_value","evidence_refs","confidence","certainty"],
        "additionalProperties":False,
    }
    return {"type":"object","properties":{"claims":{"type":"array","items":claim,"maxItems":max_claims}},"required":["claims"],"additionalProperties":False}


def prompt_for(packet: Packet, *, max_claims: int = 24) -> str:
    evidence=[]
    for p in packet.passages:
        evidence.append(f"### {p.passage_id}\nSOURCE: {p.source_kind}\nLOCATOR: {p.source_locator}\nTEXT:\n{p.text}")
    predicates="\n".join(f"- {x}" for x in ALLOWED_PREDICATES)
    evidence_ids=", ".join(p.passage_id for p in packet.passages)
    return f"""You are a constrained semantic evidence extractor for a single-cell-proteomics knowledge graph.

ACCESSION SCOPE: {packet.accession}

Your job is NOT to generate an SDRF and NOT to guess missing metadata. Extract only claims that are explicitly stated or unambiguously supported by the supplied evidence passages. Emit at most {max_claims} non-duplicate, high-value claims; prefer omission over repetition.

ALLOWED PREDICATES:
{predicates}

STRICT RULES:
1. Every claim MUST cite one or more supplied passage IDs in evidence_refs. The ONLY valid passage IDs for this request are: {evidence_ids}. Copy those IDs exactly and put only the IDs in evidence_refs; do not add labels, locators, prose, or combined strings.
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


def post_ollama(
    url: str, model: str, packet: Packet, *, timeout: int, num_ctx: int, retries: int,
    num_predict: int, max_claims: int, keep_alive: str, num_thread: int, seed: int,
) -> tuple[dict[str,Any],dict[str,Any]]:
    if requests is None:
        raise RuntimeError("requests is required for Ollama semantic extraction")
    options={
        "temperature":0.0,
        "seed":seed,
        # top_k=1 makes the intended greedy/deterministic decoding policy explicit rather than
        # relying only on temperature=0 backend behaviour.
        "top_k":1,
        "top_p":1.0,
        "num_ctx":num_ctx,
        "num_predict":num_predict,
    }
    if num_thread>0:
        options["num_thread"]=num_thread
    schema=response_schema(max_claims=max_claims,evidence_ids=(p.passage_id for p in packet.passages))
    prompt=prompt_for(packet,max_claims=max_claims)
    payload={
        "model":model,
        "prompt":prompt,
        "stream":False,
        "format":schema,
        "options":options,
        "keep_alive":keep_alive,
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
            stats={
                "attempts":attempt+1,
                "wall_seconds":round(time.monotonic()-started,3),
                "eval_count":body.get("eval_count"),
                "prompt_eval_count":body.get("prompt_eval_count"),
                "prompt_eval_cached_count":body.get("prompt_eval_cached_count"),
                "generation_seed":seed,
                "generation_temperature":0.0,
                "generation_top_k":1,
                "generation_top_p":1.0,
                "prompt_sha256":sha256_text(prompt),
                "schema_sha256":sha256_text(json.dumps(schema,sort_keys=True,separators=(",",":"))),
                "response_sha256":sha256_text(json.dumps(obj,sort_keys=True,separators=(",",":"),ensure_ascii=False)),
            }
            for key in ("total_duration","load_duration","prompt_eval_duration","eval_duration"):
                value=body.get(key)
                if isinstance(value,(int,float)):
                    stats[key+"_seconds"]=round(float(value)/1_000_000_000.0,3)
            return obj,stats
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
    if predicate in REPORTER_CHANNEL_PREDICATES:
        return "ungrounded_channel"
    return "citation_supported_no_lexical_alias"


def normalize_evidence_refs(raw_refs: Iterable[Any], passage_map: dict[str, Passage]) -> tuple[list[str], list[str]]:
    """Normalize only explicit packet-local passage identifiers.

    New v0.3 structured responses are schema-constrained to exact IDs, so this mostly supports
    already-completed v0.1/v0.2 cache entries.  It is intentionally not fuzzy: a string is accepted
    only when it contains one or more literal packet IDs as standalone identifier tokens.
    """
    canonical={pid.upper():pid for pid in passage_map}
    ordered=[]; invalid=[]
    for raw in raw_refs:
        value=norm(raw)
        if not value:
            continue
        upper=value.upper()
        if upper in canonical:
            hits=[canonical[upper]]
        else:
            hits=[]
            for key,pid in canonical.items():
                if re.search(rf"(?<![A-Z0-9]){re.escape(key)}(?![A-Z0-9])", upper):
                    hits.append(pid)
        if not hits:
            invalid.append(value)
            continue
        for pid in hits:
            if pid not in ordered:
                ordered.append(pid)
    return ordered,invalid


def canonical_reporter_tokens(value: str) -> list[str]:
    """Extract only explicit canonical TMT reporter tokens from a model value.

    A model sometimes violates the one-channel-per-claim instruction and returns values such as
    ``126, 127C`` or ``TMT-126 (carrier)``.  Splitting those literal tokens is deterministic and does
    not infer missing N/C suffixes or expand numeric ranges.
    """
    text=norm(value).upper()
    if not text:
        return []
    out=[]
    pattern=re.compile(r"(?<!\d)(12[6-9]|13[0-5])\s*[-_ ]?\s*([NC])?(?![A-Z0-9])")
    for match in pattern.finditer(text):
        token=match.group(1)+(match.group(2) or "")
        if token not in out:
            out.append(token)
    return out


def reporter_channel_present(token: str, passages: Iterable[Passage]) -> bool:
    token=norm(token).upper().replace(" ","")
    m=re.fullmatch(r"(12[6-9]|13[0-5])([NC])?",token)
    if not m:
        return False
    number,suffix=m.groups()
    if suffix:
        pattern=re.compile(rf"(?<!\d){re.escape(number)}\s*[-_ ]?\s*{suffix}(?![A-Z0-9])",re.I)
    else:
        # Do not silently coerce an explicitly N/C-resolved channel to the unresolved numeric token.
        pattern=re.compile(rf"(?<!\d){re.escape(number)}(?![A-Z0-9])",re.I)
    return any(pattern.search(p.text) for p in passages)


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
    expanded=[]
    for idx,c0 in enumerate(response.get("claims") or [],start=1):
        c=dict(c0) if isinstance(c0,dict) else {}
        pred=norm(c.get("predicate")).upper()
        if pred in REPORTER_CHANNEL_PREDICATES:
            tokens=canonical_reporter_tokens(norm(c.get("object_value")))
            if tokens:
                for subidx,token in enumerate(tokens,start=1):
                    one=dict(c); one["object_value"]=token
                    expanded.append((idx,subidx,one,norm(c.get("object_value"))))
                continue
        expanded.append((idx,1,c,norm(c.get("object_value"))))

    for idx,subidx,c,raw_object in expanded:
        reason=""
        pred=norm(c.get("predicate")).upper(); obj=norm(c.get("object_value"))
        raw_refs=[norm(x) for x in (c.get("evidence_refs") or []) if norm(x)]
        refs,invalid_refs=normalize_evidence_refs(raw_refs,passage_map)
        if pred not in ALLOWED_PREDICATES: reason="predicate_not_allowed"
        elif not obj: reason="empty_object_value"
        elif not refs or invalid_refs: reason="invalid_evidence_ref"
        elif c.get("certainty")=="uncertain": reason="model_marked_uncertain"
        cited=[passage_map[r] for r in refs if r in passage_map]
        grounding=evidence_grounding(pred,obj,cited) if cited else ""
        if pred in REPORTER_CHANNEL_PREDICATES:
            channel_token=obj.strip().upper().replace(" ", "")
            if not re.fullmatch(r"(?:12[6-9]|13[0-5])(?:[NC])?", channel_token):
                reason="reporter_channel_must_be_single_canonical_token"
            elif not reporter_channel_present(channel_token,cited):
                reason="reporter_channel_not_present_in_cited_source"
            else:
                grounding="explicit_reporter_token_in_source"
        try: conf=float(c.get("confidence",0.0))
        except Exception: conf=0.0; reason=reason or "invalid_confidence"
        conf=max(0.0,min(1.0,conf))
        if c.get("certainty")=="strongly_implied": conf=min(conf,0.82)
        if grounding=="citation_supported_no_lexical_alias": conf=min(conf,0.76)
        if conf<0.50: reason=reason or "confidence_below_floor"
        if reason:
            rejected.append({
                "accession":packet.accession,"source_kind":packet.source_kind,
                "packet_id":packet.packet_id,"claim_index":idx,"derived_subclaim_index":subidx,
                "reason":reason,"predicate":pred,"object_value":obj,"raw_object_value":raw_object,
                "evidence_refs":refs,"evidence_refs_raw":raw_refs,"invalid_evidence_refs":invalid_refs,
                "grounding":grounding,
            })
            continue
        # All cited passages in one model claim must come from the same raw source lineage.  This
        # prevents one model call from synthesizing a fact across unrelated manuscripts/sources.
        source_uris={p.source_uri for p in cited}
        if len(source_uris)!=1:
            rejected.append({"accession":packet.accession,"source_kind":packet.source_kind,"packet_id":packet.packet_id,"claim_index":idx,"derived_subclaim_index":subidx,"reason":"cross_source_claim_forbidden","predicate":pred,"object_value":obj,"evidence_refs":refs,"evidence_refs_raw":raw_refs})
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
                    rejected.append({"accession":packet.accession,"source_kind":packet.source_kind,"packet_id":packet.packet_id,"claim_index":idx,"derived_subclaim_index":subidx,"reason":"integer_literal_missing","predicate":pred,"object_value":obj})
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


def packet_json(packet: Packet) -> dict[str,Any]:
    return {"accession":packet.accession,"packet_id":packet.packet_id,"source_kind":packet.source_kind,"passages":[p.__dict__ for p in packet.passages]}


def generation_spec(*, model: str, num_ctx: int, num_predict: int, num_thread: int, max_claims: int, seed: int) -> dict[str,Any]:
    return {
        "prompt_version":PROMPT_VERSION,
        "model":model,
        "temperature":0.0,
        "seed":seed,
        "top_k":1,
        "top_p":1.0,
        "num_ctx":num_ctx,
        "num_predict":num_predict,
        "num_thread":num_thread,
        "max_claims":max_claims,
    }


def packet_cache_key(packet: Packet, spec: dict[str,Any]) -> str:
    pjson=packet_json(packet)
    payload={"generation":spec,"packet":pjson}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()


def cached_packet_to_object(raw: dict[str,Any]) -> Packet | None:
    pjson=raw.get("packet") or {}
    try:
        passages=tuple(Passage(**x) for x in (pjson.get("passages") or []))
        if not passages: return None
        return Packet(str(pjson.get("accession") or "").upper(),str(pjson.get("packet_id") or "legacy"),str(pjson.get("source_kind") or passages[0].source_kind),passages)
    except Exception:
        return None


def import_compatible_legacy_cache(
    cache: Path, *, model: str, wanted: set[str], valid_source_hashes: set[tuple[str,str,str]], suppress_reextract: bool = False,
) -> tuple[list[dict[str,Any]],list[dict[str,Any]],set[tuple[str,str,str]],dict[str,int]]:
    """Reuse completed older semantic packets whose underlying source content is still identical.

    This is deliberately source-hash/locator checked.  It allows an interrupted v0.5.6 run to feed
    v0.5.7 without forcing the CPU model to reread already-completed evidence merely because the new
    prompt uses smaller packets and a stricter output cap.
    """
    claims=[]; rejects=[]; covered=set(); packets=0; files=0
    for path in sorted(cache.glob("*.json")):
        try: raw=json.loads(path.read_text())
        except Exception: continue
        if norm(raw.get("model")) != norm(model): continue
        packet=cached_packet_to_object(raw)
        if packet is None or packet.accession not in wanted: continue
        response=raw.get("response")
        if not isinstance(response,dict) or not isinstance(response.get("claims"),list): continue
        # Every cited source must still be an identical source document / metadata passage.
        valid=True
        for p in packet.passages:
            if (packet.accession,p.source_uri,p.content_sha256) not in valid_source_hashes:
                valid=False; break
        if not valid: continue
        a,r=claim_records_from_response(packet,response,model)
        claims.extend(a); rejects.extend(r); packets+=1; files+=1
        # GPU inference is now cheap enough that prompt/schema upgrades should re-read the evidence
        # by default.  Older validated claims may still be imported, but legacy packets suppress
        # re-extraction only when explicitly requested.
        if suppress_reextract:
            for p in packet.passages:
                covered.add((packet.accession,p.source_uri,p.source_locator))
    return claims,rejects,covered,{"legacy_cache_files":files,"legacy_cache_packets_imported":packets,"legacy_graph_claims":len(claims),"legacy_rejected_claims":len(rejects)}


def write_packet_plan(path: Path, rows: list[dict[str,Any]]) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    fields=["ordinal","accession","packet_id","source_kind","passages","evidence_chars","cache_state","source_locators"]
    with path.open("w",newline="",encoding="utf-8") as fh:
        w=csv.DictWriter(fh,fieldnames=fields,delimiter="\t",extrasaction="ignore"); w.writeheader(); w.writerows(rows)


def append_jsonl(path: Path, row: dict[str,Any]) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("a",encoding="utf-8") as fh:
        fh.write(json.dumps(row,ensure_ascii=False,sort_keys=True)+"\n")


def run(args: argparse.Namespace) -> int:
    started_all=time.monotonic()
    accessions=read_accessions(args.accessions_file); wanted=set(accessions)
    by_pub=publication_rows_by_accession(args.publication_manifest,wanted)
    out=args.output; out.mkdir(parents=True,exist_ok=True); cache=args.cache_dir or out/"cache"; cache.mkdir(parents=True,exist_ok=True)
    packets_dir=out/"packets"; packets_dir.mkdir(exist_ok=True)
    coverage={}; all_passages_by_acc={}; source_counts=Counter()

    # Build the complete source/retrieval plan before making any model call.  Users can inspect the
    # plan immediately and the console reports exactly how many expensive packets remain.
    for acc in accessions:
        passages=build_metadata_passages(acc,args.snapshot,max_chars=args.metadata_packet_chars)
        pub_passages,pstats=build_publication_passages(
            acc,by_pub.get(acc,[]),args.publication_manifest,target_chars=args.chunk_chars,
            max_chunks=args.max_publication_chunks,mode=args.publication_mode,
            semantic_excerpt_chars=args.semantic_excerpt_chars,
        )
        passages += pub_passages; coverage[acc]=pstats; all_passages_by_acc[acc]=passages

    valid_source_hashes={(acc,p.source_uri,p.content_sha256) for acc,ps in all_passages_by_acc.items() for p in ps}
    claims=[]; rejects=[]; covered=set(); legacy_stats={"legacy_cache_files":0,"legacy_cache_packets_imported":0,"legacy_graph_claims":0,"legacy_rejected_claims":0}
    if args.reuse_legacy_cache:
        a,r,covered,legacy_stats=import_compatible_legacy_cache(
            cache,model=args.model,wanted=wanted,valid_source_hashes=valid_source_hashes,
            suppress_reextract=args.legacy_cache_suppresses_reextract,
        )
        claims.extend(a); rejects.extend(r)

    gen_spec=generation_spec(
        model=args.model,num_ctx=args.num_ctx,num_predict=args.num_predict,num_thread=args.num_thread,
        max_claims=args.max_claims,seed=args.seed,
    )

    planned=[]
    for acc in accessions:
        # Remove passages already read successfully by a compatible older cached packet.
        passages=[p for p in all_passages_by_acc[acc] if (acc,p.source_uri,p.source_locator) not in covered]
        by_source: dict[tuple[str,str],list[Passage]]=defaultdict(list)
        for p in passages: by_source[(p.source_kind,p.source_uri)].append(p)
        for _,ps in sorted(by_source.items()):
            planned.extend(packetize(acc,ps,max_packet_chars=args.max_packet_chars,max_passages=args.max_passages_per_packet))

    if args.max_packets>0:
        planned=planned[:args.max_packets]
    plan_rows=[]; current_cache_hits=0
    for i,packet in enumerate(planned,start=1):
        pjson=packet_json(packet)
        (packets_dir/f"{packet.packet_id}.json").write_text(json.dumps(pjson,indent=2,ensure_ascii=False)+"\n")
        source_counts[packet.source_kind]+=1
        cp=cache/f"{packet_cache_key(packet,gen_spec)}.json"
        state="current_cache" if cp.is_file() and not args.force else "model_call"
        current_cache_hits += int(state=="current_cache")
        plan_rows.append({
            "ordinal":i,"accession":packet.accession,"packet_id":packet.packet_id,"source_kind":packet.source_kind,
            "passages":len(packet.passages),"evidence_chars":sum(len(p.text) for p in packet.passages),"cache_state":state,
            "source_locators":";".join(p.source_locator for p in packet.passages),
        })
    write_packet_plan(out/"packet_plan.tsv",plan_rows)
    packet_counts=Counter(p.accession for p in planned)
    model_calls=sum(1 for r in plan_rows if r["cache_state"]=="model_call")
    print(json.dumps({
        "semantic_extraction_plan":{
            "extractor_version":VERSION,"publication_mode":args.publication_mode,"accessions":len(accessions),
            "planned_packets":len(planned),"planned_model_calls":model_calls,"current_prompt_cache_hits":current_cache_hits,
            **legacy_stats,"packets_by_accession":dict(packet_counts),
            "max_packet_chars":args.max_packet_chars,"max_claims_per_packet":args.max_claims,"num_ctx":args.num_ctx,"num_predict":args.num_predict,
            "generation":gen_spec,
        }
    },indent=2))

    checkpoint_claims=out/"semantic_claims.checkpoint.jsonl"; checkpoint_rejects=out/"rejected_claims.checkpoint.jsonl"; checkpoint_packets=out/"packet_results.checkpoint.jsonl"
    for path in (checkpoint_claims,checkpoint_rejects,checkpoint_packets):
        path.write_text("")
    for row in claims: append_jsonl(checkpoint_claims,row)
    for row in rejects: append_jsonl(checkpoint_rejects,row)

    packet_stats=[]; response_claims=0; cache_hits=current_cache_hits; failures=[]; model_wall=0.0
    total=len(planned)
    for ordinal,packet in enumerate(planned,start=1):
        pjson=packet_json(packet); cache_key=packet_cache_key(packet,gen_spec); cache_path=cache/f"{cache_key}.json"
        evidence_chars=sum(len(p.text) for p in packet.passages)
        stats={"ordinal":ordinal,"accession":packet.accession,"packet_id":packet.packet_id,"source_kind":packet.source_kind,"passages":len(packet.passages),"evidence_chars":evidence_chars,"cache":False,"claims":0,"accepted":0,"rejected":0,"error":""}
        stamp=datetime.now().astimezone().isoformat(timespec="seconds")
        cache_state="hit" if cache_path.is_file() and not args.force else "miss"
        print(f"[{ordinal}/{total}] {packet.accession} {packet.source_kind} chars={evidence_chars} cache={cache_state} start={stamp}",flush=True)
        one_start=time.monotonic()
        try:
            if cache_path.is_file() and not args.force:
                cached=json.loads(cache_path.read_text()); response=cached["response"]; model_stats=cached.get("model_stats",{}); stats["cache"]=True
            elif args.dry_run:
                stats["error"]="dry_run_no_model_call"; packet_stats.append(stats); append_jsonl(checkpoint_packets,stats); continue
            else:
                response,model_stats=post_ollama(
                    args.ollama_url,args.model,packet,timeout=args.timeout,num_ctx=args.num_ctx,retries=args.retries,
                    num_predict=args.num_predict,max_claims=args.max_claims,keep_alive=args.keep_alive,num_thread=args.num_thread,seed=args.seed,
                )
                cache_path.write_text(json.dumps({
                    "prompt_version":PROMPT_VERSION,"model":args.model,"generation":gen_spec,
                    "packet":pjson,"response":response,"model_stats":model_stats,
                },indent=2,ensure_ascii=False)+"\n")
            response_claims += len(response.get("claims") or []); stats["claims"]=len(response.get("claims") or [])
            a,r=claim_records_from_response(packet,response,args.model); claims.extend(a); rejects.extend(r); stats["accepted"]=len(a); stats["rejected"]=len(r); stats.update({f"model_{k}":v for k,v in model_stats.items()})
            for row in a: append_jsonl(checkpoint_claims,row)
            for row in r: append_jsonl(checkpoint_rejects,row)
        except Exception as exc:
            stats["error"]=f"{type(exc).__name__}: {exc}"; failures.append(stats.copy())
            if args.fail_fast: raise
        wall=time.monotonic()-one_start; stats["packet_wall_seconds"]=round(wall,3)
        if not stats["cache"]: model_wall += wall
        packet_stats.append(stats); append_jsonl(checkpoint_packets,stats)
        print(f"[{ordinal}/{total}] {packet.accession} done wall={wall:.1f}s claims={stats['claims']} accepted={stats['accepted']} rejected={stats['rejected']} error={stats['error'] or '-'}",flush=True)

    # Stable graph-claim dedupe across overlapping publication chunks and legacy/current cache input.
    dedup={}
    for row in claims:
        sig=json.dumps({k:row.get(k) for k in ("scope_accession","branch_scope","subject","predicate","object","literal_value","source_uri")},sort_keys=True)
        old=dedup.get(sig)
        if old is None or float(row.get("confidence",0))>float(old.get("confidence",0)):
            dedup[sig]=row
    claims=list(dedup.values())
    write_jsonl(out/"semantic_claims.jsonl",claims); write_jsonl(out/"rejected_claims.jsonl",rejects)
    with (out/"packet_results.tsv").open("w",newline="",encoding="utf-8") as fh:
        fields=sorted({k for r in packet_stats for k in r}) if packet_stats else ["accession","packet_id"]
        w=csv.DictWriter(fh,fieldnames=fields,delimiter="\t",extrasaction="ignore"); w.writeheader(); w.writerows(packet_stats)
    rejection_reason_counts=Counter(r.get("reason","reason_missing") for r in rejects)
    rejection_by_accession: dict[str,Counter[str]]=defaultdict(Counter)
    rejection_by_packet: dict[str,Counter[str]]=defaultdict(Counter)
    for row in rejects:
        rejection_by_accession[str(row.get("accession") or "legacy_or_unknown")][str(row.get("reason") or "reason_missing")]+=1
        rejection_by_packet[str(row.get("packet_id") or "unknown_packet")][str(row.get("reason") or "reason_missing")]+=1
    summary={
        "extractor_version":VERSION,"prompt_version":PROMPT_VERSION,"model":args.model,"accessions":len(accessions),
        "publication_mode":args.publication_mode,"packets":len(packet_stats),"packet_source_counts":dict(source_counts),"cache_hits":cache_hits,
        **legacy_stats,
        "planned_model_calls":model_calls,"model_response_claims":response_claims,"graph_claims_after_validation_dedupe":len(claims),"rejected_claims":len(rejects),"packet_failures":len(failures),
        "wall_seconds_total":round(time.monotonic()-started_all,3),"model_wall_seconds_this_run":round(model_wall,3),
        "publication_coverage":coverage,
        "predicate_counts":dict(Counter(r["predicate"] for r in claims)),
        "source_type_counts":dict(Counter(r["source_type"] for r in claims)),
        "rejection_reason_counts":dict(rejection_reason_counts),
        "rejection_reason_counts_by_accession":{k:dict(v) for k,v in sorted(rejection_by_accession.items())},
        "rejection_reason_counts_by_packet":{k:dict(v) for k,v in sorted(rejection_by_packet.items())},
        "generation":gen_spec,
        "performance_policy":{
            "semantic_retrieval":args.publication_mode=="semantic","semantic_excerpt_chars":args.semantic_excerpt_chars,
            "max_packet_chars":args.max_packet_chars,"max_claims_per_packet":args.max_claims,"num_ctx":args.num_ctx,"num_predict":args.num_predict,
            "legacy_cache_reuse":args.reuse_legacy_cache,
            "legacy_cache_suppresses_reextract":args.legacy_cache_suppresses_reextract,
            "incremental_checkpoints":True,
        },
        "policies":{
            "model_generates_sdrf":False,"model_mapping_predicates_allowed":False,"provenance_refs_required":True,
            "cross_source_claim_synthesis_allowed":False,"uncertain_claims_rejected":True,"citation_only_confidence_capped":True,
        },
        "outputs":{"claims":str(out/"semantic_claims.jsonl"),"rejected":str(out/"rejected_claims.jsonl"),"packets":str(packets_dir),"packet_plan":str(out/"packet_plan.tsv"),"packet_results":str(out/"packet_results.tsv")},
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
    # Packet-local structured schemas must enumerate only supplied evidence IDs.
    schema=response_schema(max_claims=5,evidence_ids=["P0001"])
    evidence_items=schema["properties"]["claims"]["items"]["properties"]["evidence_refs"]["items"]
    assert evidence_items["enum"]==["P0001"]
    # Legacy decorated refs may be recovered only by literal packet-ID token extraction.
    refs,bad=normalize_evidence_refs(["Evidence: P0001"],{"P0001":p})
    assert refs==["P0001"] and not bad
    refs,bad=normalize_evidence_refs(["P9999"],{"P0001":p})
    assert not refs and bad==["P9999"]
    # Multi-channel model values are deterministically split, never range-expanded, and every
    # resulting token still has to be explicitly present in the cited source.
    split_response={"claims":[
        {"subject_scope":"experimental_context","context_key":"single_cell_tmt","context_label":"single-cell TMT experiment","predicate":"HAS_CARRIER_CHANNEL","object_value":"TMT-126, 127C","evidence_refs":["P0001"],"confidence":0.99,"certainty":"explicit"},
    ]}
    aa,rr=claim_records_from_response(packet,split_response,"fixture:3b")
    role_objects={x.get("object",{}).get("key") for x in aa if x.get("predicate")=="HAS_CARRIER_CHANNEL"}
    assert role_objects=={"126","127C"} and not rr
    spec=generation_spec(model="fixture:3b",num_ctx=8192,num_predict=3072,num_thread=7,max_claims=20,seed=42)
    assert spec["seed"]==42 and spec["top_k"]==1 and spec["temperature"]==0.0
    # Publication chunking must remove the bibliography after an explicit References heading.
    chunks=split_publication_chunks("Methods\nSingle-cell TMTpro18 was used.\n\nReferences\nOther study used TMT6.")
    assert chunks and all("Other study used TMT6" not in x[1] for x in chunks)
    # Metadata extraction is accession-agnostic and retains source paths.
    with tempfile.TemporaryDirectory() as td:
        root=Path(td); (root/"projects").mkdir(); (root/"files").mkdir()
        (root/"projects"/f"{acc}.json").write_text(json.dumps({"title":"Synthetic SCP","description":"Single cells acquired by DDA","checksum":"abcdef"}))
        pp=build_metadata_passages(acc,root)
        assert pp and "Single cells acquired by DDA" in pp[0].text and "abcdef" not in pp[0].text
    # Semantic retrieval must preserve diverse ontology evidence while shortening model text.
    scored=[(passage_score("Methods",x),i,"Methods",x) for i,x in enumerate([
        "Single-cell samples were isolated by CellenONE.",
        "TMTpro18 channel 126 was the carrier and 127C the blank.",
        "Data were acquired by DIA on an Orbitrap.",
        "Long unrelated biological discussion without method anchors."
    ])]
    sel,meta=select_semantic_chunks(scored,max_chunks=3)
    assert len(sel)==3 and meta["feature_group_coverage_fraction"]>0.5
    assert len(semantic_excerpt("\n\n".join(x[3] for x in scored),max_chars=120))<=120
    # A completed older prompt cache may be reused only when accession/source content still match.
    with tempfile.TemporaryDirectory() as td:
        cache=Path(td)
        legacy={"prompt_version":"scp-kg-semantic-claims-v1","model":"fixture:3b","packet":packet_json(packet),"response":response,"model_stats":{"wall_seconds":1.0}}
        (cache/"legacy.json").write_text(json.dumps(legacy))
        aa,rr,cov,meta=import_compatible_legacy_cache(cache,model="fixture:3b",wanted={acc},valid_source_hashes={(acc,p.source_uri,p.content_sha256)})
        assert meta["legacy_cache_packets_imported"]==1 and aa and not cov
        aa,rr,cov,meta=import_compatible_legacy_cache(cache,model="fixture:3b",wanted={acc},valid_source_hashes={(acc,p.source_uri,p.content_sha256)},suppress_reextract=True)
        assert (acc,p.source_uri,p.source_locator) in cov
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
    p.add_argument("--num-ctx",type=int,default=8192)
    p.add_argument("--num-predict",type=int,default=3072)
    p.add_argument("--num-thread",type=int,default=0)
    p.add_argument("--seed",type=int,default=DEFAULT_SEED)
    p.add_argument("--keep-alive",default="30m")
    p.add_argument("--max-claims",type=int,default=20)
    p.add_argument("--retries",type=int,default=1)
    p.add_argument("--chunk-chars",type=int,default=6500)
    p.add_argument("--max-publication-chunks",type=int,default=6)
    p.add_argument("--publication-mode",choices=["full","relevant","semantic"],default="semantic")
    p.add_argument("--semantic-excerpt-chars",type=int,default=2800)
    p.add_argument("--metadata-packet-chars",type=int,default=18000)
    p.add_argument("--max-packet-chars",type=int,default=8000)
    p.add_argument("--max-passages-per-packet",type=int,default=3)
    p.add_argument("--max-packets",type=int,default=0)
    p.add_argument("--reuse-legacy-cache",action=argparse.BooleanOptionalAction,default=True)
    p.add_argument("--legacy-cache-suppresses-reextract",action=argparse.BooleanOptionalAction,default=False)
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
