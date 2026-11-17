"""Read immutable candidate evidence through local or promoted path mappings.

Standard-library only. This module is archived with each promoted release and
is also used by its independent verifier; it never imports the generator.
"""
import hashlib
import json
import os
from pathlib import Path

SOURCES = ['CTC', 'ABM', 'COG', 'MCE', 'MCD', 'MCP', 'PHARM', 'RO', 'CCDI', 'CPI']
PASSED = {'passed_all_source_pilot', 'passed_resource_benchmark', 'passed_local_candidate'}


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON key: ' + key)
            result[key] = value
        return result
    def invalid(value):
        raise ValueError('Nonfinite JSON: ' + value)
    return json.loads(Path(path).read_bytes(), object_pairs_hook=pairs, parse_constant=invalid)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(path, root):
    """Normalize historical names lexically, without accessing their old location."""
    path = Path(path)
    return os.path.normpath(str(path if path.is_absolute() else Path(root) / path))


class LocalEvidence:
    def __init__(self, workspace):
        self.root = Path(workspace).resolve()
        self.boundary = self.root / 'experiments/gen-sdv/work'
        self.files = {}
        self.aliases = {}

    def path(self, original, expected=None):
        name = canonical(original, self.root)
        physical = self.aliases.get((name, expected), name)
        path = Path(physical)
        require(path.resolve() == path and path.is_relative_to(self.boundary),
                'Evidence outside gen-sdv work or redirected by symlink: ' + str(path))
        require(path.is_file(), 'Missing evidence: ' + str(path))
        actual = sha(path)
        require(expected is None or actual == expected, 'Evidence hash mismatch: ' + str(path))
        require(str(path) not in self.files or self.files[str(path)] == actual,
                'Evidence changed during collection: ' + str(path))
        self.files[str(path)] = actual
        self.aliases[(name, actual)] = str(path)
        self.aliases[(str(path), actual)] = str(path)
        return path

    def register(self, item):
        path = self.path(item['archived_path'], item['sha256'])
        self.aliases.setdefault((canonical(item['path'], self.root), item['sha256']), str(path))

    def json(self, original, expected=None):
        return read(self.path(original, expected))

    def tree(self, directory):
        directory = Path(directory)
        require(directory.resolve() == directory and directory.is_relative_to(self.boundary), 'Unsafe evidence directory')
        for path in sorted(directory.rglob('*')):
            require(not path.is_symlink(), 'Evidence tree contains symlink')
            if path.is_file():
                self.path(path)


class SharedEvidence:
    def __init__(self, workspace, promotion):
        self.workspace = Path(workspace).resolve()
        self.root = Path(promotion['original_workspace'])
        self.aliases = {}
        self.inventory = {}
        for item in promotion['copies']:
            path = self.shared_path(item['destination'])
            require(str(path) not in self.inventory, 'Duplicate copy destination')
            require(sha(path) == item['sha256'], 'Copied evidence hash mismatch: ' + str(path))
            self.inventory[str(path)] = item['sha256']
            for original in item['original_paths']:
                key = (canonical(original, self.root), item['sha256'])
                # Duplicate archives of identical historical bytes are allowed.
                self.aliases.setdefault(key, path)

    def shared_path(self, value):
        path = self.workspace / value
        require(path.resolve() == path and path.is_relative_to(self.workspace), 'Shared path escape or symlink')
        return path

    def path(self, original, expected=None):
        name = canonical(original, self.root)
        if expected is not None:
            path = self.aliases.get((name, expected))
        else:
            choices = {p for (n, _), p in self.aliases.items() if n == name}
            require(len(choices) == 1, 'Missing or ambiguous historical mapping: ' + name)
            path = next(iter(choices))
        require(path is not None, 'Historical pin not copied: ' + name)
        require(expected is None or sha(path) == expected, 'Mapped evidence hash mismatch')
        return path

    def register(self, item):
        require(sha(self.path(item['archived_path'], item['sha256'])) == item['sha256'], 'Missing archive')
        require(sha(self.path(item['path'], item['sha256'])) == item['sha256'], 'Missing logical input mapping')

    def json(self, original, expected=None):
        return read(self.path(original, expected))


