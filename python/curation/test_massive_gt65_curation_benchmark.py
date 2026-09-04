#!/usr/bin/env python3
from __future__ import annotations
import csv, json, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "python/curation/build_massive_gt65_curation_benchmark.py"
EVAL = ROOT / "python/evaluation/evaluate_massive_gt65_curation_benchmark.py"


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(x)+"\n" for x in rows), encoding="utf-8")

def write_tsv(path, rows, fields):
    with path.open("w", encoding="utf-8", newline="") as h:
        w=csv.DictWriter(h,fieldnames=fields,delimiter="\t"); w.writeheader(); w.writerows(rows)

def main():
    with tempfile.TemporaryDirectory() as td:
        d=Path(td); out=d/'build'; ev=d/'eval'
        accepted=[
            {'accession':'PXD000001','score':50,'tier':'strong','dataset_title':'pxd one'},
            {'accession':'MSV000000001','score':80,'tier':'strong','dataset_title':'msv one'},
            {'accession':'PXD000002','score':30,'tier':'possible','dataset_title':'pxd two'},
        ]
        bridge=[{'accession':'MSV000000001','score':80,'tier':'strong','native_evidence_path':'/tmp/native.json','trusted_pxd_aliases':['PXD000001']}]
        recall=[
            {'gt_accession':'MSVGT1','matched_candidates':'MSV000000001; PXD000001','recovered':'yes','recovery_route':'native_and_pxd','dataset_title':'one'},
            {'gt_accession':'MSVGT2','matched_candidates':'PXD000002','recovered':'yes','recovery_route':'pxd_alias','dataset_title':'two'},
        ]
        write_jsonl(d/'accepted.jsonl',accepted); write_jsonl(d/'bridge.jsonl',bridge)
        write_tsv(d/'recall.tsv',recall,list(recall[0]))
        subprocess.check_call([sys.executable,str(BUILD),'--accepted-candidates-jsonl',str(d/'accepted.jsonl'),'--native-bridge-candidates-jsonl',str(d/'bridge.jsonl'),'--recall-audit-tsv',str(d/'recall.tsv'),'--output-dir',str(out),'--expect-gt-rows','2'])
        built=[json.loads(x) for x in (out/'massive_gt65_benchmark_candidates.jsonl').read_text().splitlines()]
        assert {x['accession'] for x in built}=={'MSV000000001','PXD000002'}
        assert all(not any(k.startswith('gt_') for k in x) for x in built)
        manifest=list(csv.DictReader((out/'massive_gt65_benchmark_manifest.tsv').open(),delimiter='\t'))
        assert manifest[0]['representation_class']=='native_msv_with_bridge'
        cur=[
            {'accession':'MSV000000001','run_status':'success','curation_decision':'include','decision_reason':'ok','evidence_sufficiency':'sufficient','evidence_item_count':'10','deterministic_evidence_item_count':'11','publication_annotation_count':'0','validation_warning_count':'0','result_path':'a'},
            {'accession':'PXD000002','run_status':'success','curation_decision':'review','decision_reason':'uncertain','evidence_sufficiency':'partial','evidence_item_count':'5','deterministic_evidence_item_count':'5','publication_annotation_count':'1','validation_warning_count':'0','result_path':'b'},
        ]
        write_tsv(d/'cur.tsv',cur,list(cur[0]))
        subprocess.check_call([sys.executable,str(EVAL),'--manifest',str(out/'massive_gt65_benchmark_manifest.tsv'),'--curation-summary',str(d/'cur.tsv'),'--output-dir',str(ev),'--expect-gt-rows','2'])
        summary=json.loads((ev/'massive_gt65_curation_benchmark_summary.json').read_text())
        assert summary['overall']['includes']==1 and summary['overall']['reviews']==1 and summary['overall']['hard_false_negatives']==0
    print('MassIVE GT65 curation benchmark tests passed')

if __name__=='__main__': main()
