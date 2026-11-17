"""Retain the four planned core pilots; stop before larger runs on any failure."""
from datetime import datetime
from pathlib import Path
import json
import os
import subprocess
import sys
import time

from jamboree_sdv.common import EXPERIMENT,WORK,WORKSPACE,digest,now,write_json


def stamp():
    return datetime.now().astimezone().strftime('%Y-%m-%dT%H-%M-%S.%f%z')


def main():
    group=WORK/'runs'/('pilot-suite-'+stamp());group.mkdir(parents=True,exist_ok=False)
    env=os.environ.copy();env.update(PYTHONDONTWRITEBYTECODE='1',TMPDIR=str(WORK/'tmp'))
    records=[]
    try:
        for patients in [100,1000]:
            for label,version in [('v1','1.1.0'),('v2','2.0.0')]:
                run_id=stamp();recipe=EXPERIMENT/f'recipes/pilot-core-{patients}-{label}-replay.json'
                run=WORK/'runs'/run_id;data=WORK/'data'/run_id;manifest=WORK/'releases'/(run_id+'.json')
                validation=WORK/'validation'/f'sdv-core-{patients}'/f'rules-v{version}'/run_id
                pre=WORK/'runs'/(run_id+'-preflight')
                record={'patients':patients,'rules_version':version,'run_id':run_id,'recipe':str(recipe),
                        'run':str(run),'manifest':str(manifest),'validation':str(validation),'stages':[]}
                records.append(record)
                commands=[
                    ('preflight',['--out',str(pre)]),
                    ('generate',['--preflight',str(pre/'preflight.json'),'--run-out',str(run),'--data-out',str(data),'--manifest-out',str(manifest)]),
                    ('audit',['--manifest',str(manifest),'--out',str(validation/'audit')]),
                    ('assess',['--manifest',str(manifest),'--run',str(run),'--audit',str(validation/'audit/audit.json'),'--out',str(validation/'assessment')]),
                ]
                for stage,options in commands:
                    cmd=[sys.executable,'-B','-m','jamboree_sdv.cli',stage,'--recipe',str(recipe),*options]
                    start=time.perf_counter();proc=subprocess.run(cmd,cwd=WORKSPACE,env=env,text=True,capture_output=True)
                    log=group/f'{patients}-{label}-{stage}.log';log.write_text(proc.stdout+proc.stderr)
                    record['stages'].append({'stage':stage,'command':cmd,'exit_code':proc.returncode,'wall_seconds':time.perf_counter()-start,'log':str(log)})
                    print(f'{patients} patients / {label} / {stage}: exit {proc.returncode}',flush=True)
                    if proc.returncode not in ([0,1] if stage=='audit' else [0]):
                        raise RuntimeError(f'{stage} failed; see {log}')
                assessment=validation/'assessment/assessment.json'
                result=json.loads(assessment.read_text())
                if result['status']!='passed_core_pilot':raise RuntimeError('Scoped pilot failed')
                record.update(status=result['status'],assessment=str(assessment),assessment_sha256=digest(assessment))
        write_json(group/'summary.json',{'created_at':now(),'status':'passed_core_pilots','runs':records,'full_release_ready':False,'promotion':False})
        print('PILOT_SUMMARY='+str(group/'summary.json'),flush=True)
        return 0
    except Exception as exc:
        write_json(group/'failure.json',{'created_at':now(),'status':'incomplete','error':str(exc),'runs':records})
        raise


if __name__=='__main__':
    raise SystemExit(main())
