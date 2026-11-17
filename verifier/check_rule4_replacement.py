"""Independent composite-candidate audit, with archived-input replay.

Re-evaluates inherited requirements using the unchanged v2.3 input adapter and
explicit Rule 4 dispatch. Sampling modules are never imported by this checker.
"""
import argparse
from collections import Counter
from copy import deepcopy
import csv
import importlib.util
import io
from pathlib import Path
import resource
import shutil
import sys
import time
import zipfile

from jamboree_sdv.common import read_json, write_json, digest, now
from jamboree_sdv.rule4_contract import load_rules, validate_profile, P16_ID, MCE_ID
from check_conditional_joints import check_blocks
from check_mce_inheritance import check as check_mce


def require(ok, message):
    if not ok:
        raise ValueError(message)


def rows(path, source):
    # Repeated categorical strings dominate a full CTC CSV. Bound each cache so
    # unique identifiers/free text cannot cause unbounded auxiliary storage.
    pools = {}
    with zipfile.ZipFile(path) as archive:
        require(archive.testzip() is None and len(archive.namelist()) == len(set(archive.namelist())),
                'Invalid ZIP archive')
        with archive.open(source + '.csv') as binary, io.TextIOWrapper(binary, newline='') as stream:
            reader = csv.DictReader(stream)
            require(reader.fieldnames and len(set(reader.fieldnames)) == len(reader.fieldnames), 'Invalid CSV header')
            for row in reader:
                require(None not in row and all(isinstance(v, str) for v in row.values()), 'Malformed CSV row')
                for field, value in row.items():
                    if field not in {'nccrId', 'tumorRecordNumber'} and len(value) <= 32:
                        pool = pools.setdefault(field, {})
                        if value in pool:
                            row[field] = pool[value]
                        elif len(pool) < 4096:
                            pool[value] = value
                yield row


