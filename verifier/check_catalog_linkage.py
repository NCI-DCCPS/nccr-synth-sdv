"""Independent checks for a catalog-constrained CCDI/CPI replacement; no generator imports."""
import argparse
import ast
from collections import Counter
from copy import deepcopy
import csv
import hashlib
import io
import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import zipfile


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def csv_zip(path):
    with zipfile.ZipFile(path) as z:
        names=z.namelist()
        require(len(names)==len(set(names)) and all(Path(n).name==n for n in names),'Unsafe ZIP members')
        require(z.testzip() is None,'ZIP CRC failure')
        data=[n for n in names if n.endswith('.csv')]
        require(len(data)==1,'Expected one CSV')
        return z.read(data[0]),{n:z.read(n) for n in names if n!=data[0]}


def mappings(payload):
    rows=list(csv.reader(io.StringIO(payload.decode('utf-8'),newline='')))
    require(rows and rows[0]==['nccrId','resourceParticipantId','resourceName'],'Invalid CPI schema')
    require(all(len(r)==3 and all(r) for r in rows[1:]),'Incomplete CPI row')
    return list(map(tuple,rows[1:]))


def extracted_class(path,name,method):
    tree=ast.parse(Path(path).read_text())
    node=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name==name)
    node.bases=[]
    node.body=[n for n in node.body if isinstance(n,ast.FunctionDef) and n.name==method]
    require(len(node.body)==1,'Expected ETL method missing')
    scope={'Any':Any,'json':json}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),str(path),'exec'),scope)
    return scope[name]


