import copy
from collections import Counter
from pathlib import Path
import subprocess
import sys

import pytest

from jamboree_sdv.pharm_enrichment import allocate, build_plan, enrich_rows


def inputs():
    def field(values):
        return {'charts': [{'values': [{'value': k, 'count': v} for k, v in values.items()]}]}
    public = {'total_records': 100, 'fields': {
        'canmedDrugCategory': field({'CHEMOTHERAPY': 20, '7-No CanMED Data Available': 80}),
        'canmedNonProprietaryName': field({'A': 10, 'B': 10, 'Not found in CanMED; see RxNorm fields': 80}),
        'rxnormDisplayName': field({'R (Oral Pill)': 30, 'Missing': 10,
                                   'Not sought in RxNorm because available in CanMED; see CanMED fields': 20})}}
    def record(ndc, name):
        return {'fields': {'medicationRequestNdc11': ndc, 'medicationRequestNdc9': ndc[:9],
                'canmedNonProprietaryName': name, 'canmedProprietaryName': name,
                'canmedDrugCategory': 'CHEMOTHERAPY', 'canmedMajorDrugClass': 'C',
                'canmedMinorDrugClass': '9-Missing'}}
    canmed = {'records': [record('00000000101', 'A'), record('00000000102', 'A'),
                          record('00000000201', 'B')], 'excluded_records': []}
    rx = {'records': [{'fields': {'medicationRequestNdc11': '00000000301',
        'medicationRequestNdc9': '000000003', 'rxnormRxcui': '123', 'rxnormConceptName': 'R 1 MG',
        'rxnormDisplayName': 'R (Oral Pill)', 'rxnormStrength': '1 mg',
        'rxnormDoseForm': 'Oral Tablet', 'rxnormAtcprodClass': 'C'}}]}
    return public, canmed, rx


def test_allocation_keeps_total_and_does_not_treat_package_count_as_usage():
    assert sum(allocate({'a': 1, 'b': 3}, 7).values()) == 7
    plan = build_plan(*inputs(), 100)
    rows = [{'nccrId': f'P{i}', 'medicationDispenseQuantity': '2',
             'medicationDispenseDuration': '30', 'monthsFromIndexDxtoDispense': str(i)} for i in range(100)]
    result = enrich_rows(rows, plan, 42)
    assert result == enrich_rows(rows, plan, 42)
    assert all(all(new[k] == v for k, v in old.items()) for new, old in zip(result, rows))
    assert Counter(r['canmedNonProprietaryName'] for r in result) == {
        'A': 10, 'B': 10, '7-No CanMED Data Available': 80}
    assert sum(r['rxnormRxcui'] == '7777777' for r in result) == 20
    assert sum(r['rxnormRxcui'] == '9999999' for r in result) == 10
    assert sum(r['rxnormRxcui'] == '123' for r in result) == 70
    assert plan['frequency_evidence']['rxnorm_unreported_count'] == 40


def test_overlap_with_even_excluded_canmed_key_is_rejected():
    public, canmed, rx = inputs()
    canmed['excluded_records'].append({'ndc11': '00000000301'})
    with pytest.raises(ValueError, match='CanMED'):
        build_plan(public, canmed, rx, 100)


def test_unsupported_category_and_duplicate_ndc_are_rejected():
    public, canmed, rx = inputs()
    bad = copy.deepcopy(canmed)
    bad['records'] = bad['records'][:1] * 2
    with pytest.raises(ValueError, match='duplicate'):
        build_plan(public, bad, rx, 100)
    canmed['records'] = []
    with pytest.raises(ValueError, match='support'):
        build_plan(public, canmed, rx, 100)


def test_generation_rejects_row_count_drift_and_existing_values():
    plan = build_plan(*inputs(), 100)
    with pytest.raises(ValueError, match='row count'):
        enrich_rows([], plan, 42)
    with pytest.raises(ValueError, match='populated'):
        enrich_rows([{'canmedDrugCategory': 'existing'}] * 100, plan, 42)


def test_category_with_only_unreported_names_has_explicit_fallback():
    public, canmed, rx = inputs()
    for record in canmed['records']:
        record['fields']['canmedNonProprietaryName'] = 'unreported combination'
    plan = build_plan(public, canmed, rx, 100)
    assert plan['frequency_evidence']['category_only_name_allocations'] == {'CHEMOTHERAPY': ['unreported combination']}
    group = next(g for g in plan['groups'] if g['state'] == 'canmed_matched')
    assert group['count'] == 20 and group['public_weight'] is None


def test_archived_enrichment_module_loads_without_live_package(tmp_path):
    source = Path(__file__).parents[1] / 'src/jamboree_sdv/pharm_enrichment.py'
    archived = tmp_path / 'pharm_enrichment.py'
    archived.write_bytes(source.read_bytes())
    result = subprocess.run([sys.executable, '-I', str(archived)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
