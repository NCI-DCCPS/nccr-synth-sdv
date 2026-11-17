"""Independent scoped PHARM assessment. Does not import the generator."""
from collections import Counter, defaultdict
import csv
from fractions import Fraction
import hashlib
import io
import json
import math
from pathlib import Path
import re
import zipfile

CANMED = ['canmedNonProprietaryName', 'canmedProprietaryName', 'canmedDrugCategory',
          'canmedMajorDrugClass', 'canmedMinorDrugClass']
RXNORM = ['rxnormRxcui', 'rxnormConceptName', 'rxnormDisplayName', 'rxnormStrength',
          'rxnormDoseForm', 'rxnormAtcprodClass']
NO_CANMED = '7-No CanMED Data Available'
NOT_SOUGHT = {k: '7777777' if k == 'rxnormRxcui' else '7-Not Sought in RxNorm' for k in RXNORM}
MISSING = {k: '9999999' if k == 'rxnormRxcui' else '9-Missing' for k in RXNORM}
PROTECTED = ['nccrId', 'medicationDispenseQuantity', 'medicationDispenseDuration', 'monthsFromIndexDxtoDispense']
RULE_SHA = '16b9ab2e2f605eddba267662b6dc74ef60fb2749cafd8b8b616283c0905f042c'


def require(ok, message):
    if not ok:
        raise ValueError(message)


