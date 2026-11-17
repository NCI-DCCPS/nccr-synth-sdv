"""Synthetic fixtures only; no sampler imports, reference data or source CSVs."""

from copy import deepcopy
import csv
import hashlib
import importlib.util
import io
import json
import math
from pathlib import Path
import random

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / 'verifier/check_conditional_joints.py'
MODULE_SPEC = importlib.util.spec_from_file_location('independent_conditional_joints', MODULE_PATH)
checker = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(checker)


def payload(header, cells):
    stream = io.StringIO(newline='')
    csv.writer(stream).writerows([header, *cells])
    return stream.getvalue().encode('utf-8')


def fixture(cells, given=('g',), outputs=('x', 'y'), backoff=(), header=None):
    data = payload(header or [*given, *outputs, 'count'], cells)
    spec = {'id': 'synthetic', 'type': 'conditional_joint_table', 'type_version': 1,
            'given': list(given), 'outputs': list(outputs), 'count_column': 'count',
            'backoff': [list(b) for b in backoff],
            'table': {'path': 'synthetic-fixture.csv', 'sha256': hashlib.sha256(data).hexdigest()}}

    def resolver(pin):
        assert pin == spec['table']  # The shared API passes the entire pin.
        return data

    return spec, resolver


def coupled():
    return fixture([['A', '0', '0', 49], ['A', '0', '1', 1],
                    ['A', '1', '0', 1], ['A', '1', '1', 49]])


def draws(cells, fields=('g', 'x', 'y')):
    return [dict(zip(fields, cell[:-1])) for cell in cells for _ in range(cell[-1])]


def codes(report):
    return {finding['code'] for finding in report['findings']}


def block(rule, select=None, broadcast=None, identifier='b'):
    return {'id': identifier, 'select': select, 'broadcast': broadcast, 'rules': [rule]}


def test_pinned_table_and_empirical_joint_pass_with_predeclared_bounds():
    spec, resolver = coupled()
    rows = draws([['A', '0', '0', 490], ['A', '0', '1', 10],
                  ['A', '1', '0', 10], ['A', '1', '1', 490]])
    original = deepcopy(rows)
    report = checker.check_joint_rows(iter(rows), spec, resolver)
    assert report['status'] == 'passed'
    assert rows == original
    assert report['n'] == report['evaluated_n'] == 1000
    assert report['table']['pin_verified'] is True
    assert report['table']['total_count'] == 100
    assert report['conditional_weighted_total_variation'] == 0
    assert report['backoff_rowmass'] == 0
    assert report['bound_test_count'] == 12
    assert report['acceptance']['tv_is_descriptive'] is True
    for test in report['worst_deviations']:
        if test['kind'] == 'conditional_joint' and test['cell'] == ['0', '1']:
            assert test['expected'] == 10
            assert test['variance'] == pytest.approx(9.9)
            t = math.log(2 * 12 / .001)
            assert test['bound'] == pytest.approx(t / 3 + math.sqrt(2 * 9.9 * t + t * t / 9))
            break
    else:
        pytest.fail('Missing conditional count diagnostic')
    json.dumps(report, allow_nan=False)


def test_independent_marginals_rejected_despite_correct_marginal_counts():
    spec, resolver = coupled()
    rows = draws([['A', x, y, 2500] for x in ('0', '1') for y in ('0', '1')])
    report = checker.check_joint_rows(rows, spec, resolver)
    assert codes(report) == {'frequency_bounds'}
    assert report['unsupported_output_rows'] == 0
    assert report['test_families']['output_marginal']['violations'] == 0
    assert report['test_families']['output_pair']['violations'] == 4
    assert report['conditional_weighted_total_variation'] == pytest.approx(.48)