def _audit(recipe_path, candidate_path, workspace, out, verifier_code):
    start = time.monotonic()
    workspace = Path(workspace).resolve()
    recipe_path = (workspace / recipe_path).resolve()
    candidate_path = (workspace / candidate_path).resolve()
    out = (workspace / out).resolve()
    recipe, candidate = read_json(recipe_path), read_json(candidate_path)
    generation_path = Path(candidate_path).parent / 'generation.json'
    claimed_generation = candidate['replacement']['generation']
    require((workspace / claimed_generation['path']).resolve() == generation_path and
            digest(generation_path) == claimed_generation['sha256'], 'Unbound generation evidence')
    generation = read_json(generation_path)
    aliases, read_pins = {}, {}

    def canonical(path):
        return str((workspace / path).resolve())

    for entry in generation['archived_inputs']:
        old, archive = entry['original'], entry['archive']
        archive_path = Path(canonical(archive['path']))
        require(archive_path.is_relative_to(workspace) and
                digest(archive_path) == archive['sha256'] == old['sha256'], 'Changed archived input')
        key = (canonical(old['path']), old['sha256'])
        require(key not in aliases, 'Duplicate archived input binding')
        aliases[key] = archive_path

    def resolve(pin):
        require(set(pin) == {'path', 'sha256'}, 'Invalid independent input pin')
        original = Path(canonical(pin['path']))
        require(original.is_relative_to(workspace), 'Input outside workspace')
        path = aliases.get((str(original), pin['sha256']), original)
        require(path.resolve().is_relative_to(workspace) and digest(path) == pin['sha256'], 'Input hash mismatch')
        read_pins[str(path)] = pin['sha256']
        return path

    baseline = read_json(resolve(recipe['baseline']))
    recipe_pin = {'path': str(recipe_path), 'sha256': digest(recipe_path)}
    require(generation['recipe']['sha256'] == recipe_pin['sha256'] and
            candidate['replacement']['recipe']['sha256'] == recipe_pin['sha256'], 'Recipe provenance mismatch')
    for name in ['baseline', 'rules', 'profile', 'snapshot']:
        require(generation[name] == recipe[name], 'Generation input binding mismatch: ' + name)
    require(candidate['replacement']['baseline'] == recipe['baseline'] and generation['seed'] == recipe['seed'],
            'Baseline or seed provenance mismatch')
    require(candidate['release_id'] == generation['recipe_id'] == recipe['recipe_id'] and
            candidate['kind'] == 'gen_sdv_ctc_replacement_candidate' and candidate['promoted'] is False,
            'Candidate identity or publication-state mismatch')
    require(type(generation['code_pins']) is list and generation['code_pins'], 'Missing generator code provenance')
    code_names = set()
    for entry in generation['code_pins']:
        source, archive = entry['original'], entry['archive']
        archived_path = resolve(archive)
        require(archive['sha256'] == source['sha256'] and archived_path.name not in code_names,
                'Generator code provenance mismatch')
        code_names.add(archived_path.name)
    require(code_names == {name + '.py' for name in ['__init__', 'common', 'random_streams',
            'conditional_joint', 'joint_blocks', 'rule4_contract', 'ctc_replacement']},
            'Incomplete generator code inventory')
    spec, parents = load_rules(recipe['rules'], resolve)
    profile = read_json(resolve(recipe['profile']))
    hierarchy = validate_profile(profile, spec, recipe['snapshot'], resolve)
    hierarchy = {k: hierarchy[k] for k in ['leaf_column', 'parent_columns', 'mapping']}
    require(candidate['rules']['sha256'] == recipe['rules']['sha256'] and
            candidate['rules_version'] == spec['version'], 'Candidate rule binding mismatch')
    resolve(candidate['rules'])
    files, raw = {}, {}
    preserved = []
    for inventory, dest in [('files', files), ('raw_files', raw)]:
        original = {f['source']: f for f in baseline[inventory]}
        require(len(original) == len(baseline[inventory]), 'Duplicate baseline source')
        for item in candidate[inventory]:
            source = item['source']
            require(source not in dest, 'Duplicate candidate source')
            path = resolve({'path': item['local_path'], 'sha256': item['sha256']})
            require(path.stat().st_size == item['size_bytes'], 'Payload size mismatch')
            dest[source] = item
            if source != 'CTC':
                require(item == original[source], 'Other-source inventory changed: ' + source)
                preserved.append({'inventory': inventory, 'source': source, 'sha256': item['sha256']})
        require(set(dest) == set(original), 'Incomplete candidate inventory')
    require(files['CTC'] == raw['CTC'], 'CTC raw/processed references differ')
    require(generation['ctc'] == files['CTC'] and generation['counts'] == recipe['expected'],
            'Generated payload/count provenance mismatch')
    base_ctc = next(f for f in baseline['files'] if f['source'] == 'CTC')
    before = list(rows(resolve({'path': base_ctc['local_path'], 'sha256': base_ctc['sha256']}), 'CTC'))
    after = list(rows(files['CTC']['local_path'], 'CTC'))
    key = lambda r: (r['nccrId'], r['tumorRecordNumber'])
    require([key(r) for r in before] == [key(r) for r in after] and len({key(r) for r in after}) == len(after),
            'Patient/tumor identities or order changed')
    patients = {r['nccrId'] for r in after}
    require('' not in patients and len(after) == recipe['expected']['rows'] == files['CTC']['rows'] and
            len(patients) == recipe['expected']['patients'] == files['CTC']['patients'], 'CTC counts mismatch')
    index = Counter(r['nccrId'] for r in after if r['indexCancer'] in {'1', '2'})
    require(set(index) == patients and set(index.values()) == {1}, 'Index tumor cardinality mismatch')
    require(candidate['context']['sha256'] == baseline['context']['sha256'] and
            candidate['context_contract']['sha256'] == baseline['context_contract']['sha256'], 'Context changed')
    context_path = resolve(candidate['context'])
    context = read_json(context_path)
    for name in ['patient', 'tumor']:
        item = context[name]
        require(digest(context_path.parent / item['path']) == item['sha256'], 'Changed context sidecar')
    contract = read_json(resolve(candidate['context_contract']))
    protected = ['nccrId', 'tumorRecordNumber'] + [k.split('.', 1)[1] for k in contract['raw_bindings']]
    joint = check_blocks(before, after, profile['blocks'], resolve, protected, hierarchy,
                         family_alpha=profile['frequency_check']['family_alpha'])
    reported_blocks = {b['id']: b for b in generation['blocks']['blocks']}
    require(len(reported_blocks) == len(generation['blocks']['blocks']) == len(joint['blocks']),
            'Generation block provenance mismatch')
    for block in joint['blocks']:
        claims = {r['id']: r for r in reported_blocks[block['id']]['sampler_report']['rules']}
        require(len(claims) == len(block['rules']), 'Generation draw-rule provenance mismatch')
        for rule_result in block['rules']:
            claim = claims[rule_result['id']]
            require(claim['draw_count'] == rule_result['n'] and
                    claim['backoff_draw_count'] == rule_result['backoff_rows'], 'Incorrect reported fallback use')
            claimed = {(tuple(c['conditioning_fields']), tuple(c['condition_values'])): c['draw_count']
                       for c in claim['effective_conditions']}
            actual = {(tuple(c['given']), tuple(c['values'])): c['n'] for c in rule_result['effective_strata']}
            require(len(claimed) == len(claim['effective_conditions']) and claimed == actual,
                    'Incorrect reported effective conditioning counts')
    print('Conditional output checks: ' + joint['status'], flush=True)
    rule = next(r for r in spec['rules'] if r['id'] == P16_ID)
    snapshot = resolve(recipe['snapshot']).parent
    checker_path = snapshot / 'provenance/p16_rules.py'
    module_spec = importlib.util.spec_from_file_location('archived_rule4_p16', checker_path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    p16 = module.P16Check(rule)
    for row in after:
        p16.add(row)
    p16_result = p16.finish()
    row_count, patient_count = len(after), len(patients)
    del before, after  # Release full-width frames before scanning inherited sources.
    # Load inherited audits from this archived directory when replaying, otherwise
    # from the read-only workspace. No shared files are edited or byte-compiled.
    here = Path(__file__).resolve().parent
    if not (here / 'audit_release.py').exists():
        sys.path.insert(0, str(workspace / 'tools'))
        sys.path.insert(0, str(here))
    from audit_release import audit_file
    from integrated_rules import evaluate_all
    from jamboree_sdv.promotion_evidence import SharedEvidence
    run = read_json(resolve(baseline['run_manifest']))
    promotion = read_json(resolve(baseline['promotion']))
    # A qualification baseline may itself be an isolated promoted workspace.
    # Its logical run path, not the new archived copy's path, locates that root.
    baseline_run_path = (workspace / baseline['run_manifest']['path']).resolve()
    shared_root = baseline_run_path.parents[3]
    require(shared_root.is_relative_to(workspace) and
            baseline_run_path.relative_to(shared_root).parts[:2] == ('runs', 'gen-sdv'),
            'Invalid promoted baseline layout')
    reader = SharedEvidence(shared_root, promotion)
    baseline_recipe = run['recipe']
    metadata = reader.path(baseline_recipe['snapshot']['path'], baseline_recipe['snapshot']['sha256']).parent / 'metadata'
    source_results = []
    for item in candidate['files']:
        result, _ = audit_file(item, workspace, metadata if item['source'] != 'CPI' else None)
        source_results.append(result)
        print(f"{item['source']}: {result['status']} ({result.get('rows')} rows)", flush=True)
    original_run = read_json(resolve(run['origin']['run']))
    original_candidate = read_json(resolve(run['origin']['manifest']))
    adapted_logical = Path(original_candidate['run_manifest']['path']).parent / 'inputs/adapted/preflight.json'
    adapted = reader.json(adapted_logical, original_run['archives']['adapted_sha256'])

    def inherited_resolver(pin):
        if Path(canonical(pin['path'])).is_relative_to(candidate_path.parent):
            return resolve(pin)
        return reader.path(pin['path'], pin['sha256'])

    inherited_rules = [r for r in spec['rules'] + spec['biological_constraints']
                       if r['type'] != 'conditional_joint_table' and r['id'] != P16_ID]
    inherited_candidate = deepcopy(candidate)
    for name in ['context', 'context_contract']:
        inherited_candidate[name]['path'] = str(resolve(candidate[name]))
    for inventory in ['files', 'raw_files']:
        for item in inherited_candidate[inventory]:
            item['local_path'] = str(resolve({'path': item['local_path'], 'sha256': item['sha256']}))
    inherited, extensions = evaluate_all(baseline_recipe, inherited_candidate, source_results, inherited_rules,
                                        resolver=inherited_resolver, adapted=adapted)
    bridge = check_mce(Path(files['MCE']['local_path']), context_path.parent / context['patient']['path'])
    merged = next(r for r in inherited if r['id'] == MCE_ID)
    # Preserve raw evaluator output for transparency, then bind every prose clause
    # to explicit evaluated evidence instead of retaining unresolved child checks.
    merged['legacy_eligibility_reporting'] = merged.pop('eligibility_checks', [])
    merged['eligibility_checks'] = bridge['clauses']
    merged['typed_timing_rule'] = 'mce_timing_from_synthetic_context'
    if bridge['status'] != 'passed':
        merged['status'] = 'findings'
    new_results = [{'id': P16_ID, 'type': rule['type'], **p16_result}]
    for requirement in spec['rules']:
        if requirement['type'] == 'conditional_joint_table':
            new_results.append({'id': requirement['id'], 'type': requirement['type'],
                                'status': joint['status'] if requirement['enabled'] else 'disabled',
                                'evidence': 'conditional_joint_checks'})
    by_id = {r['id']: r for r in inherited + new_results}
    results = [by_id[r['id']] for r in spec['rules'] + spec['biological_constraints']]
    findings = [r['id'] for r in results if r['status'] not in {'passed', 'disabled'}]
    if joint['status'] != 'passed':
        findings.append('conditional_graph_integrity_or_frequency')
    exceptions = []
    for source in source_results:
        if source['status'] == 'passed_checks':
            continue
        # The only inherited annotation exception is COG's literal 7777 width.
        permitted = source['source'] == 'COG' and source['status'] == 'findings'
        for kind, check in source['checks'].items():
            permitted &= not any(check[k] for k in ['missing_columns', 'extra_columns', 'vocabulary_issues',
                                                   'numeric_type_issues', 'whitespace_cells'])
            for entry in check['length_advisories']:
                permitted &= entry['stated_max'] == 3 and set(entry['examples']) == {'7777'}
                exceptions.append({'source': source['source'], 'dictionary': kind, **entry})
        permitted &= not source.get('metadata_errors') and source['sha256_matches_manifest'] is True
        permitted &= not any(source.get(k) for k in ['row_width_errors', 'blank_records', 'duplicate_headers'])
        if not permitted:
            findings.append('metadata:' + source['source'])
    for path, expected in read_pins.items():
        require(digest(path) == expected, 'Input changed during audit')
    for entry in verifier_code:
        require(digest(entry['source']['path']) == entry['source']['sha256'], 'Verifier changed during audit')
    report = {'schema_version': 1, 'kind': 'independent_rule4_ctc_replacement_assessment',
              'created_at': now(), 'complete': True, 'status': 'passed' if not findings else 'findings',
              'exit_code': int(bool(findings)), 'findings': findings, 'rows': row_count, 'patients': patient_count,
              'recipe': {'path': str(recipe_path), 'sha256': digest(recipe_path)},
              'candidate': {'path': str(candidate_path), 'sha256': digest(candidate_path)},
              'generation': {'path': str(generation_path), 'sha256': digest(generation_path)},
              'rule_results': results, 'sources': source_results, 'annotation_exceptions': exceptions,
              'context_and_identity': {'status': 'passed', 'protected_fields': protected,
                                       'context': candidate['context'], 'same_ordered_tumor_keys': True},
              'unchanged_other_payloads': preserved, 'conditional_joint_checks': joint,
              'p16_applicability': p16_result, 'mce_inheritance_bridge': bridge,
              'extension_checks': extensions, 'input_reads': read_pins,
              'generator_environment': generation.get('environment'),
              'verifier_code': verifier_code,
              'limitations': profile['source_limitations'], 'promoted': False,
              'metrics': {'wall_seconds': time.monotonic() - start,
                          'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}}
    write_json(out / 'audit.json', report)
    (out / 'summary.md').write_text(
        f"# Rule 4 CTC candidate assessment\n\nStatus: {report['status']}. "
        f"{row_count:,} tumor rows; {patient_count:,} patients.\n\n"
        "Patient/tumor keys, all context bindings and other source payloads remain unchanged.\n"
        "G1 uses staged conditional draws with retained ages; unrestricted source joint fidelity is not claimed.\n")
    return report


def archive_verifier(workspace, out):
    """Archive the standalone checker and its exact declarative dependencies."""
    here = Path(__file__).resolve().parent
    selected = {}
    directories = [here] if (here / 'audit_release.py').exists() else [workspace / 'tools', here]
    for directory in directories:
        for path in directory.glob('*.py'):
            selected[path.name] = path
    from jamboree_sdv import common
    for name in ['__init__', 'common', 'rule4_contract', 'integrated_inputs',
                 'catalog_contract', 'promotion_evidence']:
        selected['jamboree_sdv/' + name + '.py'] = Path(common.__file__).parent / (name + '.py')
    result = []
    for relative, source in sorted(selected.items()):
        target = out / 'verifier' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        sha = digest(source)
        require(digest(target) == sha, 'Verifier archival mismatch')
        result.append({'source': {'path': str(source), 'sha256': sha},
                       'archive': {'path': str(target), 'sha256': sha}})
    return result


def audit(recipe_path, candidate_path, workspace, out):
    workspace = Path(workspace).resolve()
    out = (workspace / out).resolve()
    require(out.is_relative_to(workspace / 'experiments/gen-sdv/work') and not out.exists(),
            'Audit output must be fresh and experiment-local')
    out.mkdir(parents=True)
    try:
        verifier_code = archive_verifier(workspace, out)
        write_json(out / 'started.json', {'started_at': now(), 'verifier_code': verifier_code})
        report = _audit(recipe_path, candidate_path, workspace, out, verifier_code)
        write_json(out / 'completion.json', {'complete': True, 'exit_code': report['exit_code'],
                                           'audit_sha256': digest(out / 'audit.json')})
        return report
    except Exception as exc:
        report = {'schema_version': 1, 'kind': 'independent_rule4_ctc_replacement_assessment',
                  'created_at': now(), 'complete': False, 'status': 'error', 'exit_code': 2,
                  'findings': [type(exc).__name__ + ': ' + str(exc)]}
        target = out / ('error.json' if (out / 'audit.json').exists() else 'audit.json')
        write_json(target, report)
        if not (out / 'completion.json').exists():
            write_json(out / 'completion.json', {'complete': False, 'exit_code': 2,
                                               'error_report': {'path': str(target), 'sha256': digest(target)}})
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ['recipe', 'candidate', 'workspace', 'out']:
        parser.add_argument('--' + key, required=True, type=Path)
    args = parser.parse_args(argv)
    report = audit(args.recipe, args.candidate, args.workspace, args.out)
    print(report['status'], flush=True)
    return report['exit_code']


if __name__ == '__main__':
    sys.dont_write_bytecode = True
    raise SystemExit(main())
