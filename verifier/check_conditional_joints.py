"""Independent, fixture-testable verification of conditional joint draws.

No sampler or generator modules are used. ``resolver(pin_dict)`` must return a
filesystem path (or the pinned CSV bytes). Rows are mappings of strings to
strings; blanks and leading zeros are literal codes, never missing values.

Acceptance is fixed before looking at outcomes: exact support plus simultaneous
two-sided Bernstein count bounds at family_alpha. Conditional on the supplied
givens, draws must be independent. Each cell indicator has variance p*(1-p),
including in pooled tests with unequal conditional probabilities. For M tests,
t=log(2*M/alpha), the count bound is t/3+sqrt(2*V*t+t*t/9).
The Bernstein tail and union bound control the family false-positive rate;
neither independence between tests nor large expected cell counts is required.
Determinism is established from integer counts, not floating-point variance.
Positive probabilities and complements that underflow are rejected. Original
given tuples retain separate conditional tests even when they share a fallback.
Deterministic cells have a zero bound. Failure to reject is not a power claim.

Block selections identify independent draws. Broadcast copies are checked for
integrity, never counted again for inference. The caller checks ownership and
final-value stability across rules, CTC keys/metadata, and inherited audits.
"""

from collections import Counter, defaultdict
from collections.abc import Mapping
import csv
import hashlib
import io
from itertools import combinations
import math
from pathlib import Path
import re


def _require(ok, message):
    if not ok:
        raise ValueError(message)


def _fields(value, label, *, empty=False):
    _require(isinstance(value, list), f'{label} must be a list')
    _require(empty or bool(value), f'{label} must not be empty')
    _require(all(isinstance(x, str) and x.strip() for x in value),
             f'{label} requires nonempty column names')
    _require(len(value) == len(set(value)), f'{label} contains duplicate columns')
    return tuple(value)


def _alpha(value):
    _require(type(value) in (int, float) and 0 < value < 1 and math.isfinite(value),
             'family_alpha must be finite and strictly between zero and one')


def _name(value):
    _require(isinstance(value, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]*', value),
             'Rule names and columns must be ASCII identifiers with optional dots/hyphens')


def _spec(spec):
    keys = {'id', 'type', 'type_version', 'given', 'outputs', 'table',
            'count_column', 'backoff'}
    _require(isinstance(spec, Mapping) and set(spec) == keys,
             'conditional joint spec must have exactly the declared keys')
    _name(spec['id'])
    _require(spec['type'] == 'conditional_joint_table' and
             type(spec['type_version']) is int and spec['type_version'] == 1,
             'Unsupported conditional joint type/version')
    given = _fields(spec['given'], 'given', empty=True)
    outputs = _fields(spec['outputs'], 'outputs')
    count = spec['count_column']
    for name in (*given, *outputs, count):
        _name(name)
    _require(len(set((*given, *outputs, count))) == len(given) + len(outputs) + 1,
             'given, outputs and count_column must be disjoint')
    pin = spec['table']
    _require(isinstance(pin, Mapping) and set(pin) == {'path', 'sha256'}, 'Invalid table pin')
    _require(isinstance(pin['path'], str) and pin['path'].strip() and '\x00' not in pin['path'],
             'Invalid table path')
    _require(isinstance(pin['sha256'], str) and
             re.fullmatch(r'[0-9a-fA-F]{64}', pin['sha256']) is not None,
             'Invalid table sha256')
    _require(isinstance(spec['backoff'], list), 'backoff must be an ordered list')
    subsets, seen = [], set()
    for item in spec['backoff']:
        fields = _fields(item, 'backoff subset', empty=True)
        key = frozenset(fields)
        _require(key < set(given), 'backoff must be a proper given subset')
        _require(key not in seen, 'Duplicate backoff subset')
        seen.add(key)
        subsets.append(fields)
    return given, outputs, count, [given, *subsets]