def test_sparse_singleton_strata_pool_conditional_pairs_for_power():
    cells = [[str(g), x, y, weight] for g in range(800)
             for x, y, weight in [('0', '0', 49), ('0', '1', 1), ('1', '0', 1), ('1', '1', 49)]]
    spec, resolver = fixture(cells)
    good = [{'g': str(i), 'x': str(i % 2), 'y': str(i % 2)} for i in range(800)]
    independent = [{'g': str(i), 'x': str(i % 2), 'y': str((i // 2) % 2)} for i in range(800)]
    accepted = checker.check_joint_rows(good, spec, resolver)
    rejected = checker.check_joint_rows(independent, spec, resolver)
    assert accepted['status'] == 'passed'
    assert accepted['conditional_weighted_total_variation'] > .5  # No arbitrary TV cutoff.
    coverage = accepted['sparse_condition_coverage']
    assert coverage['singleton_strata'] == coverage['sparse_strata'] == 800
    assert coverage['sparse_row_fraction'] == 1
    assert rejected['status'] == 'findings'
    assert rejected['test_families']['conditional_joint']['violations'] == 0
    assert rejected['test_families']['output_pair']['violations'] == 4


def test_three_way_joint_detects_dependence_not_visible_in_pairs():
    cells = [[str(g), str(x), str(y), str(z), 49 if (x + y + z) % 2 == 0 else 1]
             for g in range(1600) for x in range(2) for y in range(2) for z in range(2)]
    spec, resolver = fixture(cells, outputs=('x', 'y', 'z'))
    rows = [{'g': str(i), 'x': str(i % 2), 'y': str((i // 2) % 2), 'z': str((i // 4) % 2)}
            for i in range(1600)]
    report = checker.check_joint_rows(rows, spec, resolver)
    assert report['test_families']['conditional_joint']['violations'] == 0
    assert report['test_families']['output_pair']['violations'] == 0
    assert report['test_families']['output_joint']['violations'] == 8


def test_rare_cell_uses_binomial_variance_and_catches_excess():
    spec, resolver = fixture([['A', 'usual', 9999], ['A', 'rare', 1]], outputs=('x',))
    rows = draws([['A', 'usual', 9970], ['A', 'rare', 30]], fields=('g', 'x'))
    report = checker.check_joint_rows(rows, spec, resolver)
    assert report['status'] == 'findings'
    rare = next(t for t in report['worst_deviations'] if t['cell'] == ['rare'])
    assert rare['expected'] == 1
    assert rare['variance'] == pytest.approx(.9999)
    assert rare['bound'] < 10  # Hoeffding count bounds would exceed 200 here.
    assert rare['exceeds_bound']


def test_random_draws_and_unobserved_rare_cells_do_not_require_large_counts():
    spec, resolver = fixture([['A', 'usual', 999], ['A', 'rare', 1]], outputs=('x',))
    rng = random.Random(841)
    rows = [{'g': 'A', 'x': 'rare' if rng.random() < .001 else 'usual'} for _ in range(1000)]
    assert checker.check_joint_rows(rows, spec, resolver)['status'] == 'passed'
    assert checker.check_joint_rows([{'g': 'A', 'x': 'usual'}], spec, resolver)['status'] == 'passed'


def test_pooled_targets_and_variance_use_realized_condition_mix():
    spec, resolver = fixture([['A', '0', 9], ['A', '1', 1],
                              ['B', '0', 1], ['B', '1', 9]], outputs=('x',))
    rows = draws([['A', '0', 810], ['A', '1', 90], ['B', '0', 10], ['B', '1', 90]], ('g', 'x'))
    report = checker.check_joint_rows(rows, spec, resolver)
    assert report['status'] == 'passed'
    zero = next(t for t in report['worst_deviations'] if t['kind'] == 'output_marginal' and t['cell'] == ['0'])
    assert zero['expected'] == 820  # Not the reference's unconditional 50%.
    assert zero['variance'] == pytest.approx(90)  # Not 1000*.82*.18.


def test_zero_probability_strata_contribute_observed_pooled_counts():
    spec, resolver = fixture([['A', '0', 1], ['B', '1', 1]], outputs=('x',))
    rows = draws([['A', '1', 20], ['B', '1', 20]], ('g', 'x'))
    report = checker.check_joint_rows(rows, spec, resolver)
    one = next(t for t in report['worst_deviations'] if t['kind'] == 'output_marginal' and t['cell'] == ['1'])
    assert one['observed'] == 40 and one['expected'] == 20
    assert one['bound'] == 0
    assert 'unsupported_outputs' in codes(report)


def test_first_present_explicit_backoff_aggregates_counts_and_effective_strata():
    spec, resolver = fixture([['A', 'u', '0', 9], ['A', 'u', '1', 1],
                              ['A', 'v', '0', 1], ['A', 'v', '1', 9],
                              ['B', 'u', '1', 80]], given=('g', 'h'), outputs=('x',),
                             backoff=(('g',), ('h',), ()))
    rows = draws([['A', 'missing', '0', 50], ['A', 'missing', '1', 50],
                  ['A', 'other', '0', 50], ['A', 'other', '1', 50]], ('g', 'h', 'x'))
    report = checker.check_joint_rows(rows, spec, resolver)
    assert report['status'] == 'passed'
    assert len(report['effective_strata']) == 1
    assert report['effective_strata'][0]['reference_count'] == 20
    assert report['effective_strata'][0]['n'] == 200
    assert report['backoff_rowmass'] == 1
    assert all(c['backoff_level'] == 1 for c in report['conditions'])
    # A primary cell exists: never back off merely because its output is unsupported.
    bad = checker.check_joint_rows([{'g': 'B', 'h': 'u', 'x': '0'}], spec, resolver)
    assert 'unsupported_outputs' in codes(bad) and bad['backoff_rows'] == 0


def test_shared_backoff_cannot_cancel_opposite_original_condition_errors():
    spec, resolver = fixture([['known', '0', 1], ['known', '1', 1]],
                             outputs=('x',), backoff=((),))
    rows = draws([['missing-A', '0', 1000], ['missing-B', '1', 1000]], ('g', 'x'))
    report = checker.check_joint_rows(rows, spec, resolver)
    assert codes(report) == {'frequency_bounds'}
    assert report['conditional_weighted_total_variation'] == pytest.approx(.5)
    assert len(report['effective_strata']) == 1
    assert report['effective_strata'][0]['total_variation'] == 0
    assert report['backoff_rowmass'] == 1
    assert report['test_families']['conditional_joint'] == {'test_count': 4, 'violations': 4}
    assert report['test_families']['output_marginal']['violations'] == 0
    assert report['bound_test_count'] == 6
    assert report['acceptance']['tail_log_factor'] == pytest.approx(math.log(2 * 6 / .001))
    conditional = [t for t in report['worst_deviations'] if t['kind'] == 'conditional_joint']
    assert {tuple(t['values']) for t in conditional} == {('missing-A',), ('missing-B',)}
    assert all(t['given'] == ['g'] and t['effective_given'] == [] for t in conditional)
    assert all(t['expected'] == 500 and t['variance'] == 250 for t in conditional)


@pytest.mark.parametrize('observed', ['common', 'rare'])
@pytest.mark.parametrize('common_first', [True, False])
def test_unrepresentable_positive_probability_and_complement_rejected(observed, common_first):
    cells = [['A', 'common', 10 ** 400], ['A', 'rare', 1]]
    if not common_first:
        cells.reverse()
    spec, resolver = fixture(cells, outputs=('x',))
    message = 'probability complement' if common_first else 'probability is'
    with pytest.raises(ValueError, match=message):
        checker.check_joint_rows([{'g': 'A', 'x': observed}], spec, resolver)


def test_probability_underflow_in_unselected_backoff_is_also_rejected():
    spec, resolver = fixture([['A', 'common', 10 ** 400], ['B', 'rare', 1]],
                             outputs=('x',), backoff=((),))
    with pytest.raises(ValueError, match='not representable'):
        checker.check_joint_rows([{'g': 'A', 'x': 'common'}], spec, resolver)


def test_determinism_uses_integer_support_not_rounded_probability():
    spec, resolver = fixture([['A', 'common', 10 ** 100], ['A', 'rare', 1]], outputs=('x',))
    report = checker.check_joint_rows([{'g': 'A', 'x': 'common'}], spec, resolver)
    assert report['status'] == 'passed'
    assert all(not t['deterministic'] and t['variance'] > 0 and t['bound'] > 0
               for t in report['worst_deviations'])
    common = next(t for t in report['worst_deviations'] if t['cell'] == ['common'])
    assert common['expected'] == 1.0  # Rounded p does not make its support deterministic.
    spec, resolver = fixture([['A', 'common', 10 ** 400]], outputs=('x',))
    deterministic = checker.check_joint_rows([{'g': 'A', 'x': 'common'}], spec, resolver)
    assert deterministic['status'] == 'passed'
    assert all(t['deterministic'] and t['bound'] == 0 for t in deterministic['worst_deviations'])


def test_backoff_order_is_authoritative_and_no_implicit_global_fallback():
    spec, resolver = fixture([['A', 'u', '0', 1], ['B', 'v', '1', 1]],
                             given=('g', 'h'), outputs=('x',), backoff=(('g',), ('h',)))
    row = {'g': 'A', 'h': 'v', 'x': '0'}
    assert checker.check_joint_rows([row], spec, resolver)['status'] == 'passed'
    reverse = deepcopy(spec)
    reverse['backoff'].reverse()
    assert 'unsupported_outputs' in codes(checker.check_joint_rows([row], reverse, resolver))
    unknown = checker.check_joint_rows([{'g': 'C', 'h': 'w', 'x': '0'}], spec, resolver)
    assert codes(unknown) == {'unknown_conditions'}
    assert unknown['evaluated_n'] == unknown['bound_test_count'] == 0


def test_unconditional_and_explicit_global_backoff_are_supported():
    spec, resolver = fixture([['00', 1]], given=(), outputs=('x',))
    report = checker.check_joint_rows([{'x': '00'}], spec, resolver)
    assert report['status'] == 'passed'
    assert all(t['bound'] == 0 for t in report['worst_deviations'])
    spec, resolver = fixture([['A', '00', 1]], outputs=('x',), backoff=((),))
    report = checker.check_joint_rows([{'g': 'unknown', 'x': '00'}], spec, resolver)
    assert report['status'] == 'passed' and report['backoff_rowmass'] == 1


def test_strings_explicit_blanks_and_declared_column_set_not_header_order():
    spec, resolver = fixture([[1, '', '001', '0']], header=['count', 'y', 'g', 'x'])
    report = checker.check_joint_rows([{'g': '001', 'x': '0', 'y': ''}], spec, resolver)
    assert report['status'] == 'passed'
    assert 'unknown_conditions' in codes(checker.check_joint_rows([{'g': '1', 'x': '0', 'y': ''}], spec, resolver))


def test_empty_evaluation_is_not_a_pass_and_all_results_are_json_safe():
    spec, resolver = coupled()
    report = checker.check_joint_rows([], spec, resolver)
    assert codes(report) == {'empty_evaluation'}
    assert report['bound_test_count'] == 0
    json.dumps(report, allow_nan=False)


def test_table_pin_verified_independently_of_resolver():
    spec, resolver = coupled()
    with pytest.raises(ValueError, match='hash'):
        checker.check_joint_rows([], spec, lambda pin: resolver(pin) + b'\n')


def test_uppercase_hash_is_normalized_without_mutating_spec():
    spec, resolver = coupled()
    data = resolver(spec['table'])
    expected = dict(spec['table'])
    spec['table']['sha256'] = spec['table']['sha256'].upper()
    original = deepcopy(spec)

    def resolve(pin):
        assert pin == expected
        return data

    assert checker.check_joint_rows([{'g': 'A', 'x': '0', 'y': '0'}], spec, resolve)['status'] == 'passed'
    assert spec == original


@pytest.mark.parametrize('weight', [1, 10, 50])
def test_finite_sample_binomial_false_positive_mass_is_bounded(weight):
    # Exact enumeration checks the fixed analytical rule, not a threshold fit.
    spec, resolver = fixture([['0', weight], ['1', 100 - weight]], given=(), outputs=('x',))
    n, alpha, p = 20, .05, weight / 100
    false_positive_mass = 0.0
    for observed in range(n + 1):
        rows = draws([['0', observed], ['1', n - observed]], ('x',))
        report = checker.check_joint_rows(rows, spec, resolver, family_alpha=alpha)
        assert report['bound_test_count'] == 4
        if report['status'] != 'passed':
            false_positive_mass += math.comb(n, observed) * p ** observed * (1 - p) ** (n - observed)
    assert false_positive_mass <= alpha


def test_resolver_path_api_with_full_pin(tmp_path):
    spec, resolver = coupled()
    path = tmp_path / 'fixture.csv'
    path.write_bytes(resolver(spec['table']))
    seen = []

    def resolve(pin):
        seen.append(pin)
        return path

    checker.check_joint_rows([{'g': 'A', 'x': '0', 'y': '0'}], spec, resolve)
    assert seen == [spec['table']]


@pytest.mark.parametrize('change', [
    {'extra': True}, {'type': 'unknown'}, {'type_version': True}, {'type_version': 2},
    {'id': ''}, {'id': 'bad id'}, {'given': ['bad field']}, {'outputs': ['é']},
    {'count_column': ' count '}, {'given': 'g'}, {'outputs': []}, {'outputs': ['x', 'x']},
    {'given': ['x']}, {'count_column': 'x'}, {'backoff': [['g']]},
    {'backoff': [['unknown']]}, {'backoff': [[], []]}, {'backoff': [None]},
    {'table': {'path': 'p', 'sha256': 'bogus'}}, {'table': {'path': 'p'}},
    {'table': {'path': 'p\x00', 'sha256': '0' * 64}},
])
def test_strict_spec_rejects_malformed_configuration(change):
    spec, resolver = coupled()
    spec.update(change)
    with pytest.raises(ValueError):
        checker.check_joint_rows([], spec, resolver)


@pytest.mark.parametrize('raw', [
    b'g,x,y,count\nA,0,0,0\n', b'g,x,y,count\nA,0,0,-1\n',
    b'g,x,y,count\nA,0,0,1.0\n', b'g,x,y,count\nA,0,0, 1\n',
    b'g,x,y,count\nA,0,0,\n', b'g,x,y,count\nA,0,0,1\nA,0,0,2\n',
    b'g,x,y,count,count\nA,0,0,1,1\n', b'g,x,count\nA,0,1\n',
    b'g,x,y,count,extra\nA,0,0,1,x\n', b'g,x,y,count\nA,0,1\n',
    b'g,x,y,count\nA,0,0,1,2\n', b'g,x,y,count\n', b'',
    b'g,x,y,count\nA,"unterminated,0,1\n', b'\xff',
])
def test_strict_table_csv_rejects_bad_schema_counts_duplicates_and_encoding(raw):
    spec, _ = coupled()
    spec['table']['sha256'] = hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValueError):
        checker.check_joint_rows([], spec, lambda pin: raw)


@pytest.mark.parametrize('rows', [None, ['A'], [{'g': 'A', 'x': '0'}],
                                  [{'g': 'A', 'x': 0, 'y': '0'}],
                                  [{'g': 'A', 'x': '0', 'y': None}],
                                  [{'g': 'A', 'x': '0', 'y': '0'},
                                   {'g': 'A', 'x': '0', 'y': '0', 'z': ''}]])
def test_malformed_rows_raise_value_error(rows):
    spec, resolver = coupled()
    with pytest.raises(ValueError):
        checker.check_joint_rows(rows, spec, resolver)


@pytest.mark.parametrize('alpha', [0, 1, -1, True, '0.001', math.inf, math.nan, 10 ** 400])
def test_malformed_alpha_rejected(alpha):
    spec, resolver = coupled()
    with pytest.raises(ValueError):
        checker.check_joint_rows([], spec, resolver, family_alpha=alpha)


def broadcast_fixture():
    spec, resolver = fixture([['0', 1], ['1', 1]], given=(), outputs=('sex',))
    before, after = [], []
    # Strongly unequal tumor multiplicity deliberately makes tumor-weighted sex
    # far from 50:50. There are still exactly 100 independent patient draws.
    for i in range(100):
        for j in range(20 if i < 50 else 1):
            base = {'patient': str(i), 'tumor': str(j), 'index': '1' if j == 0 else '0',
                    'sex': 'old', 'keep': 'unchanged'}
            before.append(base)
            after.append({**base, 'sex': str(int(i >= 50))})
    blocks = [block(spec, {'column': 'index', 'values': ['1']},
                    {'key': 'patient', 'fields': ['sex']})]
    return before, after, blocks, resolver


def test_broadcast_copies_do_not_inflate_independent_draw_count():
    before, after, blocks, resolver = broadcast_fixture()
    original = deepcopy((before, after, blocks))
    report = checker.check_blocks(before, after, blocks, resolver, ['patient', 'tumor', 'index'])
    assert report['status'] == 'passed'
    assert report['n'] == 1050
    assert report['blocks'][0]['draw_n'] == report['blocks'][0]['rules'][0]['n'] == 100
    assert (before, after, blocks) == original
    assert checker.check_joint_rows(after, blocks[0]['rules'][0], resolver)['status'] == 'findings'
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize('mutation,code', [
    ('protected', 'protected_field_changed'), ('outside', 'unowned_field_changed'),
    ('broadcast', 'broadcast_inconsistent'), ('missing_anchor', 'broadcast_anchor_count'),
    ('multiple_anchor', 'broadcast_anchor_count'), ('count', 'row_count_changed'),
    ('reorder', 'protected_field_changed'), ('selection', 'selection_changed'),
])
def test_block_integrity_failures_return_findings(mutation, code):
    before, after, blocks, resolver = broadcast_fixture()
    if mutation == 'protected':
        after[0]['tumor'] = 'changed'
    elif mutation == 'outside':
        after[0]['keep'] = 'changed'
    elif mutation == 'broadcast':
        after[1]['sex'] = '1'
    elif mutation == 'missing_anchor':
        before[0]['index'] = after[0]['index'] = '0'
    elif mutation == 'multiple_anchor':
        before[1]['index'] = after[1]['index'] = '1'
    elif mutation == 'count':
        after.pop()
    elif mutation == 'reorder':
        after[0], after[-1] = after[-1], after[0]
    elif mutation == 'selection':
        after[1]['index'] = '1'
    report = checker.check_blocks(before, after, blocks, resolver, ['patient', 'tumor', 'index'])
    assert report['status'] == 'findings' and code in codes(report)
    if mutation in ('broadcast', 'missing_anchor', 'multiple_anchor', 'selection'):
        assert report['blocks'][0]['status'] == 'findings'
    if mutation == 'selection':
        assert report['blocks'][0]['draw_n'] == 100  # Frozen BEFORE selection.


def test_outputs_are_only_mutable_on_selected_rows():
    spec, resolver = fixture([['new', 1]], given=(), outputs=('x',))
    before = [{'id': 'a', 'index': '1', 'x': 'old'}, {'id': 'b', 'index': '0', 'x': 'old'}]
    after = [{**row, 'x': 'new'} for row in before]
    blocks = [block(spec, {'column': 'index', 'values': ['1']})]
    report = checker.check_blocks(before, after, blocks, resolver, ['id', 'index'])
    assert 'unowned_field_changed' in codes(report)


def test_protection_overrides_output_and_broadcast_permissions():
    before, after, blocks, resolver = broadcast_fixture()
    report = checker.check_blocks(before, after, blocks, resolver, ['sex'])
    assert 'protected_field_changed' in codes(report)


def test_blocks_split_alpha_across_all_rule_calls():
    spec, resolver = fixture([['0', 1]], given=(), outputs=('x',))
    second = {**deepcopy(spec), 'id': 'second'}
    rows = [{'x': '0', 'partition': 'A'}, {'x': '0', 'partition': 'B'}]
    a = block(spec, {'column': 'partition', 'values': ['A']}, identifier='a')
    a['rules'].append(second)
    b = block({**deepcopy(spec), 'id': 'third'}, {'column': 'partition', 'values': ['B']}, identifier='b')
    report = checker.check_blocks(rows, rows, [a, b], resolver, ['partition'])
    assert report['status'] == 'passed'
    assert report['rule_call_count'] == 3
    assert report['rule_family_alpha'] == pytest.approx(.001 / 3)
    assert all(r['acceptance']['family_alpha'] == .001 / 3 for b in report['blocks'] for r in b['rules'])


def test_blocks_use_final_conditioning_values_from_earlier_draws():
    sex, sex_resolver = fixture([['0', 1], ['1', 1]], given=(), outputs=('sex',))
    sex['table']['path'] = 'sex.csv'
    joint, joint_resolver = fixture([['0', 'a', 1], ['1', 'b', 1]], given=('sex',), outputs=('leaf',))
    joint['id'] = 'leaf'
    joint['table']['path'] = 'leaf.csv'
    before = [{'id': str(i), 'sex': 'old', 'leaf': 'old'} for i in range(100)]
    after = [{**row, 'sex': str(i % 2), 'leaf': 'a' if i % 2 == 0 else 'b'} for i, row in enumerate(before)]
    resolver = lambda pin: (sex_resolver if pin['path'] == 'sex.csv' else joint_resolver)(pin)
    report = checker.check_blocks(before, after, [block(sex, identifier='sex'), block(joint, identifier='leaf')],
                                  resolver, ['id'])
    assert report['status'] == 'passed'


def hierarchy_fixture():
    spec, resolver = fixture([['new', 1]], given=(), outputs=('leaf',))
    before = [{'id': 'a', 'leaf': 'old', 'parent': 'old-parent'}]
    after = [{'id': 'a', 'leaf': 'new', 'parent': 'new-parent'}]
    hierarchy = {'leaf_column': 'leaf', 'parent_columns': ['parent'],
                 'mapping': {'new': {'parent': 'new-parent'}, 'old': {'parent': 'old-parent'}}}
    return spec, resolver, before, after, hierarchy


def test_generic_hierarchy_allows_only_derived_parents_and_checks_mapping():
    spec, resolver, before, after, hierarchy = hierarchy_fixture()
    assert checker.check_blocks(before, after, [block(spec)], resolver, ['id'], hierarchy)['status'] == 'passed'
    after[0]['parent'] = 'bad'
    report = checker.check_blocks(before, after, [block(spec)], resolver, ['id'], hierarchy)
    assert 'hierarchy_mismatch' in codes(report)
    after[0]['leaf'] = 'unknown'
    assert 'hierarchy_unknown_child' in codes(checker.check_blocks(before, after, [block(spec)], resolver, ['id'], hierarchy))


def test_hierarchy_does_not_allow_rewriting_untouched_rows():
    spec, resolver, before, after, hierarchy = hierarchy_fixture()
    before[0]['select'] = after[0]['select'] = '1'
    before.append({'id': 'b', 'leaf': 'old', 'parent': 'incorrect', 'select': '0'})
    after.append({'id': 'b', 'leaf': 'old', 'parent': 'old-parent', 'select': '0'})
    report = checker.check_blocks(before, after, [block(spec, {'column': 'select', 'values': ['1']})],
                                  resolver, ['id', 'select'], hierarchy)
    assert 'unowned_field_changed' in codes(report)


def test_empty_selected_partitions_and_empty_full_evaluation_fail():
    spec, resolver = fixture([['0', 1]], given=(), outputs=('x',))
    blocks = [block(spec, {'column': 'select', 'values': ['1']})]
    rows = [{'x': '0', 'select': '0'}]
    report = checker.check_blocks(rows, rows, blocks, resolver, ['select'])
    assert 'rule_findings' in codes(report)
    assert 'empty_evaluation' in codes(report['blocks'][0]['rules'][0])
    assert 'empty_evaluation' in codes(checker.check_blocks([], [], blocks, resolver, []))


def test_block_headers_and_order_are_checked():
    spec, resolver = fixture([['0', 1]], given=(), outputs=('x',))
    before = [{'id': 'a', 'x': '0'}]
    after = [{'x': '0', 'id': 'a'}]
    assert 'headers_changed' in codes(checker.check_blocks(before, after, [block(spec)], resolver, ['id']))


@pytest.mark.parametrize('change', [
    {'extra': 1}, {'id': ''}, {'rules': []}, {'select': {'column': 'x', 'values': []}},
    {'select': {'column': 'x', 'values': [1]}}, {'select': {'column': 'x', 'values': ['0', '0']}},
    {'broadcast': {'key': 'id', 'fields': ['other']}}, {'broadcast': {'key': 'x', 'fields': ['x']}},
])
def test_block_contract_is_independently_strict(change):
    spec, resolver = fixture([['0', 1]], given=(), outputs=('x',))
    config = block(spec)
    config.update(change)
    with pytest.raises(ValueError):
        checker.check_blocks([], [], [config], resolver, [])


@pytest.mark.parametrize('protected', [None, 'x', {'x': True}])
def test_invalid_protected_field_container_raises(protected):
    spec, resolver = fixture([['0', 1]], given=(), outputs=('x',))
    with pytest.raises(ValueError, match='protected_fields'):
        checker.check_blocks([], [], [block(spec)], resolver, protected)


def test_duplicate_block_and_global_rule_ids_rejected():
    spec, resolver = fixture([['0', 1]], given=(), outputs=('x',))
    with pytest.raises(ValueError, match='Duplicate block'):
        checker.check_blocks([], [], [block(spec), block(spec)], resolver, [])
    with pytest.raises(ValueError, match='Duplicate rule'):
        checker.check_blocks([], [], [block(spec, identifier='a'), block(spec, identifier='b')], resolver, [])


def test_module_has_no_generator_imports():
    import ast
    tree = ast.parse(MODULE_PATH.read_text())
    roots = {node.module.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    roots.update(alias.name.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names)
    assert roots <= {'collections', 'csv', 'hashlib', 'io', 'itertools', 'math', 'pathlib', 're'}
