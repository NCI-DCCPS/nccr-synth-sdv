from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest

from jamboree_sdv.common import WORKSPACE, read_json, digest, write_json
from jamboree_sdv.rule4_contract import compose_inherited, MCE_ID, load_rules, validate_profile


def parents():
    return [read_json(WORKSPACE / f'reference/generation-rules/v{v}/rules.json')
            for v in ('2.3.0', '3.1.0')]


def test_composition_preserves_both_branches_without_duplicate_ids():
    old, new = parents()
    before = deepcopy((old, new))
    result = compose_inherited(old, new)
    by_id = {r['id']: r for r in result}
    assert len(by_id) == len(result) == 17
    for source in (old, new):
        for rule in source['rules']:
            if rule['id'] != MCE_ID:
                assert by_id[rule['id']] == rule
    assert by_id[MCE_ID]['context_rule_id'] == 'mce_timing_from_synthetic_context'
    assert len(by_id[MCE_ID]['special_code_requirements']) == 3
    assert (old, new) == before


def test_unknown_same_id_conflict_is_rejected():
    old, new = parents()
    new['rules'][0]['enabled'] = False
    with pytest.raises(ValueError, match='conflict'):
        compose_inherited(old, new)


def test_mce_precedence_and_calendar_boundaries():
    path = Path(__file__).parents[1] / 'verifier/check_mce_inheritance.py'
    spec = importlib.util.spec_from_file_location('rule4_mce_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    p = {'deceased': 'true', 'synthetic_index_month': '1999-01', 'coverage_end_month': '2000-01',
         'death_offset_months': '5', 'index_year': '1999', 'index_age_completed_years': '0'}
    assert module.expected_special(p, -1) == '6'
    assert module.expected_special(p, 5) == '6'  # Death month itself is not after death.
    assert module.expected_special(p, 6) == '8'  # Death takes priority over year-1999 code 6.
    assert module.expected_special(p, 24) == '8'  # Post-coverage slots retain death code.
    p['death_offset_months'] = '12'
    assert module.expected_special(p, 12) == '6'
    assert module.expected_special(p, 13) is None  # Death at coverage end does not activate 8.
    p.update(deceased='false', index_year='2000', index_age_completed_years='12')
    assert module.expected_special(p, -12) == '6'
    assert module.expected_special(p, 1) is None


def production():
    recipe = read_json(WORKSPACE / 'experiments/gen-sdv/recipes/ctc-rules-v4-10pct-r4.json')
    resolver = lambda pin: WORKSPACE / pin['path']
    rules, _ = load_rules(recipe['rules'], resolver)
    profile = read_json(resolver(recipe['profile']))
    return recipe, rules, profile, resolver


@pytest.mark.parametrize('change', ['sex_only', 'missing_nonindex', 'loose_alpha',
                                   'reversed_execution', 'unconditional_fallback'])
def test_profile_cannot_weaken_required_target(change):
    recipe, rules, profile, resolver = production()
    if change == 'sex_only':
        draw = profile['blocks'][0]['rules'][0]
        draw['given'] = ['ageRecodeSingle90p', 'raceAndOriginRecode', 'iccc2017ExtendedRecode']
        draw['outputs'] = ['sex']  # Same union of fields, fundamentally different target.
    elif change == 'missing_nonindex':
        profile['blocks'].pop(1)
        profile['rule_bindings']['ctc_g1_conditional_joint'].pop()
    elif change == 'loose_alpha':
        profile['frequency_check']['family_alpha'] = 0.1
    elif change == 'reversed_execution':
        profile['blocks'].reverse()
    else:
        profile['blocks'][1]['rules'][0]['backoff'] = [[]]
    with pytest.raises(ValueError):
        validate_profile(profile, rules, recipe['snapshot'], resolver)


def test_mandatory_joint_cannot_be_disabled(tmp_path):
    recipe, rules, profile, resolver = production()
    next(r for r in rules['rules'] if r['id'] == 'ctc_g1_conditional_joint')['enabled'] = False
    path = tmp_path / 'disabled.json'
    write_json(path, rules)
    with pytest.raises(ValueError, match='scope'):
        load_rules({'path': str(path), 'sha256': digest(path)}, resolver)


def test_new_group_uses_separate_snapshot_without_code_changes(tmp_path):
    recipe, rules, profile, resolver = production()
    table = tmp_path / 'g2.csv'
    table.write_text('iccc2017ExtendedRecode,primarySite,count\n001,C421,10\n')
    table_pin = {'path': str(table), 'sha256': digest(table)}
    manifest = tmp_path / 'manifest.json'
    write_json(manifest, {'files': [{'path': 'g2.csv', 'sha256': digest(table)}]})
    manifest_pin = {'path': str(manifest), 'sha256': digest(manifest)}
    target = {'id': 'site_given_leaf', 'given': ['iccc2017ExtendedRecode'], 'outputs': ['primarySite'],
              'select': None, 'broadcast': None, 'table': 'g2.csv'}
    requirement = {'id': 'fixture_new_group', 'type': 'conditional_joint_table', 'type_version': 1,
                   'enabled': True, 'source': 'CTC', 'severity': 'error',
                   'joint_fields': ['iccc2017ExtendedRecode', 'primarySite'],
                   'reference_snapshot': manifest_pin, 'distributions': [target],
                   'semantics': 'profile_selected_conditional_count_distribution'}
    rules['rules'].append(requirement)
    profile['blocks'].append({'id': 'new_group', 'select': None, 'broadcast': None,
                              'rules': [{'id': 'new_group_draw', 'type': 'conditional_joint_table',
                                         'type_version': 1, 'given': target['given'], 'outputs': target['outputs'],
                                         'table': table_pin, 'count_column': 'count', 'backoff': []}]})
    profile['rule_bindings']['fixture_new_group'] = ['new_group']
    path = tmp_path / 'extended.json'
    write_json(path, rules)
    extended, _ = load_rules({'path': str(path), 'sha256': digest(path)}, resolver)
    assert validate_profile(profile, extended, recipe['snapshot'], resolver)['type'] == 'hierarchical_lookup'