def _table(spec, resolver):
    given, outputs, count, levels = _spec(spec)
    _require(callable(resolver), 'resolver must be callable')
    pin = {'path': spec['table']['path'], 'sha256': spec['table']['sha256'].lower()}
    try:
        resolved = resolver(dict(pin))
        payload = resolved if isinstance(resolved, bytes) else Path(resolved).read_bytes()
    except (OSError, TypeError) as exc:
        raise ValueError('Cannot resolve conditional joint table') from exc
    digest = hashlib.sha256(payload).hexdigest()
    _require(digest == pin['sha256'], 'Conditional joint table hash mismatch')
    try:
        reader = csv.reader(io.StringIO(payload.decode('utf-8'), newline=''), strict=True)
        header = next(reader, [])
        columns = (*given, *outputs, count)
        _require(len(header) == len(columns) and set(header) == set(columns),
                 'CSV must have exactly the declared column set without duplicates')
        indexes = {name: header.index(name) for name in columns}
        tables = {fields: defaultdict(Counter) for fields in levels}
        cells, total, size = set(), 0, 0
        for row in reader:
            _require(len(row) == len(header), 'CSV row width does not match header')
            raw_count = row[indexes[count]]
            _require(re.fullmatch(r'[0-9]+', raw_count) is not None and int(raw_count) > 0,
                     'CSV counts must be positive integers')
            weight = int(raw_count)
            cell = tuple(row[indexes[name]] for name in (*given, *outputs))
            _require(cell not in cells, 'CSV contains duplicate joint cells')
            cells.add(cell)
            output = tuple(row[indexes[name]] for name in outputs)
            for fields in levels:
                condition = tuple(row[indexes[name]] for name in fields)
                tables[fields][condition][output] += weight
            total += weight
            size += 1
    except (UnicodeError, csv.Error) as exc:
        raise ValueError('Invalid UTF-8 CSV table') from exc
    _require(size > 0, 'Conditional joint table has no cells')
    for index in tables.values():
        for weights in index.values():
            denominator = sum(weights.values())
            for weight in weights.values():
                _require(weight / denominator > 0,
                         'Positive table probability is not representable')
                _require(weight == denominator or (denominator - weight) / denominator > 0,
                         'Positive table probability complement is not representable')
    return given, outputs, levels, tables, {
        'path': spec['table']['path'], 'sha256': digest, 'pin_verified': True,
        'columns': header, 'cell_count': size, 'total_count': total,
    }


def _rows(rows, required=()):
    try:
        rows = list(rows)
    except TypeError as exc:
        raise ValueError('Rows must be iterable mappings') from exc
    header = None
    for row in rows:
        _require(isinstance(row, Mapping) and all(isinstance(k, str) and k for k in row),
                 'Rows must be mappings with nonempty string column names')
        _require(all(isinstance(v, str) for v in row.values()), 'Row codes must be strings')
        if header is None:
            header = tuple(row)
        _require(set(row) == set(header), 'Rows have inconsistent headers')
        _require(set(required) <= set(row), 'Row is missing required columns')
    return rows, header


def _cell_test(kind, fields, cell, n, observed, expected, variance, **context):
    return dict(kind=kind, fields=list(fields), cell=list(cell), n=n,
                observed=observed, expected=expected, variance=variance, **context)


