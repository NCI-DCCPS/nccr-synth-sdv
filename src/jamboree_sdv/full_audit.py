"""Local output guard and exact provenance around the independent full auditor."""
import os
from pathlib import Path
import subprocess
import sys
from .common import EXPERIMENT,WORKSPACE,WORK,digest,now,output_path,pin,read_json,write_json
from .promotion_evidence import verify_raw_files
from .full_recipe import load_full_recipe
from .provenance import archive_pins


def audit_candidate(recipe_path,manifest_path,out):
    destination=output_path(out);recipe=load_full_recipe(recipe_path);manifest_path=Path(manifest_path).resolve()
    if not manifest_path.is_relative_to(WORK.resolve()):raise ValueError('Only local manifests are supported')
    manifest=read_json(manifest_path)
    raw_checks=verify_raw_files(manifest['raw_files'],manifest['files']) if 'raw_files' in manifest else None
    if manifest['rules']!=recipe.spec['rules'] or manifest['context_contract']!=recipe.contract['common_inputs']['context-schema.json']:raise ValueError('Candidate/recipe mismatch')
    command=[sys.executable,'-B',str(WORKSPACE/'tools/audit_all_source_pilot.py'),'--manifest',str(manifest_path),
             '--metadata',str(recipe.snapshot/'metadata'),'--rules',str(WORKSPACE/recipe.spec['rules']['path']),'--out',str(destination)]
    integrated=recipe.spec['schema_version']==4
    if integrated:
        command[2]=str(EXPERIMENT/'verifier/integrated_audit.py')
        command+=['--recipe',str(recipe.path)]
    env=os.environ.copy();env.update(PYTHONDONTWRITEBYTECODE='1',TMPDIR=str(WORK/'tmp'))
    resources=None
    if recipe.spec['schema_version'] in [3,4]:
        from .resources import snapshot
        resources=snapshot()
    process=subprocess.run(command,cwd=WORKSPACE,env=env,text=True,capture_output=True)
    if not destination.is_dir():raise ValueError('Audit setup failed: '+process.stderr)
    (destination/'command.log').write_text(process.stdout+process.stderr)
    if not (destination/'audit.json').is_file():raise ValueError('Audit failed: '+process.stderr)
    report=read_json(destination/'audit.json')
    if integrated and (not report.get('complete') or process.returncode!=report['exit_code'] or process.returncode not in [0,1]):
        raise ValueError('Integrated audit did not complete successfully')
    inputs=archive_pins([x for x in recipe.inputs if x['path'].startswith('reference/')]+[pin(manifest_path),pin(Path(manifest['run_manifest']['path']))],destination/'inputs')
    verifier=archive_pins([pin(WORKSPACE/'tools'/n) for n in ['audit_all_source_pilot.py','audit_release.py','release_rules.py','hierarchy_rules.py','context_rules.py']],destination/'verifier')
    if integrated:
        # All actual components are pinned, including shared read-only dependencies.
        seen={item['path'] for item in verifier}
        selected=[pin(Path(c['path'])) for c in [report['audit'],*report['audit']['components']]]
        verifier+=archive_pins([p for p in selected if p['path'] not in seen],destination/'verifier')
    if raw_checks and not integrated:
        verifier+=archive_pins([pin(EXPERIMENT/'verifier/check_raw_layout.py')],destination/'verifier')
    write_json(destination/'provenance.json',{'schema_version':1,'created_at':now(),'command':command,'exit_code':process.returncode,
                'resources':resources,'raw_layout_checks':raw_checks,
                'inputs':inputs,'verifier':verifier,'audit_sha256':digest(destination/'audit.json'),
                'incomplete_checks':[r['id'] for r in report['rule_results'] if r['status'] not in ['passed','disabled','not_applicable']]})
    return report
