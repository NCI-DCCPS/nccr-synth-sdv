"""Rule composition and profile binding, separate from sampling algorithms.

The old integrated loader remains unchanged. New categorical joint requirements
may be added without extending this module, provided they use this contract.
"""
from copy import deepcopy
from pathlib import Path

from .common import digest, read_json

PARENTS = {
    '2.3.0': '1ef2a47229785b209d5e1514d548739816879233a57728811e81211e2e64c3ed',
    '3.1.0': '0ab9a6a00706c407fb8528393974b724d27ffa38d2ef83dfdfe491ba2fa198f8',
}
MCE_ID = 'mce_status_and_monthly_enrollment_are_consistent'
P16_ID = 'ctc_p16_applicability'
MERGE_POLICY = {
    'rule_id': MCE_ID,
    'base_version': '2.3.0',
    'added_from_3_1_0': ['special_code_requirements'],
    'precedence': ['8', '6', 'availability'],
    'death_boundary': 'death strictly before coverage end; post-death slots including slots beyond coverage',
    'evaluation': 'typed timing plus independently evaluated textual-clause bridge',
}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def checked(pin, resolver):
    require(type(pin) is dict and set(pin) == {'path', 'sha256'}, 'Invalid file pin')
    require(isinstance(pin['path'], str) and isinstance(pin['sha256'], str), 'Invalid pin values')
    path = Path(resolver(pin))
    require(digest(path) == pin['sha256'], 'Input hash mismatch: ' + pin['path'])
    return path


def compose_inherited(base, newer):
    """Canonical union; the sole known same-ID difference is explicitly merged."""
    require(base['version'] == '2.3.0' and newer['version'] == '3.1.0', 'Unsupported parent versions')
    rules = deepcopy(base['rules'])
    index = {r['id']: r for r in rules}
    require(len(index) == len(rules), 'Duplicate inherited rule ID')
    for rule in newer['rules']:
        if rule['id'] not in index:
            rules.append(deepcopy(rule))
        elif rule['id'] == MCE_ID:
            old = index[MCE_ID]
            require({k: v for k, v in old.items() if k != 'context_rule_id'} ==
                    {k: v for k, v in rule.items() if k != 'special_code_requirements'},
                    'Unexpected MCE inheritance conflict')
            old['special_code_requirements'] = deepcopy(rule['special_code_requirements'])
        else:
            require(index[rule['id']] == rule, 'Inherited rule conflict: ' + rule['id'])
    require(base['biological_constraints'] == newer['biological_constraints'] and
            base['biological_constraint_policy'] == newer['biological_constraint_policy'],
            'Biological policy inheritance conflict')
    return rules