def verify(record):
    for item in record['inputs'].values():
        require(sha(item['path'])==item['sha256'],'Input changed: '+item['path'])
    files={f['source']:f for f in record['files']}
    baseline={f['source']:f for f in record['baseline_files']}
    sources={'CTC','ABM','COG','MCE','MCD','MCP','PHARM','RO','CCDI','CPI'}
    require(set(files)==set(baseline)==sources and len(record['files'])==10,'Incomplete source inventory')
    for inventory in [files,baseline]:
        for source,item in inventory.items():
            p=Path(item['local_path'])
            require(sha(p)==item['sha256'] and p.stat().st_size==item['size_bytes'],'Payload changed: '+source)
    for source in sources-{'CCDI','CPI'}:
        require(files[source]['sha256']==baseline[source]['sha256'],'Other source changed: '+source)
    original=mappings(Path(baseline['CPI']['local_path']).read_bytes())
    cpi=mappings(Path(files['CPI']['local_path']).read_bytes())
    require([(r[0],r[1]) for r in cpi]==[(r[0],r[1]) for r in original],'Patient/participant identities or row count changed')
    require(len({r[1].lower() for r in cpi})==len(cpi),'Ambiguous participant identity')
    wide,metadata=csv_zip(files['CCDI']['local_path'])
    _,old_metadata=csv_zip(baseline['CCDI']['local_path'])
    require(metadata==old_metadata,'CCDI metadata changed')
    rows=list(csv.reader(io.StringIO(wide.decode('utf-8'),newline='')))
    width=(len(rows[0])-1)//2 if rows else 0
    require(width>0 and rows[0]==['nccrId']+[f'{f}{i}' for i in range(1,width+1) for f in ['resourceParticipantId','resourceName']], 'Invalid raw CCDI header')
    seen=set();triples=[]
    for row in rows[1:]:
        require(len(row)==len(rows[0]) and row[0] and row[0].lower() not in seen,'Invalid or duplicate raw patient')
        seen.add(row[0].lower());ended=False
        for i in range(1,len(row),2):
            pair=row[i:i+2]
            if pair==['','']:
                require(i!=1,'Patient has no mapping');ended=True
            else:
                require(all(pair) and not ended,'Incomplete pair or gap');triples.append((row[0],*pair))
    require(Counter(triples)==Counter(cpi),'Raw CCDI and CPI differ')
    require(Counter(r[0] for r in triples)==Counter(r[0] for r in original),'Patient coverage or mapping counts changed')
    ctc,_=csv_zip(files['CTC']['local_path'])
    patient_ids={r['nccrId'] for r in csv.DictReader(io.StringIO(ctc.decode('utf-8')))}
    require({r[0] for r in cpi}<=patient_ids,'Patient absent from CTC')
    catalog=json.loads(Path(record['inputs']['catalog']['path']).read_text())
    domains={d['domain_name'] for d in catalog}
    require(len(domains)==len(catalog),'Duplicate catalog names')
    counts=Counter(r[2] for r in cpi)
    missing=sorted(set(counts)-domains)
    require(not missing,'Resource names missing from catalog: '+', '.join(missing))
    policy=json.loads(Path(record['inputs']['policy']['path']).read_text())
    original_distribution=json.loads(Path(record['inputs']['preflight']['path']).read_text())['adapted']['fields']['CCDI.resourceName']['distribution']
    selected=[(c,p) for c,p in zip(original_distribution['categories'],original_distribution['probabilities']) if c in domains and p>0]
    mass=sum(p for _,p in selected)
    require(policy['categories']==[c for c,_ in selected] and math.isclose(policy['retained_probability_mass'],mass), 'Incorrect conditional support')
    require(len(policy['probabilities'])==len(selected) and all(math.isclose(a,p/mass,rel_tol=1e-12) for a,(_,p) in zip(policy['probabilities'],selected)), 'Incorrect conditional weights')
    alpha=.001;bound=math.sqrt(math.log(2*len(selected)/alpha)/(2*len(cpi)))
    deviations={c:abs(counts[c]/len(cpi)-p/mass) for c,p in selected}
    require(not set(counts)-{c for c,_ in selected},'Domain lacks public probability support')
    require(max(deviations.values())<=bound,'Conditional domain marginal check failed')
    # Exercise the exact graph methods that raised the cloud error.
    platform=record['platform_files']
    for item in platform.values():require(sha(item['path'])==item['sha256'],'ETL test code changed')
    Graph=extracted_class(platform['graph_handler']['path'],'GenCcdiDqGraphsJob','_add_domain_info_to_metadata')
    Lookup=extracted_class(platform['graph_lookup']['path'],'GenDqGraphs','_lookup_permissible_value')
    class DB:
        def execute(self,query,query_params):self.name=query_params[0]
        def fetchone(self):return [counts[self.name]]
    job=Graph();job._db=DB();job._clean_bucket='local-catalog-check'
    catalog_bytes=Path(record['inputs']['catalog']['path']).read_bytes()
    job._s3=SimpleNamespace(get_object=lambda **kwargs:{'Body':io.BytesIO(catalog_bytes)})
    enriched=json.loads(metadata['ccdi_metadata.json'])
    job._add_domain_info_to_metadata(enriched)
    field=next(f for f in enriched['Dictionary_Elements'] if f['Column_Name_at_Source']=='resourceName')
    for name in counts:require(Lookup._lookup_permissible_value(name,field) is not None,'Missing graph description')
    return {'status':'passed_catalog_constrained_linkage','ccdi_patient_rows':len(seen),'mapping_rows':len(cpi),
            'raw_mapping_pair_columns':width,'observed_domains':len(counts),'supported_domains':len(selected),
            'catalog_domains':len(domains),'missing_domains':missing,'domain_counts':dict(sorted(counts.items())),
            'unchanged_other_sources':8,'patient_and_participant_ids_preserved':True,'metadata_preserved':True,
            'conditional_marginal_check':{'method':'simultaneous Hoeffding bounds','familywise_alpha':alpha,
                'maximum_absolute_deviation':max(deviations.values()),'bound':bound,'retained_public_mass':mass},
            'actual_graph_lookup_checks':len(counts),'full_cloud_etl_run':False,
            'limitation':'Resource probabilities are conditioned on the pinned catalog; original unconditional CCDI marginal assessment is not reused.'}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--record',required=True,type=Path)
    parser.add_argument('--out',required=True,type=Path)
    args=parser.parse_args()
    require(not args.out.exists(),'Refusing existing assessment')
    result=verify(json.loads(args.record.read_text()))
    result['record']={'path':str(args.record.resolve()),'sha256':sha(args.record)}
    result['verifier_sha256']=sha(__file__)
    with args.out.open('x') as stream:json.dump(result,stream,indent=2)
    print(json.dumps({k:result[k] for k in ['status','ccdi_patient_rows','mapping_rows','observed_domains','missing_domains','actual_graph_lookup_checks']}))
