"""Standalone shared-copy audit and assessment. Run the archived release copy."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import shutil
import subprocess
import sys
import time


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write('\n')


def install_boundary(workspace, original_workspace, allowed):
    """Prove final checks do not consume original work, live references or tools."""
    roots = [Path(workspace).resolve(), Path(original_workspace).resolve()]
    permitted = [Path(p).resolve() for p in allowed]
    def guard(event, args):
        if event not in ['open', 'os.listdir', 'os.scandir'] or not args or isinstance(args[0], int):
            return
        if not isinstance(args[0], (str, bytes, os.PathLike)):
            return
        path = Path(os.fsdecode(args[0])).resolve()
        if any(path == p or path.is_relative_to(p) for p in permitted):
            return
        if any(path == root or path.is_relative_to(root) for root in roots):
            raise PermissionError('Final verification blocked access outside shared copies: ' + str(path))
    sys.addaudithook(guard)


def memory_snapshot():
    if sys.platform == 'darwin':
        raw = subprocess.check_output(['/usr/bin/vm_stat'], text=True)
        page = int(re.search(r'page size of (\d+) bytes', raw)[1])
        available = page * sum(int(re.search(r'^' + re.escape(k) + r':\s+(\d+)', raw, re.M)[1])
                               for k in ['Pages free', 'Pages inactive'])
        method = 'vm_stat free + inactive pages'
    else:
        raw = Path('/proc/meminfo').read_text()
        available = int(re.search(r'^MemAvailable:\s+(\d+)', raw, re.M)[1]) * 1024
        method = 'MemAvailable'
    return {'available_memory_bytes': available, 'available_memory_method': method}


def verify_operational_inputs(run, original, reader):
    from promotion_evidence import require, sha
    snapshot = original['recipe']['snapshot']
    require(run['snapshot']['sha256'] == snapshot['sha256'], 'Snapshot mismatch')
    require(Path(run['snapshot']['path']) == reader.path(snapshot['path'], snapshot['sha256']) and
            sha(run['snapshot']['path']) == snapshot['sha256'], 'Operational snapshot pin mismatch')
    expected_inputs = [{'path': str(reader.path(item['path'], item['sha256'])), 'sha256': item['sha256']}
                       for item in original['run']['archives']['inputs']]
    require(run['inputs'] == expected_inputs, 'Operational input inventory mismatch')
    for item in run['inputs']:
        require(sha(item['path']) == item['sha256'], 'Operational input pin mismatch')


def load_release(manifest_path, validation):
    # Bootstrap only the requested shared graph before installing the boundary.
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    from promotion_evidence import read, require, sha, SharedEvidence, validate_record, SOURCES, verify_raw_files
    manifest_path = Path(manifest_path).resolve()
    workspace = manifest_path.parent.parent
    require(manifest_path.parent == workspace / 'releases', 'Manifest must be under releases/')
    manifest = read(manifest_path)
    require(manifest['kind'] == 'gen_sdv_promoted_release', 'Wrong release kind')
    require(bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}', manifest['release_id'])) and
            manifest_path.name == manifest['release_id'] + '.json', 'Unsafe release identity')
    run_path = Path(manifest['run_manifest']['path'])
    require(run_path.resolve() == run_path and run_path.is_relative_to(workspace / 'runs/gen-sdv'), 'Unsafe shared run')
    require(sha(run_path) == manifest['run_manifest']['sha256'], 'Run manifest hash mismatch')
    run = read(run_path)
    require(run['kind'] == 'gen_sdv_promoted_run' and run['release_id'] == manifest['release_id'], 'Run/release mismatch')
    require(run['promotion'] == manifest['promotion'], 'Promotion pin mismatch')
    promotion_path = Path(run['promotion']['path'])
    require(promotion_path == run_path.parent / 'promotion.json', 'Unexpected promotion path')
    require(sha(promotion_path) == run['promotion']['sha256'], 'Promotion hash mismatch')
    promotion = read(promotion_path)
    require(promotion['kind'] == 'gen_sdv_promotion' and promotion['release_id'] == manifest['release_id'], 'Promotion identity mismatch')
    data = workspace / 'data/2026-data-jamboree' / manifest['release_id']
    validation = Path(validation).resolve()
    require(validation.is_relative_to(workspace / 'runs/validation' / manifest['release_id']), 'Unsafe validation output')
    require([f['source'] for f in manifest['files']] == SOURCES and manifest['files'] == run['files'], 'Final file inventory mismatch')
    for item in manifest['files']:
        path = Path(item['local_path'])
        base = run_path.parent / 'processed' if 'raw_files' in manifest and item['source'] in ('CCDI','CPI') else data
        require(path == base / (item['source'] + '.zip') and path.resolve() == path, 'Unsafe final payload path')
    if 'raw_files' in run:
        require(manifest.get('raw_files') == run['raw_files'], 'Final raw inventory mismatch')
        for item in manifest['raw_files']:
            path = Path(item['local_path'])
            require(path == data / (item['source'] + ('.csv' if item['source']=='CPI' else '.zip')) and
                    path.resolve() == path, 'Unsafe final raw path')
    require(here == run_path.parent / 'verifier', 'Verifier must execute from the archived shared run')
    install_boundary(workspace, promotion['original_workspace'], [run_path.parent, data, manifest_path, validation])
    for item in run['verifier']:
        path = Path(item['path'])
        require(path.is_relative_to(here) and sha(path) == item['sha256'], 'Final verifier pin mismatch')
    reader = SharedEvidence(workspace, promotion)
    require(promotion['candidate_assessment'] == run['candidate_assessment'], 'Candidate assessment mismatch')
    original = validate_record(reader, run['candidate_assessment'], candidate=True)
    require(('raw_files' in original['manifest']) == ('raw_files' in manifest), 'Raw export inventory removed or added')
    if 'raw_files' in manifest:
        for old, new in zip(original['manifest']['raw_files'], manifest['raw_files']):
            require({k:v for k,v in old.items() if k!='local_path'} ==
                    {k:v for k,v in new.items() if k!='local_path'}, 'Raw export changed during promotion')
        require(verify_raw_files(manifest['raw_files'], manifest['files']) == run['raw_layout_checks'],
                'Shared raw layout check mismatch')
    for key, source, expected_hash in [
            ('manifest', original['manifest_original'], original['assessment']['bindings']['manifest_sha256']),
            ('run', original['run_original'], original['assessment']['bindings']['run_sha256'])]:
        require(run['origin'][key] == {'path': str(reader.path(source, expected_hash)), 'sha256': expected_hash},
                'Original record binding mismatch: ' + key)
    require(original['recipe'] == run['recipe'] and manifest['size'] == run['recipe']['size'], 'Selected recipe/size changed')
    require(manifest['patients'] == run['recipe']['patients'] and manifest['rules_version'] == original['run']['rules_version'], 'Release count/rules mismatch')
    require(run['metrics'] == original['run']['metrics'] and run['resources'] == original['run']['resources'], 'Generation resource evidence changed')
    require(run['environment'] == original['run']['environment'], 'Environment evidence changed')
    for old, new in zip(original['manifest']['files'], manifest['files']):
        require({k: v for k, v in old.items() if k != 'local_path'} ==
                {k: v for k, v in new.items() if k != 'local_path'}, 'Promotion changed payload metadata')
        require(sha(new['local_path']) == old['sha256'], 'Promotion changed payload bytes')
    for key in ['context', 'context_contract', 'rules']:
        require(manifest[key] == run[key] and manifest[key]['sha256'] == original['manifest'][key]['sha256'], 'Operational input mismatch: ' + key)
        require(sha(manifest[key]['path']) == manifest[key]['sha256'], 'Shared input hash mismatch: ' + key)
    require(sha(run['coverage']['path']) == original['run']['coverage_sha256'] == run['coverage_sha256'], 'Coverage mismatch')
    verify_operational_inputs(run, original, reader)
    snapshot = read(run['snapshot']['path'])
    for item in snapshot['files']:
        path = Path(run['snapshot']['path']).parent / item['path']
        require(sha(path) == item['sha256'], 'Snapshot artifact mismatch')
    return manifest, run, promotion, reader, original, workspace


def archive_inputs(run, manifest_path, audit_dir):
    from promotion_evidence import read
    originals = [Path(manifest_path), Path(read(manifest_path)['run_manifest']['path']),
                 Path(run['promotion']['path']), Path(run['context']['path']), Path(run['context_contract']['path'])]
    originals += [Path(p['path']) for p in run['inputs']]
    retained = []
    for index, path in enumerate(dict.fromkeys(originals)):
        target = audit_dir / 'inputs' / f'{index:04d}' / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        with path.open('rb') as src, target.open('xb') as dst:
            shutil.copyfileobj(src, dst)
        if digest(target) != digest(path):
            raise ValueError('Audit input copy mismatch')
        retained.append({'path': str(path), 'sha256': digest(path), 'archived_path': str(target)})
    return retained


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=['audit', 'assess'])
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args(argv)
    manifest, run, promotion, reader, original, workspace = load_release(args.manifest, args.out)
    from promotion_evidence import read, require, sha
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here / 'tools'))
    resources = memory_snapshot()
    audit_dir = args.out / 'audit'
    if args.stage == 'audit':
        command = ['--manifest', str(args.manifest), '--metadata', str(Path(run['snapshot']['path']).parent / 'metadata'),
                   '--rules', run['rules']['path'], '--out', str(audit_dir)]
        if run['recipe']['schema_version']==4:
            from integrated_shared import audit_shared
            code=audit_shared(manifest,run,args.manifest,Path(run['snapshot']['path']).parent/'metadata',audit_dir,reader)
        else:
            import audit_all_source_pilot
            code = audit_all_source_pilot.main(command)
        inputs = archive_inputs(run, args.manifest, audit_dir)
        verifiers = []
        for item in run['verifier']:
            path = Path(item['path'])
            target = audit_dir / 'verifier' / path.relative_to(here)
            target.parent.mkdir(parents=True, exist_ok=True)
            with path.open('rb') as src, target.open('xb') as dst:
                shutil.copyfileobj(src, dst)
            require(sha(target) == item['sha256'], 'Verifier archive copy mismatch')
            verifiers.append({**item, 'archived_path': str(target)})
        report = read(audit_dir / 'audit.json')
        write(audit_dir / 'provenance.json', {'schema_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
              'command': [sys.executable, '-I', '-S', '-B', str(Path(__file__)), *sys.argv[1:]],
              'audit_command': command, 'exit_code': code, 'inputs': inputs, 'verifier': verifiers,
              'audit_sha256': sha(audit_dir / 'audit.json'), 'resources': resources,
              'original_work_access': 'blocked', 'live_reference_and_tools_access': 'blocked',
              'incomplete_checks': [r['id'] for r in report['rule_results'] if r['status'] not in ['passed', 'disabled']]})
        return code
    audit = read(audit_dir / 'audit.json')
    provenance = read(audit_dir / 'provenance.json')
    require(provenance['audit_sha256'] == sha(audit_dir / 'audit.json'), 'Fresh audit provenance mismatch')
    require(audit['manifest']['sha256'] == sha(args.manifest) and audit['manifest']['path'] == str(args.manifest), 'Fresh audit manifest mismatch')
    require(audit['rules']['sha256'] == manifest['rules']['sha256'] and audit['context'] == manifest['context'], 'Fresh audit rule/context mismatch')
    require(provenance['incomplete_checks'] == [], 'Incomplete final audit rules')
    verifier_pins = {p['path']: p['sha256'] for p in run['verifier']}
    for component in [audit['audit'], *audit['audit']['components']]:
        require(verifier_pins.get(component['path']) == component['sha256'], 'Fresh audit code binding mismatch')
    for group in ['inputs', 'verifier']:
        require(bool(provenance[group]), 'Missing fresh audit archives')
        for item in provenance[group]:
            require(sha(item['archived_path']) == item['sha256'] == sha(item['path']), 'Fresh audit archive mismatch')
    if run['recipe']['schema_version']==4:
        from integrated_promoted_science import assess_outputs
    else:from promoted_science import assess_outputs
    start = time.perf_counter()
    result = assess_outputs(manifest, run, audit, provenance, reader)
    if 'raw_files' in manifest:
        from promotion_evidence import verify_raw_files
        result['raw_layout_checks'] = verify_raw_files(manifest['raw_files'], manifest['files'])
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == 'darwin' else 1024)
    if peak >= resources['available_memory_bytes'] / 2:
        result['findings'].append('Shared assessment exceeds available-memory budget')
    result.update(schema_version=1, kind='gen_sdv_shared_release_assessment',
                  status='failed_shared_release' if result['findings'] else 'passed_shared_release',
                  created_at=datetime.now(timezone.utc).isoformat(), release_id=manifest['release_id'],
                  shared_release_verified=not result['findings'], promotion=True, aws_upload=False, deployment=False,
                  original_work_access='blocked', live_reference_and_tools_access='blocked',
                  metrics={'wall_seconds': time.perf_counter() - start, 'peak_rss_bytes': peak},
                  resources=resources, candidate_assessment=run['candidate_assessment'],
                  bindings={name: {'path': str(path), 'sha256': sha(path)} for name, path in
                            [('manifest', args.manifest), ('run', manifest['run_manifest']['path']),
                             ('promotion', run['promotion']['path']), ('audit', audit_dir / 'audit.json')]},
                  verifier=run['verifier'],
                  limitations=[s for s in original['assessment']['limitations'] if not s.startswith('Local ')] +
                              ['Verified shared filesystem release only; AWS upload and deployment remain separate.'])
    destination = args.out / 'assessment'
    destination.mkdir(exist_ok=False)
    write(destination / 'assessment.json', result)
    lines = ['# gen-sdv shared release assessment', '', 'Status: **' + result['status'] + '**', '',
             'Rules: ' + manifest['rules_version'] + '; scale: ' + manifest['size']['value'], '',
             'Original work and live reference/tools reads were blocked.', '',
             '| Source | Rows | Patients |', '|---|---:|---:|']
    lines += [f"| {s} | {c['rows']} | {c['distinct_patients']} |" for s, c in result['observed_counts'].items()]
    lines += ['', 'Findings:'] + (['- ' + f for f in result['findings']] or ['- None beyond the declared COG annotation exception.'])
    lines += ['', 'AWS upload and deployment have not occurred.']
    (destination / 'summary.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps({'status': result['status'], 'findings': result['findings']}))
    return 1 if result['findings'] else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(type(exc).__name__ + ': ' + str(exc), file=sys.stderr)
        raise SystemExit(2)
