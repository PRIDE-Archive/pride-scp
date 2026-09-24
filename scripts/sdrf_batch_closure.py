#!/usr/bin/env python3
"""Validator-gated cohort closure orchestrator for PRIDE-SCP SDRFs.

This harness wraps bridge-v2 and resolver stages with an exact-byte sdrf-pipelines gate.
It is intentionally orchestration-only: the frozen readiness policy remains the final
submission authority, and every candidate mutation is followed by validator gating.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

VERSION="pride-scp-validator-gated-closure-v1"


def sha256_file(path:Path)->str:
    h=hashlib.sha256()
    with path.open("rb") as fh:
        for b in iter(lambda:fh.read(1024*1024),b""): h.update(b)
    return h.hexdigest()


def read_json(path:Path)->dict[str,Any]:
    obj=json.loads(path.read_text(encoding="utf-8"));
    if not isinstance(obj,dict): raise ValueError(f"expected JSON object: {path}")
    return obj


def write_accessions(path:Path,vals:list[str])->None:
    path.parent.mkdir(parents=True,exist_ok=True); path.write_text("".join(f"{x}\n" for x in vals),encoding="utf-8")


def parse_accessions(path:Path)->list[str]:
    import re
    vals=[]
    for line in path.read_text(errors="replace").splitlines():
        m=re.search(r"\bPXD\d{6}\b",line,re.I)
        if m: vals.append(m.group(0).upper())
    return sorted(set(vals))


def run(cmd:list[str],log:Path|None=None,env:dict[str,str]|None=None)->int:
    cp=subprocess.run(cmd,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=env)
    if log:
        log.parent.mkdir(parents=True,exist_ok=True); log.write_text(cp.stdout or "",encoding="utf-8")
    else: sys.stdout.write(cp.stdout or "")
    return cp.returncode


def find_candidate(root:Path,acc:str)->Path|None:
    direct=root/f"{acc}.sdrf.tsv"
    if direct.is_file(): return direct
    found=sorted(root.rglob(f"{acc}.sdrf.tsv")) if root.is_dir() else []
    return found[0] if found else None


def validator_gate(args:argparse.Namespace,acc:str,candidate:Path,boundary:str,out:Path)->dict[str,Any]:
    receipt=out/"validator_receipts"/acc/f"{boundary}.json"
    cmd=[args.python,str(args.validator_gate_script),"--accession",acc,"--candidate",str(candidate),"--boundary",boundary,"--output",str(receipt),"--readiness-script",str(args.readiness_script),"--parse-sdrf",args.parse_sdrf,"--ontology-mode","skip"]
    rc=run(cmd,log=out/"logs"/acc/f"validator_{boundary}.log")
    if rc not in (0,2) or not receipt.is_file():
        return {"accession":acc,"boundary":boundary,"candidate_sha256":sha256_file(candidate),"validator_gate":"red","infrastructure_error":f"validator_rc={rc}"}
    return read_json(receipt)


def append_validation(rows:list[dict[str,Any]],receipt:dict[str,Any],stage:str)->None:
    rows.append({"accession":receipt.get("accession",""),"stage":stage,"boundary":receipt.get("boundary",""),"candidate_sha256":receipt.get("candidate_sha256",""),"validator_gate":receipt.get("validator_gate","red"),"validator_version":receipt.get("validator_version",""),"runtime_sha256":receipt.get("runtime_sha256","")})


def run_bridge(args:argparse.Namespace,accs:list[str],candidate_root:Path,seed:Path|None,stage:Path)->int:
    af=stage/"accessions.txt"; write_accessions(af,accs)
    cmd=[args.python,str(args.autorepair_script),"--accessions-file",str(af),"--snapshot",str(args.snapshot),"--annotations-dir",str(args.annotations_dir),"--candidate-root",str(candidate_root),"--stage1-root",str(args.stage1_root),"--skills-root",str(args.skills_root),"--output",str(stage/"autorepair"),"--model",args.model,"--ollama-url",args.ollama_url,"--timeout",str(args.timeout),"--max-repair-rounds",str(args.bridge_repair_rounds),"--max-agent-turns",str(args.max_agent_turns),"--max-tool-actions",str(args.max_tool_actions),"--max-validator-cycles",str(args.max_validator_cycles),"--singularity",args.singularity,"--pride-scp",args.pride_scp,"--python",args.python]
    if seed is not None and seed.is_dir(): cmd += ["--seed-readiness-dir",str(seed)]
    if args.agent_sif: cmd += ["--agent-sif",str(args.agent_sif)]
    if args.readiness_sif: cmd += ["--readiness-sif",str(args.readiness_sif)]
    return run(cmd,log=stage/"bridge.log")


def terminal_from_bridge(row:dict[str,str])->str:
    return row.get("final_state") or "unsupported_or_exhausted"


def self_test()->None:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root=Path(td); af=root/"a.txt"; af.write_text("PXD000002\nPXD000001\nPXD000001\n")
        assert parse_accessions(af)==["PXD000001","PXD000002"]
        p=root/"x"; p.mkdir(); f=p/"PXD000001.sdrf.tsv"; f.write_text("a\tb\n1\t2\n")
        assert find_candidate(p,"PXD000001")==f
    print("sdrf_batch_closure self-test: PASS")


def parser()->argparse.ArgumentParser:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test",action="store_true")
    p.add_argument("--accessions-file",type=Path)
    p.add_argument("--candidate-root",type=Path)
    p.add_argument("--snapshot",type=Path)
    p.add_argument("--annotations-dir",type=Path)
    p.add_argument("--stage1-root",type=Path)
    p.add_argument("--skills-root",type=Path)
    p.add_argument("--output",type=Path)
    p.add_argument("--seed-readiness-dir",type=Path)
    p.add_argument("--publication-manifest",type=Path)
    p.add_argument("--repo-root",type=Path,default=Path.cwd())
    p.add_argument("--model",default="qwen3.6:27b")
    p.add_argument("--review-model",default="")
    p.add_argument("--ollama-url",default="http://127.0.0.1:11434/api/generate")
    p.add_argument("--timeout",type=int,default=1200)
    p.add_argument("--max-closure-passes",type=int,default=2)
    p.add_argument("--bridge-repair-rounds",type=int,default=2)
    p.add_argument("--max-agent-turns",type=int,default=4)
    p.add_argument("--max-tool-actions",type=int,default=6)
    p.add_argument("--max-validator-cycles",type=int,default=2)
    p.add_argument("--agent-sif",type=Path)
    p.add_argument("--readiness-sif",type=Path)
    p.add_argument("--singularity",default="singularity")
    p.add_argument("--pride-scp",default="pride-scp")
    p.add_argument("--python",default="python")
    p.add_argument("--parse-sdrf",default="parse_sdrf")
    p.add_argument("--readiness-script",type=Path,default=Path("/opt/pride-scp/scripts/sdrf_bigbio_readiness.py"))
    p.add_argument("--validator-gate-script",type=Path,default=Path("/opt/pride-scp/scripts/sdrf_validator_gate.py"))
    p.add_argument("--autorepair-script",type=Path,default=Path("/opt/pride-scp/scripts/sdrf_batch_autorepair.py"))
    p.add_argument("--cellosaurus-script",type=Path,default=Path("/opt/pride-scp/scripts/sdrf_cellosaurus_resolver.py"))
    p.add_argument("--review-script",type=Path,default=Path("/opt/pride-scp/scripts/sdrf_independent_review_agent.py"))
    p.add_argument("--evidence-script",type=Path,default=Path("/opt/pride-scp/scripts/sdrf_evidence_escalation_harness.py"))
    return p


def main()->int:
    args=parser().parse_args()
    if args.self_test: self_test(); return 0
    required=("accessions_file","candidate_root","snapshot","annotations_dir","stage1_root","skills_root","output")
    for k in required:
        if getattr(args,k,None) is None: raise SystemExit(f"--{k.replace('_','-')} is required")
    out=args.output; out.mkdir(parents=True,exist_ok=True)
    accs=parse_accessions(args.accessions_file)
    if not accs: raise SystemExit("accession list is empty")
    validation_rows:list[dict[str,Any]]=[]
    original_sha:dict[str,str]={}
    current_root=out/"input_candidates"; current_root.mkdir(parents=True,exist_ok=True)
    active=[]
    terminal:dict[str,dict[str,Any]]={}
    for acc in accs:
        src=find_candidate(args.candidate_root,acc)
        if src is None:
            terminal[acc]={"final_state":"unsupported_or_exhausted","reason_code":"candidate_missing"}; continue
        dest=current_root/f"{acc}.sdrf.tsv"; shutil.copy2(src,dest); original_sha[acc]=sha256_file(dest)
        rec=validator_gate(args,acc,dest,"input",out); append_validation(validation_rows,rec,"input")
        active.append(acc)
    for pass_no in range(1,args.max_closure_passes+1):
        if not active: break
        stage=out/f"closure_pass{pass_no:02d}"; stage.mkdir(parents=True,exist_ok=True)
        rc=run_bridge(args,active,current_root,args.seed_readiness_dir if pass_no==1 else None,stage)
        if rc!=0:
            for acc in active: terminal.setdefault(acc,{"final_state":"unsupported_or_exhausted","reason_code":f"bridge_infrastructure_failure:{rc}"})
            break
        ledger=stage/"autorepair"/"autorepair_ledger.tsv"
        if not ledger.is_file():
            for acc in active: terminal.setdefault(acc,{"final_state":"unsupported_or_exhausted","reason_code":"bridge_ledger_missing"})
            break
        rows={r["accession"]:r for r in csv.DictReader(ledger.open(newline=""),delimiter="\t")}
        next_active=[]; next_root=stage/"next_candidates"; next_root.mkdir(parents=True,exist_ok=True)
        for acc in active:
            row=rows.get(acc)
            if not row:
                terminal[acc]={"final_state":"unsupported_or_exhausted","reason_code":"bridge_result_missing"}; continue
            cand=Path(row.get("candidate_path") or "")
            if not cand.is_file():
                terminal[acc]={"final_state":"unsupported_or_exhausted","reason_code":"bridge_candidate_missing"}; continue
            rec=validator_gate(args,acc,cand,f"pass{pass_no:02d}_bridge_candidate",out); append_validation(validation_rows,rec,"bridge_candidate")
            state=terminal_from_bridge(row)
            # Precise bridge-v2 terminals remain bounded holds for this first closure harness.
            # Evidence enrichment / ontology resolver can be added without weakening the gate.
            if state in {"row_mapping_required","ontology_mapping_required","provenance_conflict","template_gap"}:
                terminal[acc]={"final_state":state,"reason_code":row.get("reason_code","")}; continue
            if state=="needs_independent_review":
                # Preserve review-ready bytes and gate projection before review. Review execution is
                # intentionally deferred to the full Codon harness where evidence bundle paths exist.
                terminal[acc]={"final_state":"needs_independent_review","reason_code":row.get("reason_code",""),"candidate_path":str(cand),"projected_sha256":row.get("projected_sha256","")}; continue
            if pass_no < args.max_closure_passes:
                dest=next_root/f"{acc}.sdrf.tsv"; shutil.copy2(cand,dest); next_active.append(acc)
            else:
                terminal[acc]={"final_state":state,"reason_code":row.get("reason_code","")}
        current_root=next_root; active=next_active
    fields=["accession","initial_candidate_sha256","final_state","reason_code","projected_sha256","all_submission_boundaries_green"]
    ledger_out=out/"closure_ledger.tsv"
    with ledger_out.open("w",newline="",encoding="utf-8") as fh:
        w=csv.DictWriter(fh,fieldnames=fields,delimiter="\t",lineterminator="\n"); w.writeheader()
        for acc in accs:
            info=terminal.get(acc,{"final_state":"unsupported_or_exhausted","reason_code":"closure_incomplete"})
            w.writerow({"accession":acc,"initial_candidate_sha256":original_sha.get(acc,""),"final_state":info.get("final_state",""),"reason_code":info.get("reason_code",""),"projected_sha256":info.get("projected_sha256",""),"all_submission_boundaries_green":"false"})
    val_out=out/"validation_ledger.tsv"
    with val_out.open("w",newline="",encoding="utf-8") as fh:
        fields2=["accession","stage","boundary","candidate_sha256","validator_gate","validator_version","runtime_sha256"]
        w=csv.DictWriter(fh,fieldnames=fields2,delimiter="\t",lineterminator="\n",extrasaction="ignore"); w.writeheader(); w.writerows(validation_rows)
    counts=Counter(x.get("final_state","") for x in terminal.values())
    summary={"controller":VERSION,"accessions":len(accs),"state_counts":dict(counts),"submission_ready":counts.get("submission_ready",0),"ledger":str(ledger_out),"validation_ledger":str(val_out),"validator_gated":True}
    (out/"closure_summary.json").write_text(json.dumps(summary,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(summary,indent=2)); return 0

if __name__=="__main__": raise SystemExit(main())
