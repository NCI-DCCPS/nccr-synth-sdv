"""Immutable local candidates with deterministic ZIP members and explicit paths."""
from collections import Counter
import csv
import io
import resource
import sys
import time
import zipfile
from .common import digest,now,output_path,read_json,write_json
from .preflight import compile_recipe
from .provenance import retain_preflight
from .sources import CoreSources


def zip_info(name):
    info=zipfile.ZipInfo(name,date_time=(1980,1,1,0,0,0))
    info.compress_type=zipfile.ZIP_DEFLATED
    info.external_attr=0o100644<<16
    return info


def generate(recipe_path,preflight_path,run_out,data_out,manifest_out):
    run,data,manifest=(output_path(p) for p in [run_out,data_out,manifest_out])
    paths=[run,data,manifest]
    if any(a==b or a.is_relative_to(b) or b.is_relative_to(a) for i,a in enumerate(paths) for b in paths[i+1:]):
        raise ValueError('Generation destinations must be distinct and non-nested')
    recipe,current=compile_recipe(recipe_path)
    retained=read_json(preflight_path);retained.pop('created_at',None)
    if retained!=current:raise ValueError('Stale or altered preflight inputs, code, environment or settings')
    start=time.perf_counter()
    run.mkdir(parents=True,exist_ok=False);data.mkdir(parents=True,exist_ok=False)
    manifest.parent.mkdir(parents=True,exist_ok=True)
    try:
        archived=retain_preflight(current,run)
        generator=CoreSources(recipe,current['adapted']);files=[];coverage=[]
        for source in recipe.spec['sources']:
            path=data/f'{source}.zip';header=current['adapted']['headers'][source]
            blanks=Counter();rows=0
            with zipfile.ZipFile(path,'x') as archive:
                with archive.open(zip_info(source+'.csv'),'w') as binary:
                    with io.TextIOWrapper(binary,encoding='utf-8',newline='') as stream:
                        writer=csv.DictWriter(stream,fieldnames=header,lineterminator='\n');writer.writeheader()
                        for row in generator.rows(source):
                            writer.writerow(row);rows+=1;blanks.update(k for k,v in row.items() if v=='')
                dictionary=recipe.snapshot/'metadata'/f'{source.lower()}_metadata.json'
                archive.writestr(zip_info(dictionary.name),dictionary.read_bytes())
            files.append({'source':source,'local_path':str(path),'sha256':digest(path),'rows':rows,'size_bytes':path.stat().st_size})
            coverage.extend({'source':source,'field':field,'rows':rows,'blank_cells':blanks[field],
                             'mode':current['adapted']['fields'][source+'.'+field]['mode']} for field in header)
        # Detect concurrent changes before declaring the candidate complete.
        _,after=compile_recipe(recipe_path)
        if after!=current:raise ValueError('Input or implementation changed during generation')
        record={'schema_version':1,'kind':'gen_sdv_core_candidate','created_at':now(),
                'recipe':recipe.spec,'rules_version':recipe.rules_spec['version'],'scope':recipe.contract,
                'preflight':{'path':str(preflight_path),'sha256':digest(preflight_path)},
                'files':files,'archives':archived,'environment':current['environment'],
                'metrics':{'wall_seconds':time.perf_counter()-start,
                           'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024),
                           'disk_payload_bytes':sum(f['size_bytes'] for f in files)},'promotion':False}
        write_json(run/'coverage.json',coverage)
        write_json(run/'run-manifest.json',record)
        write_json(manifest,{'release_id':recipe.spec['recipe_id'],'kind':'local_core_pilot_candidate','scope':recipe.spec['sources'],
                             'rules':recipe.spec['rules'],'run_manifest':{'path':str(run/'run-manifest.json'),'sha256':digest(run/'run-manifest.json')},'files':files})
        return record
    except Exception as exc:
        write_json(run/'failure.json',{'status':'incomplete','created_at':now(),'error':str(exc)})
        raise
