"""One local CLI run: resolve size, pin prerequisites, generate, audit, assess."""
from datetime import datetime
from pathlib import Path
import os
import subprocess
import sys
import time
from .common import EXPERIMENT,WORK,WORKSPACE,checked_pin,read_json,write_json,pin,shared_tools,local_verifiers
from .sizing import scaled_targets


def select_prerequisites(spec,common,explicit):
    if spec['schema_version']==4:
        local_verifiers()
        from integrated_prerequisites import required_sizes,verify_prerequisite
    else:
        shared_tools()
        from candidate_evidence import required_sizes,verify_prerequisite
    needed=required_sizes(spec)
    if explicit:
        pins=[pin(Path(p)) for p in explicit]
        for p in pins:verify_prerequisite(p,spec,common)
        return pins
    selected={}
    candidates=sorted(WORK.glob('validation/**/assessment/assessment.json'),key=lambda p:p.stat().st_mtime,reverse=True)
    for path in candidates:
        if set(needed)<=set(selected):break
        try:
            candidate=read_json(path);n=candidate.get('observed_counts',{}).get('CTC',{}).get('distinct_patients')
            if n not in needed or n in selected or not candidate.get('status','').startswith('passed_'):continue
            item=pin(path);verify_prerequisite(item,spec,common);selected[n]=item
        except (OSError,ValueError,KeyError,TypeError):continue
    missing=set(needed)-set(selected)
    if missing:raise ValueError('Run and pass prerequisite patient size(s) first: '+str(sorted(missing)))
    return [selected[n] for n in needed]


def run(recipe_path,patients=None,scale=None,prerequisites=()):
    if patients is not None and scale is not None:raise ValueError('Choose --patients or --scale, not both')
    template=Path(recipe_path).resolve();spec=read_json(template)
    if spec.get('schema_version') not in [3,4]:raise ValueError('Runtime size parameters require a scalable all-sources recipe')
    contract=read_json(checked_pin(spec['contract']));common={k:read_json(checked_pin(v)) for k,v in contract['common_inputs'].items()}
    request={'kind':'patients','value':patients} if patients is not None else ({'kind':'scale','value':scale} if scale is not None else spec['size'])
    size,n,targets=scaled_targets(common['generation-contract.json'],request,contract)
    label='sdv-all-v2_3_0' if spec['schema_version']==4 else 'sdv-all-v2'
    if 'tumor-multiplicity' in spec['profiles']:label+='-multiplicity-v1'
    spec.update(size=size,patients=n,template=pin(template),recipe_id=f'{label}-{size["kind"]}-{size["value"]}')
    spec['prerequisites']=select_prerequisites(spec,common,prerequisites)
    stamp=datetime.now().astimezone().strftime('%Y-%m-%dT%H-%M-%S.%f%z');run_id=f'{spec["recipe_id"]}-{stamp}'
    case=WORK/'runs'/run_id;case.mkdir(parents=True,exist_ok=False)
    effective=case/'effective-recipe.json';write_json(effective,spec)
    data=WORK/'data'/run_id;manifest=WORK/'releases'/(run_id+'.json');rules=read_json(checked_pin(spec['rules']))['version']
    validation=WORK/'validation'/spec['recipe_id']/('rules-v'+rules)/stamp
    commands=[['preflight','--out',str(case/'preflight')],
              ['generate','--preflight',str(case/'preflight/preflight.json'),'--run-out',str(case/'generation'),'--data-out',str(data),'--manifest-out',str(manifest)],
              ['audit','--manifest',str(manifest),'--out',str(validation/'audit')],
              ['assess','--manifest',str(manifest),'--run',str(case/'generation'),'--audit',str(validation/'audit/audit.json'),'--out',str(validation/'assessment')]]
    record={'schema_version':1,'size':size,'patients':n,'targets':targets,'recipe':pin(effective),'recipe_path':str(effective),
            'run':str(case/'generation'),'data':str(data),'manifest':str(manifest),'validation':str(validation),'stages':[],'promotion':False}
    write_json(case/'request.json',{'command':[sys.executable,*sys.argv],'template':pin(template),'size':size,'prerequisites':spec['prerequisites']})
    print('RUN_DIRECTORY='+str(case),flush=True)
    env=os.environ.copy();env.update(PYTHONDONTWRITEBYTECODE='1',TMPDIR=str(WORK/'tmp'))
    try:
        for args in commands:
            command=[sys.executable,'-B','-m','jamboree_sdv.cli',args[0],'--recipe',str(effective),*args[1:]]
            print(f'Starting {args[0]} for {n:,} patients',flush=True);start=time.perf_counter()
            log=case/(args[0]+'.log')
            with log.open('x') as stream:process=subprocess.run(command,cwd=WORKSPACE,env=env,stdout=stream,stderr=subprocess.STDOUT)
            record['stages'].append({'command':command,'exit_code':process.returncode,'wall_seconds':time.perf_counter()-start,'log':str(log)})
            print(f'{args[0]}: exit {process.returncode}',flush=True)
            if process.returncode not in ([0,1] if args[0]=='audit' else [0]):raise ValueError(f'{args[0]} failed; see {log}')
        assessment=validation/'assessment/assessment.json';result=read_json(assessment)
        record.update(status=result['status'],assessment=pin(assessment),counts=result['observed_counts'],
                      generation_metrics=result['metrics'],assessment_metrics=result['assessment_metrics'],
                      audit_metrics=read_json(validation/'audit/audit.json')['metrics'])
        write_json(case/'result.json',record)
        print('ASSESSMENT='+str(assessment),flush=True);return record
    except Exception as exc:
        write_json(case/'failure.json',{**record,'status':'incomplete','error':str(exc)});raise
