#!/usr/bin/env python3
from __future__ import annotations
import csv, importlib.util, json, subprocess, sys, tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]; BRIDGE=ROOT/'python/curation/build_massive_native_curation_bridge.py'; CURATION=ROOT/'python/curation/pride_scp_curation_v19.py'
def load_module(path,name):
    spec=importlib.util.spec_from_file_location(name,path); mod=importlib.util.module_from_spec(spec); assert spec and spec.loader; spec.loader.exec_module(mod); return mod
def write_tsv(path,rows,fields):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',encoding='utf-8',newline='') as h:
        w=csv.DictWriter(h,fieldnames=fields,delimiter='\t'); w.writeheader(); w.writerows(rows)
def test_bridge_and_msv_routing():
    with tempfile.TemporaryDirectory() as td:
        t=Path(td); snapshot=t/'snapshot'; massive=snapshot/'native/massive'; projects=massive/'projects'; cache=massive/'identity_enrichment'; projects.mkdir(parents=True); (cache/'massive_proxi').mkdir(parents=True)
        msv='MSV000123456'; project={'accession':msv,'pxdAliases':['PXD012345'],'sourceIdentity':{'canonical_dataset_identity':'dataset_identity:test','pxd_aliases':['PXD012345'],'publication_dois':['10.1000/example'],'publication_urls':['https://doi.org/10.1000/example']},'massiveNativeRecord':{'title':'Single-cell proteomics test','description':'Individual cells were analyzed by LC-MS/MS.'}}
        (projects/f'{msv}.json').write_text(json.dumps(project))
        (cache/'massive_proxi'/f'{msv}.json').write_text(json.dumps({'title':'Single-cell proteomics test','dataFiles':[{'name':'Associated raw file URI','value':'https://example/x.raw'},{'name':'metadata','value':'https://example/README.txt'}],'publications':[{'doi':'10.1000/example'}]}))
        candidate={'accession':msv,'dataset_title':'Single-cell proteomics test','dataset_description':'Individual cells were analyzed by LC-MS/MS.','score':100,'tier':'strong','hits':[],'project_json_path':str(projects/f'{msv}.json'),'files_json_path':'','sdrf_path':''}
        candidates=t/'candidates.jsonl'; candidates.write_text(json.dumps(candidate)+'\n')
        delta=t/'delta.tsv'; write_tsv(delta,[{'candidate_accession':msv}],['candidate_accession'])
        diag=t/'diag.tsv'; write_tsv(diag,[{'accession':msv,'semantic_priority':'A_specific'}],['accession','semantic_priority'])
        out=t/'out'; subprocess.run([sys.executable,str(BRIDGE),'--candidates-jsonl',str(candidates),'--delta-tsv',str(delta),'--candidate-diagnostics',str(diag),'--snapshot',str(snapshot),'--output-dir',str(out),'--expect-candidates','1'],check=True,capture_output=True,text=True)
        summary=json.loads((out/'massive_native_curation_bridge_summary.json').read_text()); assert summary['candidate_count']==1 and summary['gt_used_for_bridge'] is False and summary['trusted_pxd_alias_candidate_count']==1 and summary['file_inventory_coverage']==1
        augmented=json.loads((out/'massive_native_curation_candidates.jsonl').read_text().strip()); assert augmented['trusted_pxd_aliases']==['PXD012345']; assert Path(augmented['native_evidence_path']).is_file()
        cur=load_module(CURATION,'curation_v19_bridge_test'); annotation={'target_accession':'PXD012345','_annotation_path':'/tmp/PXD012345.json','_annotation_declared_target':'PXD012345','_annotation_route_kind':'explicit_match'}
        docs,identity=cur._candidate_annotations({'PXD012345':[annotation]},augmented); assert identity['trusted_pxd_aliases']==['PXD012345'] and len(docs)==1 and docs[0]['_annotation_identity_route']=='PXD012345' and docs[0]['_annotation_target_accession']==msv
        items=cur.build_evidence_items(augmented,[],max_items=18,max_chars=14000); assert any('native_bridge' in x['source_label'] for x in items)
def test_selected_accession_regex_supports_msv():
    cur=load_module(CURATION,'curation_v19_regex_test'); assert cur.ACCESSION_RE.fullmatch('PXD012345'); assert cur.ACCESSION_RE.fullmatch('MSV000123456'); assert not cur.ACCESSION_RE.fullmatch('MSV123')
if __name__=='__main__': test_bridge_and_msv_routing(); test_selected_accession_regex_supports_msv(); print('MassIVE native curation bridge tests passed')