def check_joint_rows(rows, spec, resolver, *, family_alpha=0.001):
    """Check one independent draw partition; statistical failures are findings.

    Malformed configuration, table pins/schema/counts and row shapes raise
    ValueError. Empty evaluations, unknown conditions, unsupported outputs and
    frequency failures return status='findings'. No unseen condition is silently
    dropped: only explicitly ordered backoff subsets may supply its target.
    """
    _alpha(family_alpha)
    given, outputs, levels, tables, provenance = _table(spec, resolver)
    rows, _ = _rows(rows, (*given, *outputs))
    groups = defaultdict(Counter)
    for row in rows:
        groups[tuple(row[k] for k in given)][tuple(row[k] for k in outputs)] += 1
    effective, conditions = {}, []
    tests = []
    weighted_tv = 0.0
    unknown, unsupported, backoff_n = 0, 0, 0
    for condition, observed in sorted(groups.items()):
        values = dict(zip(given, condition))
        chosen = None
        for level, fields in enumerate(levels):
            key = tuple(values[f] for f in fields)
            if key in tables[fields]:
                chosen = fields, key, level
                break
        n = sum(observed.values())
        if chosen is None:
            unknown += n
            conditions.append({'given': list(condition), 'n': n, 'status': 'unknown_condition'})
            continue
        fields, key, level = chosen
        stratum = effective.setdefault((fields, key), {
            'weights': tables[fields][key], 'observed': Counter(), 'n': 0,
            'backoff_level': level,
        })
        stratum['observed'].update(observed)
        stratum['n'] += n
        backoff_n += n if level else 0
        bad = sum(v for cell, v in observed.items() if cell not in stratum['weights'])
        unsupported += bad
        weights = stratum['weights']
        total = sum(weights.values())
        tv = 0.5 * math.fsum(abs(observed[cell] / n - weights.get(cell, 0) / total)
                             for cell in weights.keys() | observed.keys())
        weighted_tv += n * tv
        for cell, weight in sorted(weights.items()):
            p, q = weight / total, (total - weight) / total
            tests.append(_cell_test('conditional_joint', outputs, cell, n, observed[cell],
                                   n * p, n * p * q, deterministic=weight == total,
                                   given=list(given), values=list(condition),
                                   effective_given=list(fields), effective_values=list(key)))
        conditions.append({'given': list(condition), 'n': n, 'effective_given': list(fields),
                           'effective_values': list(key), 'backoff_level': level,
                           'unsupported_rows': bad, 'total_variation': tv, 'status': 'evaluated'})

    # These families depend on the reference and givens, never observed outputs.
    projections = {tuple([i]): 'output_marginal' for i in range(len(outputs))}
    projections.update({p: 'output_pair' for p in combinations(range(len(outputs)), 2)})
    projections.setdefault(tuple(range(len(outputs))), 'output_joint')
    pooled = {projection: {} for projection in projections}
    strata = []
    evaluated_n = len(rows) - unknown
    for (fields, key), stratum in sorted(effective.items()):
        weights, obs, n = stratum['weights'], stratum['observed'], stratum['n']
        total = sum(weights.values())
        tv = 0.5 * math.fsum(abs(obs[cell] / n - weights.get(cell, 0) / total)
                             for cell in weights.keys() | obs.keys())
        sparse_cells = sum(n * weight / total < 5 for weight in weights.values())
        strata.append({'given': list(fields), 'values': list(key), 'n': n,
                       'reference_count': total, 'support_cells': len(weights),
                       'expected_cells_below_5': sparse_cells, 'total_variation': tv,
                       'backoff_level': stratum['backoff_level']})
        for projection, aggregate in pooled.items():
            projected_weights, projected_obs = Counter(), Counter()
            for cell, weight in weights.items():
                projected_weights[tuple(cell[i] for i in projection)] += weight
            for cell, observed in obs.items():
                projected_obs[tuple(cell[i] for i in projection)] += observed
            for cell, weight in projected_weights.items():
                entry = aggregate.setdefault(cell, {'expected': [], 'variance': [], 'observed': 0,
                                                     'deterministic': True})
                p, q = weight / total, (total - weight) / total
                entry['expected'].append(n * p)
                entry['variance'].append(n * p * q)
                entry['deterministic'] &= weight == total
            # Include observations from every effective stratum, even when a
            # projected cell has zero probability in that particular stratum.
            for cell, observed in projected_obs.items():
                entry = aggregate.setdefault(cell, {'expected': [], 'variance': [], 'observed': 0,
                                                     'deterministic': True})
                entry['observed'] += observed
    for projection, aggregate in pooled.items():
        for cell, entry in sorted(aggregate.items()):
            # Out-of-support observations are already an exact finding, and must
            # not increase M (which would change thresholds after seeing data).
            if not entry['expected']:
                continue
            tests.append(_cell_test(projections[projection], [outputs[i] for i in projection],
                                   cell, evaluated_n, entry['observed'],
                                   math.fsum(entry['expected']), math.fsum(entry['variance']),
                                   deterministic=entry['deterministic']))

    test_count = len(tests)
    t = math.log(2) + math.log(test_count) - math.log(family_alpha) if test_count else None
    by_kind = defaultdict(lambda: {'test_count': 0, 'violations': 0})
    for test in tests:
        variance = test['variance']
        bound = 0.0 if test['deterministic'] else t / 3 + math.sqrt(2 * variance * t + t * t / 9)
        deviation = abs(test['observed'] - test['expected'])
        test.update(bound=bound, absolute_deviation=deviation,
                    signed_deviation=test['observed'] - test['expected'],
                    exceeds_bound=deviation > bound,
                    bound_ratio=deviation / bound if bound else None)
        by_kind[test['kind']]['test_count'] += 1
        by_kind[test['kind']]['violations'] += int(test['exceeds_bound'])
    violations = sum(item['exceeds_bound'] for item in tests)
    findings = []
    if not rows:
        findings.append({'code': 'empty_evaluation', 'rows': 0})
    if unknown:
        findings.append({'code': 'unknown_conditions', 'rows': unknown})
    if unsupported:
        findings.append({'code': 'unsupported_outputs', 'rows': unsupported})
    if violations:
        findings.append({'code': 'frequency_bounds', 'violating_tests': violations})
    sparse = [s for s in strata if s['expected_cells_below_5']]
    sparse_n = sum(s['n'] for s in sparse)
    return {
        'schema_version': 1, 'id': spec['id'], 'status': 'findings' if findings else 'passed',
        'findings': findings, 'table': provenance, 'n': len(rows), 'evaluated_n': evaluated_n,
        'unknown_condition_rows': unknown, 'unsupported_output_rows': unsupported,
        'given': list(given), 'outputs': list(outputs), 'conditions': conditions,
        'effective_strata': strata, 'backoff_rows': backoff_n,
        'backoff_rowmass': backoff_n / len(rows) if rows else None,
        'conditional_weighted_total_variation': weighted_tv / evaluated_n if evaluated_n else None,
        'sparse_condition_coverage': {
            'expected_count_threshold': 5, 'effective_strata': len(strata),
            'sparse_strata': len(sparse), 'sparse_rows': sparse_n,
            'sparse_row_fraction': sparse_n / evaluated_n if evaluated_n else None,
            'singleton_strata': sum(s['n'] == 1 for s in strata),
            'expected_cells_below_5': sum(s['expected_cells_below_5'] for s in strata),
            'reference_primary_conditions': len(tables[given]),
            'observed_primary_conditions': sum(c.get('backoff_level') == 0 for c in conditions),
        },
        'acceptance': {
            'method': 'simultaneous_two_sided_bernstein', 'family_alpha': family_alpha,
            'test_count': test_count, 'tail_log_factor': t,
            'count_bound': 't/3 + sqrt(2*variance*t + t*t/9); zero for deterministic cells',
            'expected_counts': 'sum over independent draws of conditional probabilities',
            'variance': 'sum over independent draws of p*(1-p)',
            'conditional_test_unit': 'original given tuple against its selected primary/backoff distribution',
            'conditional_tv_unit': 'original given tuple, weighted by its evaluated row count',
            'determinism': 'each contributing integer cell count is zero or its stratum total',
            'decision': 'all support checks and all count bounds must pass; empty fails',
            'tv_is_descriptive': True, 'assumption': 'independent draws conditional on givens',
            'projections': [[outputs[i] for i in p] for p in projections],
        },
        'bound_test_count': test_count, 'violating_test_count': violations,
        'test_families': dict(by_kind),
        'worst_deviations': sorted(tests, key=lambda x: (
            x['exceeds_bound'], x['absolute_deviation'] - x['bound'], x['absolute_deviation']),
            reverse=True)[:20],
    }