def load_rules(pin, resolver):
    spec = read_json(checked(pin, resolver))
    composition = spec.get('composition')
    require(type(composition) is dict and set(composition) == {'parents', 'merge_policy'},
            'Missing Rule 4 composition contract')
    require(composition['merge_policy'] == MERGE_POLICY, 'Unsupported inheritance merge')
    require(type(composition['parents']) is dict and set(composition['parents']) == set(PARENTS),
            'Unsupported parent inventory')
    parents = {}
    for version, expected in PARENTS.items():
        item = composition['parents'][version]
        require(item.get('sha256') == expected, 'Unsupported inherited rule fingerprint')
        parents[version] = read_json(checked(item, resolver))
    inherited = compose_inherited(parents['2.3.0'], parents['3.1.0'])
    require(isinstance(spec.get('version'), str) and spec['version'].split('.')[0] == '4',
            'Expected a Rule 4 version')
    rules = spec.get('rules')
    require(type(rules) is list and all(type(r) is dict for r in rules), 'Invalid rule inventory')
    actual = {r.get('id'): r for r in rules}
    require(len(actual) == len(rules) and all(isinstance(k, str) and k for k in actual),
            'Duplicate or invalid rule ID')
    for rule in inherited:
        require(actual.get(rule['id']) == rule, 'Changed or omitted inherited rule: ' + rule['id'])
    for key in ['lookups', 'frequency_references', 'biological_constraints', 'biological_constraint_policy']:
        require(spec.get(key) == parents['2.3.0'][key], 'Changed inherited registry/policy: ' + key)
    inherited_ids = {r['id'] for r in inherited}
    extensions = [r for r in rules if r['id'] not in inherited_ids]
    require(bool(extensions), 'Rule 4 needs a conditional joint extension')
    for rule in extensions:
        require(set(rule) == {'id', 'type', 'type_version', 'enabled', 'source', 'joint_fields',
                             'distributions', 'reference_snapshot', 'semantics', 'severity'},
                'Unknown conditional joint requirement key')
        require(rule['type'] == 'conditional_joint_table' and type(rule['type_version']) is int
                and rule['type_version'] == 1, 'Unsupported extension capability')
        require(rule['enabled'] is True and rule['source'] == 'CTC' and rule['severity'] == 'error',
                'Unsupported joint requirement scope')
        fields = rule['joint_fields']
        require(type(fields) is list and len(fields) >= 2 and len(fields) == len(set(fields))
                and all(isinstance(f, str) and f for f in fields), 'Invalid joint field inventory')
        require(rule['semantics'] == 'profile_selected_conditional_count_distribution',
                'Unsupported joint distribution semantics')
        distributions = rule['distributions']
        require(type(distributions) is list and distributions, 'Missing required joint distributions')
        require(all(type(d) is dict and set(d) == {'id', 'given', 'outputs', 'select', 'broadcast', 'table'}
                    for d in distributions), 'Invalid required distribution contract')
        require(all(isinstance(d['id'], str) and d['id'] for d in distributions) and
                len({d['id'] for d in distributions}) == len(distributions), 'Duplicate distribution ID')
        for distribution in distributions:
            require(type(distribution['given']) is list and type(distribution['outputs']) is list and
                    distribution['outputs'], 'Invalid required draw fields')
            combined = distribution['given'] + distribution['outputs']
            require(all(isinstance(f, str) and f for f in combined) and
                    len(combined) == len(set(combined)), 'Duplicate required draw fields')
            table_path = Path(distribution['table'])
            require(not table_path.is_absolute() and '..' not in table_path.parts,
                    'Unsafe required count table path')
        require({f for d in distributions for f in d['given'] + d['outputs']} == set(fields),
                'Required distributions omit joint fields')
        checked(rule['reference_snapshot'], resolver)
    return spec, parents