def validate_record(reader, assessment_pin, *, candidate=False, expected=None, active=None, memo=None):
    """Verify bindings and the complete prerequisite graph from retained bytes."""
    active = set() if active is None else active
    memo = {} if memo is None else memo
    identity = (canonical(assessment_pin['path'], reader.root), assessment_pin['sha256'])
    require(identity not in active, 'Cyclic prerequisite evidence')
    if identity in memo:
        record = memo[identity]
        _compatible(reader, record['recipe'], expected)
        return record
    active.add(identity)
    assessment = reader.json(*identity)
    require(assessment.get('status') in PASSED and assessment.get('findings') == [], 'Assessment did not pass')
    assessment_original = Path(identity[0])
    audit_original = assessment_original.parent.parent / 'audit/audit.json'
    bindings = assessment['bindings']
    audit = reader.json(audit_original, bindings['audit_sha256'])
    manifest_original = audit['manifest']['path']
    manifest = reader.json(manifest_original, bindings['manifest_sha256'])
    run_original = manifest['run_manifest']['path']
    run = reader.json(run_original, bindings['run_sha256'])
    require(audit['manifest']['sha256'] == bindings['manifest_sha256'] and
            manifest['run_manifest']['sha256'] == bindings['run_sha256'], 'Broken manifest/run audit chain')
    recipe = run['recipe']
    require(run.get('promotion') is False and manifest['files'] == run['files'], 'Candidate inventory/promotion mismatch')
    require([f['source'] for f in manifest['files']] == SOURCES, 'Incomplete or reordered source inventory')
    if 'raw_files' in run:
        require(manifest.get('raw_files') == run['raw_files'], 'Raw candidate inventory mismatch')
        raw_checks = verify_raw_files(manifest['raw_files'], manifest['files'], reader.path)
        require(raw_checks == run['raw_layout_checks'] == assessment.get('raw_layout_checks'),
                'Missing or inconsistent raw layout assessment')
    require(manifest['scope'] == SOURCES and recipe['sources'] == SOURCES[:-1], 'Incomplete source scope')
    require(manifest['rules'] == recipe['rules'] and manifest['context'] == run['context'], 'Candidate rule/context mismatch')
    for group in ('inputs', 'code'):
        require(bool(run['archives'][group]), 'Missing generation archive group')
        for item in run['archives'][group]:
            reader.register(item)
    recipe_entries = [p for p in run['archives']['inputs'] if p['sha256'] == bindings['recipe_sha256']]
    require(recipe_entries and any(reader.json(p['archived_path'], p['sha256']) == recipe for p in recipe_entries),
            'Missing matching archived recipe')
    recipe_pin = {k: recipe_entries[0][k] for k in ('path', 'sha256')}
    contract = reader.json(recipe['contract']['path'], recipe['contract']['sha256'])
    require(manifest['context_contract'] == contract['common_inputs']['context-schema.json'], 'Context contract mismatch')
    for pin in [recipe['snapshot'], recipe['rules'], recipe['contract'], *recipe['profiles'].values(), *contract['common_inputs'].values()]:
        reader.path(pin['path'], pin['sha256'])
    frozen = reader.json(Path(run_original).parent / 'inputs/adapted/preflight.json', run['archives']['adapted_sha256'])
    require({(p['path'], p['sha256']) for p in frozen['inputs']} ==
            {(p['path'], p['sha256']) for p in run['archives']['inputs']}, 'Incomplete adapted-input archive')
    require({(p['path'], p['sha256']) for p in frozen['code']} ==
            {(p['path'], p['sha256']) for p in run['archives']['code']}, 'Incomplete code archive')
    reader.path(Path(run_original).parent / 'coverage.json', run['coverage_sha256'])
    context = reader.json(run['context']['path'], run['context']['sha256'])
    for kind in ('patient', 'tumor'):
        reader.path(Path(run['context']['path']).parent / context[kind]['path'], context[kind]['sha256'])
    require(context['patient']['rows'] == recipe['patients'], 'Context patient count mismatch')
    payload_bytes = 0
    for item in manifest['files']:
        path = reader.path(item['local_path'], item['sha256'])
        require(path.stat().st_size == item['size_bytes'], 'Payload size mismatch')
        payload_bytes += item['size_bytes']
        require(assessment['observed_counts'][item['source']] == {'rows': item['rows'], 'distinct_patients': item['patients']},
                'Assessment/payload count mismatch')
    require(run['metrics']['disk_payload_bytes'] == payload_bytes, 'Payload resource metric mismatch')
    rules = reader.json(recipe['rules']['path'], recipe['rules']['sha256'])
    require(run['rules_version'] == rules['version'] and audit['rules']['sha256'] == recipe['rules']['sha256'], 'Rule version mismatch')
    enabled = rules['rules'] + rules.get('biological_constraints', [])
    expected_rules = {r['id']: 'passed' if r['enabled'] else 'disabled' for r in enabled}
    for outcomes in (assessment['rule_results'], audit['rule_results']):
        require(len(outcomes) == len(expected_rules) and {r['id']: r['status'] for r in outcomes} == expected_rules,
                'Missing, failed or incomplete mandatory rules')
    provenance = reader.json(Path(audit_original).parent / 'provenance.json')
    require(provenance['audit_sha256'] == bindings['audit_sha256'] and provenance['incomplete_checks'] == [], 'Incomplete audit provenance')
    for group in ('inputs', 'verifier'):
        require(bool(provenance[group]), 'Missing audit provenance group')
        for item in provenance[group]:
            reader.register(item)
    for item in assessment['verifier']:
        reader.register(item)
    code = {canonical(p['path'], reader.root): p['sha256'] for p in run['archives']['code']}
    for component in [audit['audit'], *audit['audit']['components']]:
        require(code.get(canonical(component['path'], reader.root)) == component['sha256'], 'Audit/generation code pin mismatch')
    require({s['source']: s['sha256'] for s in audit['sources']} ==
            {f['source']: f['sha256'] for f in manifest['files']}, 'Audit/payload mismatch')
    if recipe['schema_version'] in [3,4]:
        require(assessment.get('resource_checks', {}).get('status') == 'passed', 'Resource checks did not pass')
        require(assessment['size'] == recipe['size'], 'Assessment size mismatch')
    if candidate:
        require(assessment['status'] == 'passed_local_candidate' and assessment.get('local_candidate_validated') is True,
                'Promotion requires a validated scale candidate')
        require(recipe['schema_version'] in [3,4] and recipe['size']['kind'] == 'scale', 'Promotion requires scale scope')
        require(run['resources'].get('forecast') is not None, 'Missing candidate resource forecast')
    _compatible(reader, recipe, expected)
    children = [validate_record(reader, p, expected=recipe, active=active, memo=memo) for p in recipe.get('prerequisites', [])]
    counts = [c['recipe']['patients'] for c in children]
    require(len(counts) == len(set(counts)), 'Duplicate prerequisite size')
    needed = ([100, 1000, 10000] if recipe.get('size', {}).get('kind') == 'scale' or (recipe['schema_version']==4 and recipe['patients']>10000)
              else ([1000] if recipe['schema_version'] in [3,4] and recipe['patients'] > 1000 else []))
    require(set(needed) <= set(counts), 'Missing required prerequisite assessments')
    if recipe['schema_version'] in [3,4]:
        claimed = {(p['assessment']['path'], p['assessment']['sha256']) for p in assessment['prerequisites']}
        require(claimed == {(p['path'], p['sha256']) for p in recipe['prerequisites']}, 'Prerequisite report mismatch')
    if recipe['schema_version']==4:
        for child in children:
            require({p['path']:p['sha256'] for p in child['run']['archives']['code']}==
                    {p['path']:p['sha256'] for p in run['archives']['code']},'Prerequisite implementation differs')
    record = dict(assessment=assessment, assessment_pin=assessment_pin, manifest=manifest,
                  manifest_original=str(manifest_original), run=run, run_original=str(run_original),
                  audit=audit, audit_original=str(audit_original), recipe=recipe, recipe_pin=recipe_pin, children=children)
    memo[identity] = record
    active.remove(identity)
    if isinstance(reader, LocalEvidence):
        for directory in [Path(run_original).parent, Path(audit_original).parent, assessment_original.parent]:
            reader.tree(directory)
        generation = Path(run_original).parent
        # Keep the CLI request, effective recipe, stage logs and preflight too.
        if generation.name == 'generation' and (generation.parent / 'request.json').is_file():
            reader.tree(generation.parent)
    return record


def verify_raw_files(raw_files, processed_files, resolve=None):
    """Load the independent checker locally or from its archived shared copy."""
    import importlib.util
    here = Path(__file__).resolve()
    checker = here.parent / 'check_raw_layout.py'
    if not checker.is_file():
        checker = here.parents[2] / 'verifier/check_raw_layout.py'
    spec = importlib.util.spec_from_file_location('sdv_raw_layout_check', checker)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.verify(raw_files, processed_files, resolve)


def _compatible(reader, recipe, expected):
    if expected is None:
        return
    for key in ('snapshot', 'rules', 'profiles', 'sources', 'seed'):
        require(recipe[key] == expected[key], 'Prerequisite scientific inputs differ: ' + key)
    left = reader.json(recipe['contract']['path'], recipe['contract']['sha256'])
    right = reader.json(expected['contract']['path'], expected['contract']['sha256'])
    require(set(left['common_inputs']) == set(right['common_inputs']), 'Prerequisite common input scope differs')
    for name in left['common_inputs']:
        a, b = left['common_inputs'][name], right['common_inputs'][name]
        require(reader.json(a['path'], a['sha256']) == reader.json(b['path'], b['sha256']),
                'Prerequisite common definition differs: ' + name)