def _block(block):
    _require(isinstance(block, Mapping) and set(block) == {'id', 'select', 'broadcast', 'rules'},
             'Block must have exactly id, select, broadcast, rules')
    _require(isinstance(block['id'], str) and block['id'].strip(), 'Invalid block id')
    _require(isinstance(block['rules'], list) and bool(block['rules']), 'Block rules must not be empty')
    for rule in block['rules']:
        _spec(rule)
    _require(len({r['id'] for r in block['rules']}) == len(block['rules']), 'Duplicate rule id in block')
    select = block['select']
    if select is not None:
        _require(isinstance(select, Mapping) and set(select) == {'column', 'values'}, 'Invalid select')
        _fields([select['column']], 'select column')
        values = select['values']
        _require(isinstance(values, list) and bool(values) and all(isinstance(v, str) for v in values),
                 'Select values must be a nonempty list of string codes')
        _require(len(values) == len(set(values)), 'Duplicate select value')
    broadcast = block['broadcast']
    if broadcast is not None:
        _require(isinstance(broadcast, Mapping) and set(broadcast) == {'key', 'fields'}, 'Invalid broadcast')
        _fields([broadcast['key']], 'broadcast key')
        fields = _fields(broadcast['fields'], 'broadcast fields')
        _require(broadcast['key'] not in fields, 'Broadcast key cannot be a broadcast field')
        outputs = {f for r in block['rules'] for f in r['outputs']}
        _require(set(fields) <= outputs, 'Broadcast fields must be block rule outputs')


