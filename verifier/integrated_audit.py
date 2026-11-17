"""Experiment-local full audit; shared tools are imported read-only."""
import argparse
from pathlib import Path
import resource
import sys
import time

from jamboree_sdv.common import WORKSPACE, local_verifiers, read_json, write_json, digest, now, checked_pin, full_code_pins
local_verifiers()
from audit_release import audit_file
from jamboree_sdv.integrated_inputs import load_rules
from integrated_rules import evaluate_all


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['recipe','manifest','metadata','rules','out']:parser.add_argument('--'+name,required=True,type=Path)
    args=parser.parse_args(argv);start=time.perf_counter()
    recipe=read_json(args.recipe);manifest=read_json(args.manifest);spec,rules=load_rules(args.rules)
    if digest(args.rules)!=recipe['rules']['sha256'] or manifest['rules']!=recipe['rules']:raise ValueError('Rule binding mismatch')
    checked_pin(manifest['context_contract'])
    if len({f['source'] for f in manifest['files']})!=len(manifest['files']):raise ValueError('Duplicate source')
    args.out.mkdir(parents=True,exist_ok=False)
    driver=Path(__file__).resolve()
    code=[p for p in full_code_pins(True) if p['path'].startswith(('tools/','experiments/gen-sdv/verifier/','experiments/gen-sdv/src/jamboree_sdv/catalog_contract','experiments/gen-sdv/src/jamboree_sdv/integrated_inputs','experiments/gen-sdv/src/jamboree_sdv/common','experiments/gen-sdv/src/jamboree_sdv/multiplicity_reference'))]
    report={'report_schema_version':2,'release_id':manifest['release_id'],'created_at':now(),
            'audit':{'version':'2.0.0-integrated','path':str(driver),'sha256':digest(driver),
                     'components':[{'path':str(WORKSPACE/p['path']),'sha256':p['sha256']} for p in code if WORKSPACE/p['path']!=driver]},
            'manifest':{'path':str(args.manifest.resolve()),'sha256':digest(args.manifest)},
            'rules':{'path':str(args.rules.resolve()),'sha256':digest(args.rules),'version':spec['version']},
            'context':manifest['context'],'context_contract':manifest['context_contract'],'sources':[]}
    ids={}
    for item in manifest['files']:
        source,patients=audit_file(item,WORKSPACE,args.metadata if item['source']!='CPI' else None)
        report['sources'].append(source);ids[item['source']]=patients
    report['patient_id_linkage']={s:{'distinct_nonblank_ids':len(v),'ids_absent_from_ctc':len(v-(ids.get('CTC') or set()))} for s,v in ids.items() if v is not None}
    report['rule_results'],report['extension_checks']=evaluate_all(recipe,manifest,report['sources'],rules)
    code=2 if any(s['status']=='error' for s in report['sources']) else int(any(s['status']!='passed_checks' for s in report['sources']) or any(r['status'] not in ['passed','disabled'] for r in report['rule_results']))
    report.update(exit_code=code,status='error' if code==2 else ('findings_or_incomplete' if code else 'passed_checks'),
                  complete=True,metrics={'wall_seconds':time.perf_counter()-start,
                  'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024)})
    write_json(args.out/'audit.json',report)
    print(report['status']);return code


if __name__=='__main__':raise SystemExit(main())
