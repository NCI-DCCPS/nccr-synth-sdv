"""Guarded persistence for the independent all-source pilot assessment."""
from .common import EXPERIMENT,WORKSPACE,now,output_path,pin,read_json,shared_tools,write_json,local_verifiers,full_code_pins
from .promotion_evidence import verify_raw_files
from .provenance import archive_pins
from .full_recipe import load_full_recipe
import resource
import sys
import time
from pathlib import Path


def assess_candidate(recipe_path,run_path,manifest_path,audit_path,out):
    destination=output_path(out);recipe=load_full_recipe(recipe_path);shared_tools()
    resources=None
    integrated=recipe.spec['schema_version']==4
    if recipe.spec['schema_version'] in [3,4]:
        if integrated:
            local_verifiers()
            from integrated_assessment import assess
        else:from assess_scaled_candidate import assess
        from .resources import snapshot
        resources=snapshot()
    else:from assess_all_source_pilot import assess
    start=time.perf_counter();result=assess(manifest_path,run_path,audit_path,recipe_path)
    manifest=read_json(manifest_path)
    run=read_json(Path(run_path)/'run-manifest.json')
    if 'raw_files' in manifest or 'raw_files' in run:
        try:
            result['raw_layout_checks']=verify_raw_files(manifest['raw_files'],manifest['files'])
            if run['raw_files']!=manifest['raw_files'] or run['raw_layout_checks']!=result['raw_layout_checks']:
                raise ValueError('Raw inventory/checks do not match generation')
        except (ValueError,OSError,KeyError) as exc:
            result['findings'].append('Raw layout: '+str(exc));result['status']='findings';result['local_candidate_validated']=False
    result['assessment_metrics']={'wall_seconds':time.perf_counter()-start,
        'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024)}
    if resources:
        okay=result['assessment_metrics']['peak_rss_bytes']<resources['available_memory_bytes']/2
        result['resource_checks']={**resources,'assessment_within_half_available_memory':okay,
            'status':'passed' if okay and not any('memory budget' in s for s in result['findings']) else 'failed'}
        if not okay:
            result['findings'].append('Assessment exceeds available-memory budget');result['status']=result['status'].replace('passed_','failed_');result['local_candidate_validated']=False
    destination.mkdir(parents=True,exist_ok=False)
    names=['assess_all_source_pilot.py','assess_core_pilot.py','release_rules.py','context_rules.py','hierarchy_rules.py']
    if resources:names+=['assess_scaled_candidate.py','candidate_evidence.py']
    result.update(created_at=now(),verifier=archive_pins([pin(WORKSPACE/'tools'/name) for name in names],destination/'verifier'))
    if integrated:
        seen={p['path'] for p in result['verifier']}
        result['verifier']+=archive_pins([p for p in full_code_pins(True) if p['path'] not in seen],destination/'verifier')
    if not integrated and ('raw_files' in manifest or 'raw_files' in run):
        result['verifier']+=archive_pins([pin(EXPERIMENT/'verifier/check_raw_layout.py')],destination/'verifier')
    write_json(destination/'assessment.json',result)
    lines=['# gen-sdv all-source pilot assessment','',f"Status: **{result['status']}**",'',
           'Full release ready: **false**. All artifacts remain local.','',
           '| Source | Rows | Patients |','|---|---:|---:|']
    for s,c in result['observed_counts'].items():lines.append(f"| {s} | {c['rows']} | {c['distinct_patients']} |")
    lines+=['','Findings:']+(['- '+v for v in result['findings']] or ['- None within the declared pilot scope.'])
    lines+=['','Limitations:']+['- '+v for v in result['limitations']]
    (destination/'summary.md').write_text('\n'.join(lines)+'\n');return result