def _hierarchy(hierarchy):
    if hierarchy is None:
        return None, (), {}
    _require(isinstance(hierarchy, Mapping) and
             set(hierarchy) == {'leaf_column', 'parent_columns', 'mapping'},
             'Hierarchy requires leaf_column, parent_columns, mapping')
    child = hierarchy['leaf_column']
    _fields([child], 'hierarchy child')
    parents = _fields(hierarchy['parent_columns'], 'hierarchy parents')
    _require(child not in parents, 'Hierarchy child overlaps parents')
    mapping = hierarchy['mapping']
    _require(isinstance(mapping, Mapping) and bool(mapping), 'Empty hierarchy mapping')
    for code, values in mapping.items():
        _require(isinstance(code, str) and isinstance(values, Mapping) and set(values) == set(parents)
                 and all(isinstance(v, str) for v in values.values()), 'Invalid hierarchy mapping cell')
    return child, parents, mapping


def check_blocks(before_rows, after_rows, blocks, resolver, protected_fields,
                 hierarchy=None, family_alpha=0.001):
    """Verify final rows against block draws and exact mutation boundaries.

    ``hierarchy`` is None or {leaf_column, parent_columns, mapping}, where the
    mapping is child code -> {parent field: code}. Identity fields supplied by
    the caller in protected_fields are compared at the same row position. No
    source-specific key is inferred or claimed to be validated.
    """
    _alpha(family_alpha)
    _require(isinstance(blocks, list) and bool(blocks), 'blocks must be a nonempty list')
    for block in blocks:
        _block(block)
    _require(len({b['id'] for b in blocks}) == len(blocks), 'Duplicate block id')
    rule_ids = [r['id'] for b in blocks for r in b['rules']]
    _require(len(rule_ids) == len(set(rule_ids)), 'Duplicate rule id across blocks')
    _require(not isinstance(protected_fields, (str, bytes, Mapping)),
             'protected_fields must be an iterable of column names')
    try:
        protected = _fields(list(protected_fields), 'protected_fields', empty=True)
    except TypeError as exc:
        raise ValueError('protected_fields must be an iterable of column names') from exc
    child, parents, mapping = _hierarchy(hierarchy)
    required = set(protected) | set(parents) | ({child} if child else set())
    for block in blocks:
        if block['select']:
            required.add(block['select']['column'])
        if block['broadcast']:
            required.add(block['broadcast']['key'])
        for rule in block['rules']:
            required.update((*rule['given'], *rule['outputs']))
    before, before_header = _rows(before_rows, required)
    after, after_header = _rows(after_rows, required)
    findings, counts, examples = [], Counter(), []

    def finding(code, **details):
        counts[code] += 1
        if len(examples) < 20:
            examples.append({'code': code, **details})

    if len(before) != len(after):
        finding('row_count_changed', before=len(before), after=len(after))
    if before_header is not None and after_header is not None and before_header != after_header:
        finding('headers_changed')
    if not before or not after:
        finding('empty_evaluation')
    allowed = [set() for _ in after]
    calls = sum(len(block['rules']) for block in blocks)
    rule_alpha = family_alpha / calls
    _alpha(rule_alpha)
    reports = []
    for block in blocks:
        counts_before_block = counts.copy()
        select, broadcast = block['select'], block['broadcast']

        def selected(row):
            return select is None or row[select['column']] in select['values']

        # Selection is frozen on the original rows; conditioning uses final
        # draw values, whose ownership/stability the caller verifies separately.
        indices = [i for i, row in enumerate(before) if selected(row) and i < len(after)]
        for i, (old, new) in enumerate(zip(before, after)):
            if selected(old) != selected(new):
                finding('selection_changed', block=block['id'], row=i)
        outputs = {f for r in block['rules'] for f in r['outputs']}
        for i in indices:
            allowed[i].update(outputs)
        if broadcast:
            key, fields = broadcast['key'], broadcast['fields']
            members, anchors = defaultdict(list), defaultdict(list)
            for i, row in enumerate(after):
                members[row[key]].append(i)
            for i in indices:
                anchors[after[i][key]].append(i)
            for i, (old, new) in enumerate(zip(before, after)):
                if old[key] != new[key]:
                    finding('broadcast_key_changed', block=block['id'], row=i)
            for patient, positions in members.items():
                anchor = anchors[patient]
                if len(anchor) != 1:
                    finding('broadcast_anchor_count', block=block['id'], key=patient, anchors=len(anchor))
                    continue
                source = after[anchor[0]]
                for i in positions:
                    allowed[i].update(fields)
                    for field in fields:
                        if after[i][field] != source[field]:
                            finding('broadcast_inconsistent', block=block['id'], row=i, field=field)
        draw_rows = [after[i] for i in indices]
        rules = [check_joint_rows(draw_rows, rule, resolver, family_alpha=rule_alpha)
                 for rule in block['rules']]
        block_findings = [{'code': code, 'count': n} for code, n in
                          sorted((counts - counts_before_block).items())]
        if any(r['status'] != 'passed' for r in rules):
            block_findings.append({'code': 'rule_findings'})
        reports.append({'id': block['id'], 'draw_n': len(draw_rows), 'rules': rules,
                        'findings': block_findings,
                        'status': 'findings' if block_findings else 'passed'})

    for i, row in enumerate(after):
        if child:
            if child in allowed[i]:
                allowed[i].update(parents)
            target = mapping.get(row[child])
            if target is None:
                finding('hierarchy_unknown_child', row=i, child=row[child])
            else:
                for field in parents:
                    if row[field] != target[field]:
                        finding('hierarchy_mismatch', row=i, field=field)
        if i < len(before):
            for field in before[i].keys() & row.keys():
                if before[i][field] != row[field]:
                    if field in protected:
                        finding('protected_field_changed', row=i, field=field)
                    elif field not in allowed[i]:
                        finding('unowned_field_changed', row=i, field=field)
    findings.extend({'code': code, 'count': n} for code, n in sorted(counts.items()))
    if any(r['status'] != 'passed' for b in reports for r in b['rules']):
        findings.append({'code': 'rule_findings'})
    return {'schema_version': 1, 'status': 'findings' if findings else 'passed',
            'n': len(after), 'before_n': len(before), 'findings': findings,
            'integrity_examples': examples, 'blocks': reports,
            'family_alpha': family_alpha, 'rule_family_alpha': rule_alpha,
            'rule_call_count': calls, 'protected_fields': list(protected),
            'row_identity_policy': 'position-wise protected and unowned fields; caller verifies keys',
            'full_inherited_audit': False}
