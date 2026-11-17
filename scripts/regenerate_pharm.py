"""Regenerate only blank PHARM drug fields from an explicitly pinned recipe.

Usage: python scripts/regenerate_pharm.py RECIPE.json
Creates a local candidate. Never promotes, edits baseline payloads, or uploads.
"""
from datetime import datetime
import hashlib
import importlib.metadata
import json
from pathlib import Path
import shutil
import sys
import zipfile

from jamboree_sdv.pharm_enrichment import build_plan, write_candidate


def sha(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def pin(path):
    return {'path': str(Path(path).resolve()), 'sha256': sha(path)}


def generate(recipe_path):
    recipe_path = Path(recipe_path).resolve()
    recipe = json.loads(recipe_path.read_bytes())
    inputs = recipe['inputs']
    def check_inputs():
        for item in inputs.values():
            if sha(item['path']) != item['sha256']:
                raise ValueError('Input hash mismatch: ' + item['path'])
        for key in ['canmed_manifest', 'rxnorm_manifest']:
            path = Path(inputs[key]['path'])
            for item in json.loads(path.read_bytes())['files']:
                if sha(path.parent / item['path']) != item['sha256']:
                    raise ValueError('Lookup bundle changed: ' + item['path'])
        for f in baseline['files']:
            if sha(f['local_path']) != f['sha256']:
                raise ValueError('Baseline changed: ' + f['source'])
    baseline = json.loads(Path(inputs['release']['path']).read_bytes())
    check_inputs()
    if inputs['rules']['sha256'] != '16b9ab2e2f605eddba267662b6dc74ef60fb2749cafd8b8b616283c0905f042c':
        raise ValueError('Unsupported rule bundle; this scoped adapter supports exact v2.2.0 only')
    run, data, manifest = (Path(recipe[k]).resolve() for k in ['run_out', 'data_out', 'manifest_out'])
    if any(p.exists() for p in [run, data, manifest]):
        raise ValueError('Refuse existing output destination')
    with zipfile.ZipFile(inputs['baseline_pharm']['path']) as z:
        import csv, io
        n = len(list(csv.DictReader(io.StringIO(z.read('PHARM.csv').decode()))))
    loaded = {k: json.loads(Path(inputs[k]['path']).read_bytes()) for k in ['public', 'canmed', 'rxnorm']}
    plan = build_plan(loaded['public'], loaded['canmed'], loaded['rxnorm'], n)
    run.mkdir(parents=True); data.mkdir(parents=True)
    archive = run / 'inputs'; archive.mkdir()
    archived_inputs = {}
    for name, item in inputs.items():
        target = archive / (name + Path(item['path']).suffix)
        shutil.copyfile(item['path'], target)
        archived_inputs[name] = pin(target)
    (archive / 'plan.json').write_text(json.dumps(plan, indent=2) + '\n')
    archived_inputs['plan'] = pin(archive / 'plan.json')
    shutil.copyfile(recipe_path, archive / 'recipe.json')
    archived_inputs['recipe'] = pin(archive / 'recipe.json')
    code = run / 'generator'; code.mkdir()
    package = Path(__file__).resolve().parents[1] / 'src/jamboree_sdv'
    for path in sorted(package.rglob('*.py')):
        target = code / path.relative_to(package)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    shutil.copyfile(__file__, code / Path(__file__).name)
    code_pins = [pin(p) for p in sorted(code.rglob('*.py'))]
    write_candidate(inputs['baseline_pharm']['path'], data / 'PHARM.zip', plan, recipe['seed'])
    files = []
    for f in baseline['files']:
        if f['source'] == 'PHARM':
            path = data / 'PHARM.zip'
            files.append({'source': 'PHARM', 'local_path': str(path), 'sha256': sha(path),
                          'size_bytes': path.stat().st_size, 'rows': n})
        else:
            files.append(f)
    check_inputs()
    record = {'schema_version': 1, 'kind': 'local_sdv_pharm_augmentation_candidate',
              'created_at': datetime.now().astimezone().isoformat(), 'rules_version': '2.2.0',
              'inputs': archived_inputs, 'original_inputs': inputs, 'baseline_files': baseline['files'],
              'files': files, 'generator_code': code_pins, 'seed': recipe['seed'],
              'environment': {'python': sys.version, 'libraries': {p: importlib.metadata.version(p) for p in ['copulas', 'numpy', 'pandas', 'scipy']}},
              'method': 'Preserved SDV/Copulas claims with offline rule-based drug tuple enrichment; no refitting.',
              'promotion': False, 'uploaded': False, 'assessment_status': 'pending'}
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open('x') as stream:
        stream.write(json.dumps(record, indent=2) + '\n')
    return record


if __name__ == '__main__':
    result = generate(sys.argv[1])
    print(json.dumps({'status': 'candidate_generated_assessment_pending', 'rows': next(f['rows'] for f in result['files'] if f['source'] == 'PHARM')}))
