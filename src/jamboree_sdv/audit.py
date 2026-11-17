"""Invoke the common read-only auditor with a generator-local output guard."""
import os
from pathlib import Path
import subprocess
import sys
from .common import WORKSPACE,WORK,digest,now,output_path,pin,read_json,write_json
from .recipe import load_recipe
from .provenance import archive_pins


def audit_candidate(recipe_path,manifest_path,out):
    destination=output_path(out)
    recipe=load_recipe(recipe_path)
    manifest_path=Path(manifest_path).resolve()
    if not manifest_path.is_relative_to(WORK.resolve()):raise ValueError('Only local candidate manifests may be audited by this command')
    manifest=read_json(manifest_path)
    if manifest.get('rules')!=recipe.spec['rules']:raise ValueError('Candidate/recipe rule mismatch')
    command=[sys.executable,'-B',str(WORKSPACE/'tools/audit_release.py'),'--manifest',str(manifest_path),
             '--metadata',str(recipe.snapshot/'metadata'),'--rules',str(WORKSPACE/recipe.spec['rules']['path']),
             '--out',str(destination)]
    env=os.environ.copy();env.update(PYTHONDONTWRITEBYTECODE='1',TMPDIR=str(WORK/'tmp'))
    process=subprocess.run(command,cwd=WORKSPACE,env=env,text=True,capture_output=True)
    if not destination.exists():raise ValueError('Audit setup failed: '+process.stderr)
    (destination/'command.log').write_text(process.stdout+process.stderr)
    if process.returncode not in [0,1]:raise ValueError('Audit processing failed: '+process.stderr)
    report=read_json(destination/'audit.json')
    common=[x for x in recipe.inputs if x['path'].startswith('reference/')]
    common.extend([pin(manifest_path),pin(Path(manifest['run_manifest']['path']))])
    inputs=archive_pins(common,destination/'inputs')
    verifier=archive_pins([pin(WORKSPACE/'tools'/name) for name in ['audit_release.py','release_rules.py','hierarchy_rules.py']],destination/'verifier')
    write_json(destination/'provenance.json',{'schema_version':1,'created_at':now(),'command':command,
               'exit_code':process.returncode,'inputs':inputs,'verifier':verifier,'audit_sha256':digest(destination/'audit.json'),
               'incomplete_checks':[r['id'] for r in report['rule_results'] if r['status'] not in ['passed','disabled','not_applicable']]})
    return report

