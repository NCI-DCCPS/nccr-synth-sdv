"""Local generation, independent assessment and explicit filesystem promotion."""
import argparse
import json
from pathlib import Path
import sys


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    subs=parser.add_subparsers(dest='command',required=True)
    run=subs.add_parser('run',help='Run generation and independent checks with an explicit size')
    from .common import EXPERIMENT, WORKSPACE
    run.add_argument('--recipe',type=Path,default=EXPERIMENT/'recipes/all-sources-integrated-v3.json')
    size=run.add_mutually_exclusive_group()
    size.add_argument('--patients',type=int,help='CTC patients, at least 100; prerequisite and resource gates apply')
    size.add_argument('--scale',help='Positive snapshot fraction, e.g. 0.1 for 10%%; prerequisite and resource gates apply')
    run.add_argument('--prerequisite',action='append',type=Path,default=[],help='Passing assessment JSON; otherwise discover compatible local evidence')
    replacement=subs.add_parser('replace-ctc',help='Generate and independently audit a CTC-only conditional-joint candidate')
    replacement.add_argument('--recipe',required=True,type=Path)
    replacement.add_argument('--workspace',type=Path,default=WORKSPACE)
    replacement.add_argument('--out',required=True,type=Path,help='Fresh directory below gen-sdv/work')
    promote=subs.add_parser('promote',help='Copy a validated scale candidate and verify the shared release')
    promote.add_argument('--assessment',required=True,type=Path)
    promote.add_argument('--release-id',required=True)
    verify=subs.add_parser('verify-promoted',help='Run fresh checks using an archived shared verifier')
    verify.add_argument('--manifest',required=True,type=Path)
    verify.add_argument('--out',required=True,type=Path)
    delivery=subs.add_parser('prepare-etl',help='Prepare lossless CCDI/CPI delivery copies locally')
    delivery.add_argument('--manifest',required=True,type=Path)
    delivery.add_argument('--out',required=True,type=Path)
    layout=delivery.add_mutually_exclusive_group(required=True)
    layout.add_argument('--package-id',help='One release folder containing raw source ZIPs and CPI.csv')
    layout.add_argument('--delivery-date',help='Legacy source/date folders, YYYY-MM-DD')
    for name in ['preflight','generate','audit','assess']:
        child=subs.add_parser(name)
        child.add_argument('--recipe',required=True,type=Path)
        if name=='generate':
            for option in ['preflight','run-out','data-out','manifest-out']:child.add_argument('--'+option,required=True,type=Path)
        else:child.add_argument('--out',required=True,type=Path)
        if name in ['audit','assess']:child.add_argument('--manifest',required=True,type=Path)
        if name=='assess':
            child.add_argument('--run',required=True,type=Path);child.add_argument('--audit',required=True,type=Path)
    args=parser.parse_args(argv)
    try:
        if args.command=='replace-ctc':
            from .ctc_replacement import run as replace_ctc
            from .common import local_verifiers
            replace_ctc(args.recipe,args.workspace,args.out)
            local_verifiers()
            from check_rule4_replacement import audit as audit_replacement
            result=audit_replacement(args.recipe,args.out/'candidate.json',args.workspace,args.out/'audit')
            print(json.dumps({'status':result['status'],'exit_code':result['exit_code'],
                              'candidate':str(args.out/'candidate.json'),'audit':str(args.out/'audit/audit.json')}))
            return result['exit_code']
        if args.command=='prepare-etl':
            from .delivery import prepare_delivery
            result=prepare_delivery(args.manifest,args.out,args.delivery_date,package_id=args.package_id)
            print(json.dumps({'status':result['status'],'checks':result['checks'],
                              'manifest':str(args.out/'delivery.json')}));return 0
        if args.command=='promote':
            from .promotion import promote
            print(json.dumps(promote(args.assessment,args.release_id)));return 0
        if args.command=='verify-promoted':
            from .promotion import verify_shared
            print(json.dumps(verify_shared(args.manifest,args.out)));return 0
        if args.command=='run':
            from .pipeline import run
            result=run(args.recipe,args.patients,args.scale,args.prerequisite)
            print(json.dumps({'status':result['status'],'size':result['size'],'manifest':result['manifest']}));return 0
        from .common import read_json
        full=read_json(args.recipe).get('schema_version') in [2,3,4]
        if args.command=='preflight':
            if full:from .full_preflight import preflight
            else:from .preflight import preflight
            result=preflight(args.recipe,args.out)
            print(json.dumps({'status':'ready_for_all_source_pilot' if full else 'ready_for_core_pilot','sources':result['adapted']['scope']['scope']}));return 0
        if args.command=='generate':
            if full:from .full_writer import generate
            else:from .writer import generate
            result=generate(args.recipe,args.preflight,args.run_out,args.data_out,args.manifest_out)
            print(json.dumps({'status':'generated_local_candidate','sources':[{k:f[k] for k in ['source','rows','sha256']} for f in result.get('raw_files',result['files'])]}));return 0
        if args.command=='audit':
            if full:from .full_audit import audit_candidate
            else:from .audit import audit_candidate
            result=audit_candidate(args.recipe,args.manifest,args.out)
            print(json.dumps({'status':result['status'],'exit_code':result['exit_code']}));return result['exit_code']
        if full:from .full_assessment import assess_candidate
        else:from .assessment import assess_candidate
        result=assess_candidate(args.recipe,args.run,args.manifest,args.audit,args.out)
        print(json.dumps({'status':result['status'],'findings':result['findings'],'full_release_ready':False}))
        return 0 if result['status'] in ['passed_core_pilot','passed_all_source_pilot','passed_resource_benchmark','passed_local_candidate'] else 1
    except (OSError,ValueError,KeyError,TypeError) as exc:
        print(f'gen-sdv {args.command} error: {exc}',file=sys.stderr);return 2


if __name__=='__main__':
    raise SystemExit(main())