def validate_profile(profile, spec, snapshot_pin, resolver):
    require(type(profile) is dict and set(profile) == {
        'schema_version', 'profile_id', 'version', 'context_policy', 'blocks', 'rule_bindings',
        'frequency_check', 'reference_scope', 'hierarchy_rule_id', 'source_limitations'},
        'Unknown replacement profile key')
    require(profile['schema_version'] == 1 and type(profile['schema_version']) is int,
            'Unsupported replacement profile schema')
    require(profile['context_policy'] == 'preserve_all_existing_context_bindings_and_sidecars',
            'CTC-only replacement must preserve context')
    frequency = profile['frequency_check']
    require(type(frequency) is dict and set(frequency) == {'method', 'family_alpha'}
            and frequency['method'] == 'independent_conditional_multinomial'
            and type(frequency['family_alpha']) in (int, float) and frequency['family_alpha'] == 0.001,
            'Invalid frequency acceptance contract')
    snapshot_path = checked(snapshot_pin, resolver)
    snapshot_paths = {snapshot_path.resolve()}
    for requirement in spec['rules']:
        if requirement['type'] == 'conditional_joint_table' and requirement['enabled']:
            snapshot_paths.add(checked(requirement['reference_snapshot'], resolver).resolve())
    inventory = {}
    for manifest_path in snapshot_paths:
        snapshot = read_json(manifest_path)
        local_paths = set()
        for item in snapshot['files']:
            relative = Path(item['path'])
            path = (manifest_path.parent / relative).resolve()
            require(not relative.is_absolute() and path.is_relative_to(manifest_path.parent.resolve()),
                    'Snapshot file escapes bundle')
            require(path not in local_paths and digest(path) == item['sha256'], 'Invalid snapshot inventory')
            local_paths.add(path)
            require(path not in inventory or inventory[path] == item['sha256'], 'Conflicting snapshot pins')
            inventory[path] = item['sha256']
    blocks = profile['blocks']
    require(type(blocks) is list and blocks, 'Empty sampling graph')
    block_ids = [b['id'] for b in blocks]
    require(len(set(block_ids)) == len(block_ids), 'Duplicate block ID')
    rule_ids = [r['id'] for b in blocks for r in b['rules']]
    require(len(set(rule_ids)) == len(rule_ids), 'Sampler rule IDs must be globally unique')
    for block in blocks:
        for rule in block['rules']:
            path = checked(rule['table'], resolver).resolve()
            require(inventory.get(path) == rule['table']['sha256'], 'Count table not in selected snapshot')
    # Conditional links to generated fields must consume the preceding draw,
    # including every fallback. Otherwise a reordered block can condition on
    # stale baseline values, or fallback can silently sever a cross-group link.
    writers = {}
    for position, block in enumerate(blocks):
        for draw in block['rules']:
            for field in draw['outputs']:
                writers.setdefault(field, set()).add(position)
    for position, block in enumerate(blocks):
        own_outputs = {field for draw in block['rules'] for field in draw['outputs']}
        for draw in block['rules']:
            linked = set(draw['given']) & set(writers) - own_outputs
            require(all(max(writers[field]) < position for field in linked),
                    'Conditional blocks must follow generated conditioning fields')
            require(all(linked <= set(level) for level in draw['backoff']),
                    'Fallback cannot discard generated conditioning fields')
    requirements = {r['id']: r for r in spec['rules'] if r['enabled'] and
                    (r['type'] == 'conditional_joint_table' or r['id'] == P16_ID)}
    bindings = profile['rule_bindings']
    require(type(bindings) is dict and set(bindings) == set(requirements), 'Incomplete rule-to-block binding')
    selected = []
    for key, ids in bindings.items():
        require(type(ids) is list and ids and len(set(ids)) == len(ids) and set(ids) <= set(block_ids),
                'Invalid bound block inventory')
        selected.extend(ids)
        block_map = {b['id']: b for b in blocks}
        bound = [block_map[block_id] for block_id in ids]
        req = requirements[key]
        if req['type'] == 'conditional_joint_table':
            require([block_ids.index(block_id) for block_id in ids] ==
                    sorted(block_ids.index(block_id) for block_id in ids),
                    'Required distribution execution order changed')
            required_snapshot = checked(req['reference_snapshot'], resolver)
            fields = {f for b in bound for r in b['rules'] for f in r['given'] + r['outputs']}
            require(fields == set(req['joint_fields']), 'Joint fields do not match their requirement')
            require(len(bound) == len(req['distributions']), 'Required joint draw coverage is incomplete')
            for block, target in zip(bound, req['distributions']):
                require(block['select'] == target['select'] and block['broadcast'] == target['broadcast'],
                        'Required joint grain, selector or broadcast mismatch')
                require(len(block['rules']) == 1, 'Each required joint tuple needs one draw rule')
                draw = block['rules'][0]
                require(draw['given'] == target['given'] and draw['outputs'] == target['outputs'],
                        'Required joint conditioning/output ownership mismatch')
                require(checked(draw['table'], resolver).resolve() ==
                        (required_snapshot.parent / target['table']).resolve(), 'Required joint target table mismatch')
        else:
            for block in bound:
                require(block['select'] is None and block['broadcast'] is None, 'p16 must cover every tumor')
                require(len(block['rules']) == 1, 'p16 requires one joint block')
                rule = block['rules'][0]
                joint_schema = (rule['given'] == [req['year_column']] and
                                set(rule['outputs']) == {req['schema_column'], req['target_column']})
                conditional_result = (rule['given'] == [req['year_column'], req['schema_column']] and
                                      rule['outputs'] == [req['target_column']])
                require((joint_schema or conditional_result) and rule['backoff'] == [],
                        'p16 requires exact year/schema support without fallback')
    require(len(selected) == len(set(selected)) and set(selected) == set(block_ids),
            'Unbound or multiply bound sampling block')
    hierarchy = next((r for r in spec['rules'] if r['id'] == profile['hierarchy_rule_id']), None)
    require(hierarchy is not None and hierarchy['enabled'] and hierarchy['type'] == 'hierarchical_lookup',
            'Missing selected hierarchy rule')
    return hierarchy