def sha(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def check_drug_row(row, canmed, all_canmed, rxnorm):
    ndc = row['medicationRequestNdc11']
    if ndc == 'UNK':
        require(row['medicationRequestNdc9'] == 'UNK' and all(row[k] == NO_CANMED for k in CANMED)
                and all(row[k] == v for k, v in MISSING.items()), 'Inconsistent unresolved missing state')
        return 'unresolved'
    require(re.fullmatch('[0-9]{11}', ndc) and row['medicationRequestNdc9'] == ndc[:9], 'Invalid NDC representation')
    if ndc in canmed:
        require(all(row[k] == v for k, v in canmed[ndc].items()), 'CanMED tuple mismatch')
        require(all(row[k] == v for k, v in NOT_SOUGHT.items()), 'CanMED row has incorrect RxNorm not-sought state')
        return 'canmed_matched'
    require(ndc not in all_canmed, 'CanMED excluded source key used')
    require(ndc in rxnorm, 'No authoritative RxNorm evidence for NDC')
    require(all(row[k] == NO_CANMED for k in CANMED), 'RxNorm row has invented CanMED data')
    require(all(row[k] == v for k, v in rxnorm[ndc].items()), 'RxNorm tuple mismatch')
    return 'rxnorm_matched'


def read_zip(path, source):
    with zipfile.ZipFile(path) as z:
        require(len(z.namelist()) == 2 and set(z.namelist()) == {source + '.csv', source.lower() + '_metadata.json'}, 'Unexpected ZIP members')
        require(z.testzip() is None, 'ZIP CRC error')
        reader = csv.DictReader(io.StringIO(z.read(source + '.csv').decode()))
        return reader.fieldnames, list(reader), z.read(source.lower() + '_metadata.json')


def counts(public, field):
    return {r['value']: r['count'] for r in public['fields'][field]['charts'][0]['values']}


def expected_counts(weights, n):
    denominator = sum(weights.values())
    quotients = {k: divmod(v * n, denominator) for k, v in weights.items()}
    result = {k: pair[0] for k, pair in quotients.items()}
    remainder = n - sum(result.values())
    for k in sorted(weights, key=lambda k: (-quotients[k][1], k))[:remainder]:
        result[k] += 1
    return result


def month(value):
    y, m = map(int, value[:7].split('-'))
    return y * 12 + m - 1


def verify(record):
    for item in record['inputs'].values():
        require(sha(item['path']) == item['sha256'], 'Input hash mismatch: ' + item['path'])
    require(record['inputs']['rules']['sha256'] == RULE_SHA, 'Unsupported rules bundle')
    rules = json.loads(Path(record['inputs']['rules']['path']).read_bytes())
    lookup_pin = rules['lookups']['canmed_ndc_2026_11_12']
    frequency_pin = rules['frequency_references']['pharm_public_2026_11_09']
    for key, expected in [('canmed_manifest', lookup_pin['manifest']['sha256']),
                          ('canmed', lookup_pin['normalized_sha256']),
                          ('public', frequency_pin['normalized_json']['sha256']),
                          ('public_report', frequency_pin['published_json']['sha256'])]:
        require(record['inputs'][key]['sha256'] == expected, 'Rule input pin mismatch: ' + key)
    rx_manifest = json.loads(Path(record['inputs']['rxnorm_manifest']['path']).read_bytes())
    require(record['inputs']['rxnorm']['sha256'] == rx_manifest['normalized']['sha256'], 'RxNorm manifest binding mismatch')
    public = json.loads(Path(record['inputs']['public']['path']).read_bytes())
    canmed_lookup = json.loads(Path(record['inputs']['canmed']['path']).read_bytes())
    rx_lookup = json.loads(Path(record['inputs']['rxnorm']['path']).read_bytes())
    plan = json.loads(Path(record['inputs']['plan']['path']).read_bytes())
    canmed = {r['fields']['medicationRequestNdc11']: r['fields'] for r in canmed_lookup['records']}
    canmed_attributes = {r['fields']['medicationRequestNdc11']: r['source_attributes'] for r in canmed_lookup['records']}
    all_canmed = set(canmed) | {r['ndc11'] for r in canmed_lookup['excluded_records']}
    rxnorm = {r['fields']['medicationRequestNdc11']: r['fields'] for r in rx_lookup['records']}
    require(len(canmed) == len(canmed_lookup['records']) and len(rxnorm) == len(rx_lookup['records']), 'Duplicate lookup key')
    baseline = {f['source']: f for f in record['baseline_files']}
    files = {f['source']: f for f in record['files']}
    require(set(files) == set(baseline) == {'CTC', 'ABM', 'COG', 'MCE', 'MCD', 'MCP', 'PHARM', 'RO', 'CCDI', 'CPI'}, 'Incomplete source inventory')
    for inventory in [files, baseline]:
        for source, f in inventory.items():
            require(sha(f['local_path']) == f['sha256'], 'Payload hash mismatch: ' + source)
    require(all(files[s]['sha256'] == baseline[s]['sha256'] for s in files if s != 'PHARM'), 'Other source changed')
    old_header, old, old_metadata = read_zip(record['inputs']['baseline_pharm']['path'], 'PHARM')
    header, rows, metadata = read_zip(files['PHARM']['local_path'], 'PHARM')
    require(header == old_header and metadata == old_metadata, 'PHARM schema or metadata changed')
    require(len(rows) == len(old) == plan['rows'], 'Row count changed')
    require(all({k: a[k] for k in PROTECTED} == {k: b[k] for k in PROTECTED} for a, b in zip(old, rows)), 'Protected PHARM row values changed')
    require(all(None not in row and all(v is not None and v != '' for v in row.values()) for row in rows), 'Blank or malformed PHARM row')
    _, ctc, _ = read_zip(files['CTC']['local_path'], 'CTC')
    _, mce, _ = read_zip(files['MCE']['local_path'], 'MCE')
    patients = {r['nccrId'] for r in ctc}
    enrolled = {r['nccrId'] for r in mce if r['reportedInsuranceEnrollment'] == '1'}
    contexts = {r['nccrId']: r for r in csv.DictReader(Path(record['inputs']['patient_context']['path']).open())}
    require(set(contexts) == patients, 'Context differs from CTC universe')
    ids = {r['nccrId'] for r in rows}
    require(ids <= patients and ids <= enrolled, 'PHARM orphan or unconfirmed MCE patient')
    temporal = Counter(); states = Counter(); groups = Counter(); packages = defaultdict(Counter)
    observed = {k: Counter() for k in header}
    for row in rows:
        state = check_drug_row(row, canmed, all_canmed, rxnorm)
        states[state] += 1
        key = (state, row['canmedDrugCategory'], row['canmedNonProprietaryName'].casefold()) if state == 'canmed_matched' else (state, row['rxnormDisplayName'] if state == 'rxnorm_matched' else 'UNK')
        groups[key] += 1; packages[key][row['medicationRequestNdc11']] += 1
        for k, v in row.items(): observed[k][v] += 1
        context = contexts[row['nccrId']]
        if row['monthsFromIndexDxtoDispense'] == '9999':
            temporal['unknown_dispensing_month'] += 1
            continue
        actual = month(context['synthetic_index_month']) + int(row['monthsFromIndexDxtoDispense'])
        low, high = month(context['synthetic_birth_month']), month(context['coverage_end_month'])
        if context['deceased'] == 'true':
            high = min(high, month(context['synthetic_index_month']) + int(context['death_offset_months']))
        require(low <= actual <= high, 'Dispensing outside birth/coverage/death window')
        temporal['valid_patient_timeline'] += 1
        if state == 'canmed_matched':
            attrs = canmed_attributes[row['medicationRequestNdc11']]
            start, end = attrs['package_effective_date'], attrs['package_discontinuation_date']
            if not start: temporal['canmed_unknown_effective_date'] += 1
            if not end: temporal['canmed_unknown_discontinuation_date'] += 1
            if start and actual < month(start): temporal['canmed_dispense_before_package_effective_month'] += 1
            if end and actual > month(end): temporal['canmed_dispense_after_package_discontinuation_month'] += 1
    n = len(rows)
    categories = expected_counts(counts(public, 'canmedDrugCategory'), n)
    require(dict(observed['canmedDrugCategory']) == categories, 'Public category allocation mismatch')
    missing = round(Fraction(counts(public, 'rxnormDisplayName')['Missing'] * n, public['total_records']))
    require(states['unresolved'] == missing, 'Published missingness allocation mismatch')
    expected = {}
    names = {k.casefold(): v for k, v in counts(public, 'canmedNonProprietaryName').items() if not k.startswith('Not found')}
    for category, size in categories.items():
        if category == NO_CANMED: continue
        all_names = {f['canmedNonProprietaryName'].casefold() for f in canmed.values() if f['canmedDrugCategory'] == category}
        supported = all_names & names.keys()
        if not supported:
            require(plan['frequency_evidence']['category_only_name_allocations'].get(category) == sorted(all_names), 'Undeclared unreported name allocation')
        weights = {name: names[name] for name in supported} if supported else dict.fromkeys(all_names, 1)
        for name, k in expected_counts(weights, size).items():
            expected[('canmed_matched', category, name)] = k
    rx_public = counts(public, 'rxnormDisplayName')
    supported_rx = {f['rxnormDisplayName'] for f in rxnorm.values()}
    evidence = plan['frequency_evidence']
    require(evidence['rxnorm_unreported_count'] == public['total_records'] - sum(rx_public.values()), 'Unreported RxNorm mass not disclosed')
    require(evidence['rxnorm_unsupported_names'] == {k: v for k, v in rx_public.items() if k != 'Missing' and not k.startswith('Not sought') and k not in supported_rx}, 'Unsupported RxNorm mass not disclosed')
    generic_public = counts(public, 'canmedNonProprietaryName')
    supported_canmed = {f['canmedNonProprietaryName'].casefold() for f in canmed.values()}
    require(evidence['canmed_unreported_count'] == public['total_records'] - sum(generic_public.values()), 'Unreported CanMED mass not disclosed')
    require(evidence['canmed_unsupported_names'] == {k: v for k, v in generic_public.items() if not k.startswith('Not found') and k.casefold() not in supported_canmed}, 'Unsupported CanMED mass not disclosed')
    for name, k in expected_counts({name: rx_public[name] for name in supported_rx}, categories[NO_CANMED] - missing).items():
        expected[('rxnorm_matched', name)] = k
    expected[('unresolved', 'UNK')] = missing
    require(groups == Counter(expected), 'Effective published-name allocations differ')
    package_tests = []
    for group in plan['groups']:
        key = (group['state'], group['category'], group['name']) if group['state'] == 'canmed_matched' else (group['state'], group['name'])
        require(group['count'] == expected[key], 'Plan differs from independently derived count')
        options = {f['medicationRequestNdc11'] for f in group['tuples']}
        if group['state'] == 'canmed_matched':
            valid = {ndc for ndc, f in canmed.items() if f['canmedDrugCategory'] == group['category'] and f['canmedNonProprietaryName'].casefold() == group['name']}
        elif group['state'] == 'rxnorm_matched':
            valid = {ndc for ndc, f in rxnorm.items() if f['rxnormDisplayName'] == group['name']}
        else: valid = {'UNK'}
        require(options == valid and len(options) == len(group['tuples']), 'Altered package allocation support')
        count = expected[key]
        if count:
            bound = math.sqrt(math.log(2 * (len(canmed) + len(rxnorm) + 1) / .001) / (2 * count))
            deviation = max(abs(packages[key][ndc] / count - 1 / len(options)) for ndc in options)
            require(deviation <= bound, 'Uniform package allocation statistical check failed')
            package_tests.append({'group': list(key), 'n': count, 'package_options': len(options), 'maximum_deviation': deviation, 'bound': bound})
    deviations = []
    for field in [*CANMED, *RXNORM]:
        if not public['fields'].get(field, {}).get('charts'): continue
        published = counts(public, field)
        def label(value):
            if value == NO_CANMED or value.startswith('Not found in CanMED'): return NO_CANMED.casefold()
            if value == '7-Not Sought in RxNorm' or value.startswith('Not sought in RxNorm'): return 'not sought'
            if value in ['9-Missing', 'Missing']: return 'missing'
            return value.casefold()
        a, p = Counter(), Counter()
        for k, v in observed[field].items(): a[label(k)] += v
        for k, v in published.items(): p[label(k)] += v
        changes = [{'label': k, 'actual_count': a[k], 'public_count': p[k],
                    'actual_fraction': a[k] / n, 'public_fraction': p[k] / public['total_records'],
                    'difference': a[k] / n - p[k] / public['total_records']} for k in a.keys() | p.keys()]
        deviations.append({'field': field, 'published_count': sum(p.values()), 'unreported_count': public['total_records'] - sum(p.values()),
                           'labels_unreported_have_unknown_frequency': True,
                           'comparisons': sorted(changes, key=lambda r: -abs(r['difference']))})
    return {'status': 'passed_scoped_pharm_augmentation', 'rows': n, 'patients': len(ids),
            'states': dict(states), 'other_sources_unchanged': 9,
            'preserved_columns_in_original_row_order': PROTECTED, 'metadata_identical': True,
            'new_rule_results': [{'id': x, 'status': 'passed'} for x in ['pharm_canmed_ndc_tuple_is_consistent', 'pharm_drug_reference_evidence_and_missingness', 'pharm_sampling_targets_remain_published_json']],
            'effective_group_counts_exact': True, 'uniform_package_checks': package_tests,
            'temporal_checks': dict(temporal), 'frequency_deviations': deviations,
            'coverage': {k: {'blank_cells': c[''], 'distinct_values': len(c)} for k, c in observed.items()},
            'limits': ['Scoped augmentation assessment; not a new all-source release qualification.',
                       'Package dates are informational under the declared identity-only temporal policy.',
                       'Full cloud ETL and clinical drug appropriateness not evaluated.']}
