"""Run integrated checks exclusively through the verified promotion mapping."""
from pathlib import Path
import resource
import sys
import time

from assess_core_pilot import read,sha
from audit_release import audit_file
from jamboree_sdv.integrated_inputs import load_rules
from integrated_rules import evaluate_all


def evaluate_shared(manifest,run,sources,rules,reader):
    physical={item['path'] for item in run['inputs']}|{run[k]['path'] for k in ['context','context_contract','rules']}
    def resolve(item):
        path=Path(item['path']) if item['path'] in physical else reader.path(item['path'],item['sha256'])
        if sha(path)!=item['sha256']:raise ValueError('Mapped input hash mismatch')
        return path
    original=read(run['origin']['run']['path'])
    candidate=read(run['origin']['manifest']['path'])
    if candidate['run_manifest']['sha256']!=run['origin']['run']['sha256']:raise ValueError('Candidate run binding mismatch')
    # Resolve the historical adapted-input pin; never read its original path.
    logical=Path(candidate['run_manifest']['path']).parent/'inputs/adapted/preflight.json'
    adapted=reader.json(logical,original['archives']['adapted_sha256'])
    return evaluate_all(run['recipe'],manifest,sources,rules,resolver=resolve,adapted=adapted)


def audit_shared(manifest,run,manifest_path,metadata,out,reader):
    start=time.perf_counter();spec,rules=load_rules(Path(run['rules']['path']))
    source_results=[];ids={}
    for item in manifest['files']:
        result,patients=audit_file(item,Path('/'),metadata if item['source']!='CPI' else None)
        source_results.append(result);ids[item['source']]=patients
    results,extensions=evaluate_shared(manifest,run,source_results,rules,reader)
    code=2 if any(r['status']=='error' for r in source_results) else int(any(r['status']!='passed_checks' for r in source_results) or any(r['status'] not in ['passed','disabled'] for r in results))
    here=Path(__file__).resolve()
    report={'report_schema_version':2,'release_id':manifest['release_id'],
        'audit':{'version':'2.0.0-integrated-shared','path':str(here),'sha256':sha(here),
                 'components':[item for item in run['verifier'] if item['path']!=str(here)]},
        'manifest':{'path':str(manifest_path),'sha256':sha(manifest_path)},
        'rules':{**run['rules'],'version':spec['version']},'context':manifest['context'],
        'context_contract':manifest['context_contract'],'sources':source_results,'rule_results':results,
        'extension_checks':extensions,'complete':True,'exit_code':code,
        'status':'error' if code==2 else ('findings_or_incomplete' if code else 'passed_checks'),
        'metrics':{'wall_seconds':time.perf_counter()-start,'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024)}}
    from verify_promoted import write
    out.mkdir(parents=True,exist_ok=False);write(out/'audit.json',report)
    return code
