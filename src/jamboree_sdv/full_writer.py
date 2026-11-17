"""Retained local all-source candidates, deterministic ZIPs and typed sidecars."""
from collections import Counter
import csv
import io
import resource
import sys
import time
import zipfile
from .common import digest,now,output_path,read_json,write_json
from .full_preflight import compile_recipe
from .full_sources import FullSources
from .provenance import retain_preflight
from .writer import zip_info
from .delivery import wide_ccdi, zip_csv
from .promotion_evidence import verify_raw_files


def write_context(generator,recipe,run):
    directory=run/'context';directory.mkdir()
    schema=recipe.common['context-schema.json'];manifest={'schema_version':1,'context_id':schema['context_id'],
              'context_schema_sha256':recipe.contract['common_inputs']['context-schema.json']['sha256']}
    for kind,rows in [('patient',generator.patients),('tumor',generator.tumors)]:
        path=directory/(kind+'.csv');header=[f['name'] for f in schema[kind]['fields']]
        with path.open('x',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=header,lineterminator='\n');writer.writeheader()
            for row in rows:
                writer.writerow({k:'' if v is None else (str(v).lower() if type(v) is bool else str(v)) for k,v in row.items()})
        manifest[kind]={'path':path.name,'sha256':digest(path),'rows':len(rows)}
    path=directory/'manifest.json';write_json(path,manifest)
    return {'path':str(path),'sha256':digest(path)}


def generate(recipe_path,preflight_path,run_out,data_out,manifest_out):
    run,data,manifest=(output_path(p) for p in [run_out,data_out,manifest_out]);paths=[run,data,manifest]
    if any(a==b or a.is_relative_to(b) or b.is_relative_to(a) for i,a in enumerate(paths) for b in paths[i+1:]):raise ValueError('Output destinations overlap')
    recipe,current=compile_recipe(recipe_path);retained=read_json(preflight_path);retained.pop('created_at',None)
    if retained!=current:raise ValueError('Stale or altered preflight')
    resources=None
    if recipe.spec['schema_version'] in [3,4]:
        from .resources import check_budget
        resources=check_budget(recipe)
    start=time.perf_counter();run.mkdir(parents=True,exist_ok=False);data.mkdir(parents=True,exist_ok=False);manifest.parent.mkdir(parents=True,exist_ok=True)
    try:
        archived=retain_preflight(current,run);generator=FullSources(recipe,current['adapted']);files=[];coverage=[];ccdi=[]
        if resources:write_json(run/'resources.json',resources)
        context=write_context(generator,recipe,run)
        (run/'processed').mkdir()
        for source in recipe.spec['sources']+['CPI']:
            header=current['adapted']['headers'][source] if source!='CPI' else recipe.common['generation-contract.json']['CPI']['columns']
            iterator=generator.rows(source) if source!='CPI' else iter(ccdi)
            if source=='PHARM' and recipe.spec['schema_version']==4:
                from .pharm_enrichment import iter_enriched
                from .random_streams import seed_for
                iterator=iter_enriched(iterator,current['adapted']['pharm_plan'],seed_for(recipe.spec['seed'],recipe.profiles['pharm-enrichment']['seed_stream']))
            # The dictionary describes processed CCDI. Keep that audit view out of raw data.
            path=(run/'processed' if source in ('CCDI','CPI') else data)/(source+'.zip');blanks=Counter();count=0;ids=set()
            with zipfile.ZipFile(path,'x') as archive:
                with archive.open(zip_info(source+'.csv'),'w') as binary,io.TextIOWrapper(binary,encoding='utf-8',newline='') as stream:
                    writer=csv.DictWriter(stream,fieldnames=header,lineterminator='\n');writer.writeheader()
                    for row in iterator:
                        writer.writerow(row);count+=1;ids.add(row['nccrId']);blanks.update(f for f,v in row.items() if v=='')
                        if source=='CCDI':ccdi.append(row)
                dictionary=recipe.snapshot/'metadata'/f'{("CCDI" if source=="CPI" else source).lower()}_metadata.json'
                archive.writestr(zip_info(source.lower()+'_metadata.json'),dictionary.read_bytes())
            files.append({'source':source,'local_path':str(path),'sha256':digest(path),'rows':count,'patients':len(ids),'size_bytes':path.stat().st_size})
            view='processed ' if source in ('CCDI','CPI') else ''
            print(f'{source}: wrote {count:,} {view}rows',flush=True)
            coverage.extend({'source':source,'field':field,'rows':count,'blank_cells':blanks[field],
                             'mode':current['adapted']['fields'][source+'.'+field]['mode'] if source!='CPI' else 'projection'} for field in header)
        raw_files=[]
        for item in files:
            source=item['source']
            target=data/(source+('.csv' if source=='CPI' else '.zip'))
            if source=='CCDI':
                member,payload=zip_csv(item['local_path'])
                with zipfile.ZipFile(item['local_path']) as original,zipfile.ZipFile(target,'x') as raw:
                    for entry in original.infolist():
                        raw.writestr(entry,wide_ccdi(payload) if entry.filename==member else original.read(entry))
            elif source=='CPI':
                _,payload=zip_csv(item['local_path'])
                with target.open('xb') as stream:stream.write(payload)
            raw_files.append({**item,'local_path':str(target),'sha256':digest(target),
                              'size_bytes':target.stat().st_size,
                              'rows':item['patients'] if source=='CCDI' else item['rows']})
        raw_checks=verify_raw_files(raw_files,files)
        print(f"CCDI raw: wrote {raw_checks['ccdi_patient_rows']:,} patient rows with {raw_checks['ccdi_mapping_rows']:,} mappings",flush=True)
        _,after=compile_recipe(recipe_path)
        if after!=current:raise ValueError('Inputs or implementation changed during generation')
        write_json(run/'coverage.json',coverage)
        record={'schema_version':2,'kind':'gen_sdv_all_source_candidate','created_at':now(),'recipe':recipe.spec,
                'rules_version':recipe.rules_spec['version'],'scope':recipe.contract,'context':context,'files':files,
                'raw_files':raw_files,'raw_layout_checks':raw_checks,
                'archives':archived,'coverage_sha256':digest(run/'coverage.json'),'environment':current['environment'],
                'resources':resources,
                'metrics':{'wall_seconds':time.perf_counter()-start,'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024),
                           'disk_payload_bytes':sum(f['size_bytes'] for f in files),
                           'disk_raw_export_bytes':sum(f['size_bytes'] for f in raw_files)},'promotion':False}
        write_json(run/'run-manifest.json',record)
        write_json(manifest,{'release_id':recipe.spec['recipe_id'],'kind':'local_all_source_pilot_candidate','scope':recipe.spec['sources']+['CPI'],
                            'rules':recipe.spec['rules'],'context':context,'context_contract':recipe.contract['common_inputs']['context-schema.json'],
                            'run_manifest':{'path':str(run/'run-manifest.json'),'sha256':digest(run/'run-manifest.json')},
                            'files':files,'raw_files':raw_files})
        return record
    except Exception as exc:
        write_json(run/'failure.json',{'status':'incomplete','created_at':now(),'error':str(exc)});raise
