"""Sequential all-source gates: 100 patients before 1,000, both rule versions."""
from pathlib import Path
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from jamboree_sdv.common import EXPERIMENT,WORK,WORKSPACE,write_json


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--patients',nargs='+',type=int,default=[100,1000]);args=parser.parse_args()
    if args.patients not in [[100],[100,1000]]:raise ValueError('Run the 100-patient gate first')
    stamp=datetime.now().astimezone().strftime('%Y-%m-%dT%H-%M-%S.%f%z')
    suite=WORK/'runs'/('all-source-suite-'+stamp);suite.mkdir(parents=True,exist_ok=False)
    results=[]
    for n in args.patients:
        for version in ['v1','v2']:
            case=suite/f'{n}-{version}';case.mkdir();recipe=EXPERIMENT/'recipes'/f'pilot-all-{n}-{version}.json'
            data=WORK/'data'/f'all-{stamp}-{n}-{version}';manifest=WORK/'releases'/f'all-{stamp}-{n}-{version}.json'
            spec=json.loads(recipe.read_text());rule=json.loads((WORKSPACE/spec['rules']['path']).read_text())['version']
            validation=WORK/'validation'/f'sdv-all-{n}-{version}'/('rules-v'+rule)/stamp
            validation.mkdir(parents=True,exist_ok=False)
            commands=[['preflight','--out',str(case/'preflight')],
                ['generate','--preflight',str(case/'preflight/preflight.json'),'--run-out',str(case/'run'),'--data-out',str(data),'--manifest-out',str(manifest)],
                ['audit','--manifest',str(manifest),'--out',str(validation/'audit')],
                ['assess','--manifest',str(manifest),'--run',str(case/'run'),'--audit',str(validation/'audit/audit.json'),'--out',str(validation/'assessment')]]
            stages=[]
            for stage in commands:
                cmd=[sys.executable,'-B','-m','jamboree_sdv.cli',stage[0],'--recipe',str(recipe),*stage[1:]]
                start=time.perf_counter();process=subprocess.run(cmd,cwd=WORKSPACE,text=True,capture_output=True)
                stages.append({'command':cmd,'exit_code':process.returncode,'wall_seconds':time.perf_counter()-start})
                (case/(stage[0]+'.log')).write_text(process.stdout+process.stderr)
                print(f'{n} {version} {stage[0]}: {process.returncode}',flush=True)
                if process.returncode not in ([0,1] if stage[0]=='audit' else [0]):
                    write_json(suite/'failure.json',{'case':str(case),'stage':stage[0],'exit_code':process.returncode,'output':process.stdout+process.stderr});raise SystemExit(1)
            assessment=json.loads((validation/'assessment/assessment.json').read_text())
            results.append({'patients':n,'rules':version,'rules_version':rule,'case':str(case),'manifest':str(manifest),'data':str(data),
                'validation':str(validation),'status':assessment['status'],'counts':assessment['observed_counts'],'metrics':assessment['metrics'],
                'audit_metrics':json.loads((validation/'audit/audit.json').read_text())['metrics'],
                'assessment_metrics':assessment['assessment_metrics'],'stages':stages})
            write_json(case/'result.json',results[-1])
    write_json(suite/'summary.json',{'status':'passed_all_source_pilots','cases':results,'promotion':False})
    print(suite)


if __name__=='__main__':main()
