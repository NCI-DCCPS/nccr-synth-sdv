"""Explicit, exclusive local promotion; historical evidence is never rewritten."""
from datetime import datetime
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
import zipfile
from .common import WORKSPACE, EXPERIMENT, read_json, write_json, now
from .promotion_evidence import LocalEvidence, validate_record, sha, require, canonical, SOURCES


def destination(root, relative):
    path = root / relative
    require(not Path(relative).is_absolute() and path.resolve() == path and path.is_relative_to(root),
            'Destination escapes workspace or uses a symlink')
    return path


def copy_checked(source, target, expected):
    require(target.absolute() == target.resolve(), 'Copy destination uses a symlink')
    require(not target.exists() and not target.is_symlink(), 'Refusing existing destination: ' + str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    with Path(source).open('rb') as src, target.open('xb') as dst:
        shutil.copyfileobj(src, dst)
    require(sha(target) == expected, 'Copy hash mismatch: ' + str(target))


def collect(assessment, original_workspace=WORKSPACE):
    reader = LocalEvidence(original_workspace)
    path = Path(assessment).absolute()
    pin = {'path': str(path), 'sha256': sha(path)}
    record = validate_record(reader, pin, candidate=True)
    return reader, record


def _layout(reader, record, run_relative, data_relative):
    generation = Path(record['run_original']).parent
    audit = Path(record['audit_original']).parent
    assessment = Path(canonical(record['assessment_pin']['path'], reader.root)).parent
    payloads = {canonical(f['local_path'], reader.root): f['source'] for f in record['manifest']['files']}
    raw_payloads = {canonical(f['local_path'], reader.root): f['source'] for f in record['manifest'].get('raw_files', [])}
    aliases = {}
    for (name, digest), physical in reader.aliases.items():
        aliases.setdefault((physical, digest), set()).add(name)
    copies = []
    for original, digest in sorted(reader.files.items()):
        source = Path(original)
        if original in raw_payloads:
            source_name = raw_payloads[original]
            target = data_relative / (source_name + ('.csv' if source_name == 'CPI' else '.zip'))
        elif original in payloads:
            base = run_relative / 'processed' if raw_payloads and payloads[original] in ('CCDI','CPI') else data_relative
            target = base / (payloads[original] + '.zip')
        elif source == Path(record['manifest_original']):
            target = run_relative / 'provenance/candidate-release.json'
        elif source.is_relative_to(generation):
            part = source.relative_to(generation)
            if part == Path('run-manifest.json'):
                part = Path('provenance/candidate-run-manifest.json')
            elif part.parts[0] == 'code':
                part = Path('provenance/generator-code') / Path(*part.parts[1:])
            elif part.name in ['coverage.json', 'resources.json'] and len(part.parts) == 1:
                part = Path('metrics') / part
            target = run_relative / part
        elif source.is_relative_to(audit):
            target = run_relative / 'provenance/candidate-audit' / source.relative_to(audit)
        elif source.is_relative_to(assessment):
            target = run_relative / 'provenance/candidate-assessment' / source.relative_to(assessment)
        elif generation.name == 'generation' and source.is_relative_to(generation.parent):
            target = run_relative / 'provenance/candidate-commands' / source.relative_to(generation.parent)
        else:
            target = run_relative / 'provenance/prerequisites' / source.relative_to(reader.root)
        copies.append({'source': original, 'destination': str(target), 'sha256': digest,
                       'original_paths': sorted(aliases.get((original, digest), {original}))})
    # Execute the exact independent tools archived by this candidate, not live tools.
    for item in record['run']['archives']['code']:
        if item['path'].startswith('tools/') and item['path'].endswith('.py'):
            copies.append({'source': str(reader.path(item['archived_path'], item['sha256'])),
                           'destination': str(run_relative / 'verifier' / item['path']),
                           'sha256': item['sha256'], 'original_paths': []})
    integrated=record.get('recipe',{}).get('schema_version')==4
    if integrated:
        for item in record['run']['archives']['code']:
            logical=Path(item['path']);target=None
            if logical.parent==Path('experiments/gen-sdv/verifier') and logical.suffix=='.py':target=Path(logical.name)
            if logical.parent==Path('experiments/gen-sdv/src/jamboree_sdv') and logical.name in ['__init__.py','common.py','catalog_contract.py','integrated_inputs.py','multiplicity_reference.py']:
                target=Path('jamboree_sdv')/logical.name
            if logical==Path('experiments/gen-sdv/src/jamboree_sdv/promotion_evidence.py'):target=Path(logical.name)
            if target is not None:
                copies.append({'source':str(reader.path(item['archived_path'],item['sha256'])),
                               'destination':str(run_relative/'verifier'/target),'sha256':item['sha256'],'original_paths':[]})
    for source in ([] if integrated else [EXPERIMENT / 'verifier/verify_promoted.py', EXPERIMENT / 'verifier/promoted_science.py',
                   Path(__file__).with_name('promotion_evidence.py')]):
        copies.append({'source': str(source), 'destination': str(run_relative / 'verifier' / source.name),
                       'sha256': sha(source), 'original_paths': [str(source)]})
    for name in ['promotion.py', 'promotion_evidence.py', 'common.py', 'cli.py']:
        source = Path(__file__).with_name(name)
        copies.append({'source': str(source),
                       'destination': str(run_relative / 'provenance/promoter-code' / name),
                       'sha256': sha(source), 'original_paths': [str(source)]})
    require(len({p['destination'] for p in copies}) == len(copies), 'Conflicting copy destinations')
    return copies


def _mapped(copies, original, digest):
    matches = [p for p in copies if original in p['original_paths'] and p['sha256'] == digest]
    require(bool(matches), 'Missing promotion mapping: ' + original)
    return {'path': matches[0]['destination'], 'sha256': digest}


def _absolute(pin, root):
    return {**pin, 'path': str(destination(root, pin['path']))}


def promote(assessment, release_id, *, workspace=WORKSPACE):
    """Copy an assessed scale candidate and verify its shared release copies."""
    workspace = Path(workspace).resolve()
    require(bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}', release_id)), 'Unsafe release ID')
    stamp = datetime.now().astimezone().strftime('%Y-%m-%dT%H-%M-%S.%f%z')
    run_id = release_id + '-' + stamp
    run_relative = Path('runs/gen-sdv') / run_id
    data_relative = Path('data/2026-data-jamboree') / release_id
    release_relative = Path('releases') / (release_id + '.json')
    validation_relative = Path('runs/validation') / release_id
    for relative in (run_relative, data_relative, release_relative):
        path = destination(workspace, relative)
        require(not path.exists() and not path.is_symlink(), 'Refusing existing destination: ' + str(path))
    reader, record = collect(assessment)
    print('Candidate and prerequisite evidence verified', flush=True)
    copies = _layout(reader, record, run_relative, data_relative)
    required = sum(Path(item['source']).stat().st_size for item in copies)
    free = shutil.disk_usage(workspace).free
    require(free > 4 * required, 'Insufficient disk for staging, final copies and safety margin')
    staging = destination(workspace, Path('runs/gen-sdv') / ('.promotion-' + release_id + '-' + uuid.uuid4().hex))
    staging.mkdir(parents=True, exist_ok=False)
    write_json(staging / 'request.json', {'created_at': now(), 'release_id': release_id,
               'candidate_assessment': record['assessment_pin'], 'required_copy_bytes': required,
               'free_disk_bytes': free, 'run': str(run_relative), 'data': str(data_relative)})
    try:
        for item in copies:
            copy_checked(item['source'], staging / 'copies' / item['destination'], item['sha256'])
        for item in record['manifest']['files']:
            mapped = _mapped(copies, canonical(item['local_path'], reader.root), item['sha256'])
            with zipfile.ZipFile(staging / 'copies' / mapped['path']) as archive:
                require(archive.testzip() is None, 'Invalid payload ZIP CRC')
        promote_record = {'schema_version': 1, 'kind': 'gen_sdv_promotion', 'created_at': now(),
                          'release_id': release_id, 'original_workspace': str(reader.root),
                          'candidate_assessment': record['assessment_pin'],
                          'command': [sys.executable, *sys.argv],
                          'options': {'assessment': record['assessment_pin']['path'], 'release_id': release_id},
                          'implementation': {'version': '1.0.0', 'python': sys.version,
                              'code': [{'path': p['destination'], 'sha256': p['sha256']} for p in copies
                                       if Path(p['destination']).is_relative_to(run_relative / 'provenance/promoter-code')]},
                          'copies': [{k: v for k, v in p.items() if k != 'source'} for p in copies],
                          'aws_upload': False, 'deployment': False}
        staged_run = staging / 'copies' / run_relative
        write_json(staged_run / 'promotion.json', promote_record)
        promotion_pin = {'path': str(destination(workspace, run_relative / 'promotion.json')),
                         'sha256': sha(staged_run / 'promotion.json')}
        files = [{**f, 'local_path': str(destination(workspace, _mapped(copies, canonical(f['local_path'], reader.root), f['sha256'])['path']))}
                 for f in record['manifest']['files']]
        def copied(original, digest):
            return _absolute(_mapped(copies, canonical(original, reader.root), digest), workspace)
        def input_pin(pin):
            return copied(pin['path'], pin['sha256'])
        run = {'schema_version': 1, 'kind': 'gen_sdv_promoted_run', 'created_at': now(), 'run_id': run_id,
               'release_id': release_id, 'promotion': promotion_pin, 'candidate_assessment': record['assessment_pin'],
               'recipe': record['recipe'], 'files': files,
               'context': input_pin(record['manifest']['context']),
               'coverage': copied(Path(record['run_original']).parent / 'coverage.json', record['run']['coverage_sha256']),
               'coverage_sha256': record['run']['coverage_sha256'], 'metrics': record['run']['metrics'],
               'resources': record['run']['resources'], 'environment': record['run']['environment'],
               'rules': input_pin(record['recipe']['rules']),
               'context_contract': input_pin(record['manifest']['context_contract']),
               'snapshot': input_pin(record['recipe']['snapshot']),
               'inputs': [input_pin(p) for p in record['run']['archives']['inputs']],
               'verifier': [{'path': str(destination(workspace, p['destination'])), 'sha256': p['sha256']}
                            for p in copies if Path(p['destination']).is_relative_to(run_relative / 'verifier')],
               'origin': {'manifest': copied(record['manifest_original'], record['assessment']['bindings']['manifest_sha256']),
                          'run': copied(record['run_original'], record['assessment']['bindings']['run_sha256'])}}
        if 'raw_files' in record['manifest']:
            run['raw_files'] = [{**f, 'local_path': copied(f['local_path'], f['sha256'])['path']}
                                for f in record['manifest']['raw_files']]
            run['raw_layout_checks'] = record['run']['raw_layout_checks']
        write_json(staged_run / 'manifest.json', run)
        rule_version = record['run']['rules_version']
        validation_relative = validation_relative / ('rules-v' + rule_version) / stamp
        release = {'schema_version': 1, 'kind': 'gen_sdv_promoted_release', 'release_id': release_id,
                   'created_at': now(), 'generator': 'gen-sdv', 'size': record['recipe']['size'],
                   'patients': record['recipe']['patients'], 'rules_version': rule_version,
                   'scope': SOURCES, 'rules': run['rules'], 'context': run['context'],
                   'context_contract': run['context_contract'], 'files': files, 'promotion': promotion_pin,
                   'run_manifest': {'path': str(destination(workspace, run_relative / 'manifest.json')),
                                    'sha256': sha(staged_run / 'manifest.json')},
                   'verification': {'assessment_path': str(destination(workspace, validation_relative / 'assessment/assessment.json')),
                                    'required': True}, 'aws_upload': False, 'deployment': False}
        if 'raw_files' in run:
            release['raw_files'] = run['raw_files']
        write_json(staging / 'release.json', release)
        # Reserve both final directories exclusively. Any failure leaves evidence.
        final_run = destination(workspace, run_relative)
        final_data = destination(workspace, data_relative)
        final_run.mkdir(parents=True, exist_ok=False)
        final_data.mkdir(parents=True, exist_ok=False)
        for base, final in [(staged_run, final_run), (staging / 'copies' / data_relative, final_data)]:
            for path in sorted(base.rglob('*')):
                if path.is_file():
                    copy_checked(path, final / path.relative_to(base), sha(path))
        for item in copies:
            require(sha(destination(workspace, item['destination'])) == item['sha256'], 'Final copy hash mismatch')
        final_manifest = destination(workspace, release_relative)
        final_manifest.parent.mkdir(parents=True, exist_ok=True)
        os.link(staging / 'release.json', final_manifest)  # atomic, no replacement
        write_json(staging / 'published.json', {'status': 'copied_pending_final_verification',
                   'manifest': str(final_manifest), 'sha256': sha(final_manifest)})
        print('SHARED_MANIFEST=' + str(final_manifest), flush=True)
        result = verify_shared(final_manifest, destination(workspace, validation_relative))
        write_json(staging / 'complete.json', result)
        return result
    except Exception as exc:
        write_json(staging / 'failure.json', {'status': 'incomplete_promotion', 'created_at': now(),
                   'error': type(exc).__name__ + ': ' + str(exc),
                   'release_manifest_exists': destination(workspace, release_relative).exists()})
        raise


def verify_shared(manifest, validation):
    manifest = Path(manifest).resolve()
    release = read_json(manifest)
    run = read_json(release['run_manifest']['path'])
    require(sha(release['run_manifest']['path']) == release['run_manifest']['sha256'], 'Shared run hash mismatch')
    verifier = Path(release['run_manifest']['path']).parent / 'verifier/verify_promoted.py'
    require(any(p['path'] == str(verifier) and p['sha256'] == sha(verifier) for p in run['verifier']), 'Unbound final verifier')
    validation = Path(validation).absolute()
    validation.mkdir(parents=True, exist_ok=False)
    stages = []
    for stage in ['audit', 'assess']:
        print('Starting shared ' + stage, flush=True)
        command = [sys.executable, '-I', '-S', '-B', str(verifier), stage, '--manifest', str(manifest), '--out', str(validation)]
        with (validation / (stage + '.log')).open('x') as stream:
            process = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, cwd=manifest.parent.parent)
        stages.append({'command': command, 'exit_code': process.returncode})
        require(process.returncode in ([0, 1] if stage == 'audit' else [0]),
                'Shared ' + stage + ' failed; see ' + str(validation / (stage + '.log')))
    assessment_path = validation / 'assessment/assessment.json'
    assessment = read_json(assessment_path)
    require(assessment['status'] == 'passed_shared_release', 'Shared release did not pass')
    return {'status': 'passed_shared_release', 'manifest': str(manifest), 'manifest_sha256': sha(manifest),
            'assessment': str(assessment_path), 'assessment_sha256': sha(assessment_path), 'stages': stages,
            'aws_upload': False, 'deployment': False}
