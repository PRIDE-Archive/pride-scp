#!/usr/bin/env python3
"""Non-editing exact-hash review agent for PRIDE-SCP SDRF closure.

The reviewer can return only approved, held, or insufficient_evidence. Deterministic
prerequisites are checked before any model call. The reviewer cannot write SDRF bytes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from pathlib import Path
from typing import Any

STATUSES={"approved","held","insufficient_evidence"}


def sha256_file(path: Path) -> str:
    h=hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda:fh.read(1024*1024),b""):
            h.update(block)
    return h.hexdigest()


def read_json(path: Path) -> dict[str,Any]:
    obj=json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj,dict): raise ValueError(f"expected JSON object: {path}")
    return obj


def evidence_refs(obj: Any) -> set[str]:
    refs:set[str]=set()
    def walk(x:Any)->None:
        if isinstance(x,dict):
            for k,v in x.items():
                if k in {"evidence_ref","evidence_refs","source_ref","source_refs","ref","refs"}:
                    if isinstance(v,str) and v.strip(): refs.add(v.strip())
                    elif isinstance(v,list): refs.update(str(y).strip() for y in v if str(y).strip())
                walk(v)
        elif isinstance(x,list):
            for y in x: walk(y)
    walk(obj); return refs


def prerequisites(projected:Path,readiness:dict[str,Any],validator:dict[str,Any],evidence:dict[str,Any])->tuple[bool,list[str]]:
    failures=[]
    digest=sha256_file(projected)
    if readiness.get("state")!="needs_independent_review": failures.append("readiness_state_not_needs_independent_review")
    if readiness.get("blockers"): failures.append("readiness_has_blockers")
    rsha=str(readiness.get("projected_sha256") or "").lower()
    if not rsha or rsha!=digest.lower(): failures.append("projected_sha_mismatch")
    if validator.get("candidate_sha256","").lower()!=digest.lower(): failures.append("validator_sha_mismatch")
    if validator.get("validator_gate")!="green": failures.append("validator_not_green")
    parse=readiness.get("parse_sdrf") or []
    if not parse or not all(isinstance(x,dict) and x.get("passed") is True for x in parse): failures.append("readiness_parse_sdrf_not_green")
    skills=readiness.get("skills_check") or {}
    if not isinstance(skills,dict) or skills.get("passed") is not True: failures.append("skills_check_not_green")
    if not evidence_refs(evidence): failures.append("no_evidence_refs")
    return not failures,failures


def call_model(url:str,model:str,prompt:str,timeout:int)->dict[str,Any]:
    payload=json.dumps({"model":model,"prompt":prompt,"stream":False,"format":"json"}).encode()
    req=urllib.request.Request(url,data=payload,headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(req,timeout=timeout) as resp:
        outer=json.load(resp)
    raw=outer.get("response",outer) if isinstance(outer,dict) else outer
    if isinstance(raw,str): return json.loads(raw)
    if isinstance(raw,dict): return raw
    raise ValueError("unexpected reviewer response")


def review(accession:str,projected:Path,readiness:dict[str,Any],validator:dict[str,Any],evidence:dict[str,Any],model:str,url:str,timeout:int,fixture:Path|None=None)->dict[str,Any]:
    digest=sha256_file(projected)
    ok,failures=prerequisites(projected,readiness,validator,evidence)
    if not ok:
        return {"accession":accession,"status":"held","sha256":digest,"reason":"deterministic_prerequisite_failed","failures":failures,"evidence_refs":sorted(evidence_refs(evidence))}
    if fixture and fixture.is_file():
        decision=read_json(fixture)
    else:
        prompt=(
            "You are a non-editing independent SDRF reviewer. Return one JSON object only. "
            "Allowed status values: approved, held, insufficient_evidence. Do not propose edits. "
            "Approve only if the supplied exact candidate, readiness diagnostics and evidence support the projection.\n\n"
            f"accession={accession}\nsha256={digest}\n"
            f"readiness={json.dumps(readiness,sort_keys=True)}\n"
            f"validator={json.dumps(validator,sort_keys=True)}\n"
            f"evidence={json.dumps(evidence,sort_keys=True)}\n"
        )
        decision=call_model(url,model,prompt,timeout)
    status=str(decision.get("status") or "").strip().lower()
    if status not in STATUSES: status="insufficient_evidence"
    cited=decision.get("evidence_refs") or decision.get("refs") or []
    if isinstance(cited,str): cited=[cited]
    allowed=evidence_refs(evidence)
    cited=[str(x) for x in cited if str(x) in allowed]
    if status=="approved" and not cited:
        status="insufficient_evidence"
    return {"accession":accession,"status":status,"sha256":digest,"reason":str(decision.get("reason") or ""),"evidence_refs":cited,"model":model,"non_editing":True}


def self_test()->None:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root=Path(td); p=root/"x.tsv"; p.write_text("a\tb\n1\t2\n")
        d=sha256_file(p)
        readiness={"state":"needs_independent_review","blockers":[],"projected_sha256":d,"parse_sdrf":[{"passed":True}],"skills_check":{"passed":True}}
        validator={"candidate_sha256":d,"validator_gate":"green"}
        evidence={"items":[{"evidence_ref":"E1","text":"source"}]}
        fixture=root/"decision.json"; fixture.write_text(json.dumps({"status":"approved","evidence_refs":["E1"],"reason":"supported"}))
        out=review("PXD000001",p,readiness,validator,evidence,"fake","http://invalid",5,fixture)
        assert out["status"]=="approved" and out["sha256"]==d
    print("sdrf_independent_review_agent self-test: PASS")


def parser()->argparse.ArgumentParser:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test",action="store_true")
    p.add_argument("--accession")
    p.add_argument("--projected",type=Path)
    p.add_argument("--readiness",type=Path)
    p.add_argument("--validator-receipt",type=Path)
    p.add_argument("--evidence",type=Path)
    p.add_argument("--output",type=Path)
    p.add_argument("--model",default="qwen3.6:27b")
    p.add_argument("--ollama-url",default="http://127.0.0.1:11434/api/generate")
    p.add_argument("--timeout",type=int,default=1200)
    p.add_argument("--fixture",type=Path)
    return p


def main()->int:
    args=parser().parse_args()
    if args.self_test: self_test(); return 0
    for name in ("accession","projected","readiness","validator_receipt","evidence","output"):
        if getattr(args,name,None) in (None,""): raise SystemExit(f"--{name.replace('_','-')} is required")
    result=review(args.accession,args.projected,read_json(args.readiness),read_json(args.validator_receipt),read_json(args.evidence),args.model,args.ollama_url,args.timeout,args.fixture)
    args.output.parent.mkdir(parents=True,exist_ok=True); args.output.write_text(json.dumps(result,indent=2)+"\n")
    print(json.dumps(result,indent=2)); return 0

if __name__=="__main__": raise SystemExit(main())
