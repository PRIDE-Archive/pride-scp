#!/usr/bin/env python3
"""Freeze a three-accession exact-hash review bundle after revoked release20 repairs."""
from __future__ import annotations
import argparse, csv, hashlib, json, shutil, tarfile
from pathlib import Path

TARGETS = ["PXD019515", "PXD019958", "PXD062702"]

def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()

def copy_if(src: Path, dst: Path) -> bool:
    if not src.is_file(): return False
    dst.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(src, dst); return True

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--repo', required=True, type=Path)
    ap.add_argument('--readiness-output', required=True, type=Path)
    ap.add_argument('--repair-output', required=True, type=Path)
    ap.add_argument('--output-dir', required=True, type=Path)
    ap.add_argument('--archive', required=True, type=Path)
    ns=ap.parse_args()
    out=ns.output_dir
    if out.exists(): shutil.rmtree(out)
    out.mkdir(parents=True)
    rows=[]
    for acc in TARGETS:
        rj=ns.readiness_output/'accessions'/f'{acc}.readiness.json'
        if not rj.is_file(): raise SystemExit(f'{acc}: missing readiness JSON')
        obj=json.loads(rj.read_text())
        if obj.get('state') != 'needs_independent_review':
            raise SystemExit(f"{acc}: expected needs_independent_review after deterministic validation, got {obj.get('state')}")
        projected=ns.readiness_output/'projected'/acc/f'{acc}.sdrf.tsv'
        candidate=ns.repair_output/'candidates'/acc/f'{acc}.sdrf.tsv'
        repair_manifest=ns.repair_output/'manifests'/f'{acc}.repair.json'
        for p in (projected,candidate,repair_manifest):
            if not p.is_file(): raise SystemExit(f'{acc}: missing {p}')
        digest=sha256(projected)
        expected=str(obj.get('projected_sha256') or '').lower()
        if digest != expected: raise SystemExit(f'{acc}: projected SHA mismatch')
        ad=out/'accessions'/acc
        copy_if(projected, ad/f'{acc}.projected.sdrf.tsv')
        copy_if(candidate, ad/f'{acc}.repaired_candidate.sdrf.tsv')
        copy_if(repair_manifest, ad/f'{acc}.repair.json')
        copy_if(rj, ad/f'{acc}.readiness.json')
        # Source evidence used by the original curation lane.
        base=ns.repo/'data'/'sdrf_annotation_gt105_pride_v031_full'
        for sub,suffix in [('evidence','.evidence.json'),('audit','.sdrf.audit.json'),('review','.sdrf.review.tsv'),('proposals','.ollama.json')]:
            copy_if(base/sub/f'{acc}{suffix}', ad/'evidence'/sub/f'{acc}{suffix}')
        copy_if(ns.repo/'data'/'snapshot'/'projects'/f'{acc}.json', ad/'evidence'/'snapshot'/f'{acc}.project.json')
        copy_if(ns.repo/'data'/'snapshot'/'files'/f'{acc}.json', ad/'evidence'/'snapshot'/f'{acc}.files.json')
        rows.append({'accession':acc,'projected_sha256':digest,'review_status':'pending','readiness_state':obj.get('state','')})
    inv=out/'review_inventory.tsv'
    with inv.open('w',newline='') as fh:
        w=csv.DictWriter(fh,delimiter='\t',fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    manifest=[]
    for p in sorted(x for x in out.rglob('*') if x.is_file()):
        if p.name=='SHA256SUMS': continue
        manifest.append(f'{sha256(p)}  {p.relative_to(out)}')
    (out/'SHA256SUMS').write_text('\n'.join(manifest)+'\n')
    ns.archive.parent.mkdir(parents=True,exist_ok=True)
    with tarfile.open(ns.archive,'w:gz') as tf: tf.add(out,arcname=out.name)
    print(f'review_accessions={len(rows)}')
    print(f'archive={ns.archive}')
    print(f'sha256={sha256(ns.archive)}')
if __name__=='__main__': main()
