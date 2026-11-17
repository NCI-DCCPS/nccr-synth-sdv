"""Patient-level multiplicity, exact totals, and coordinated tumor timelines."""
from collections import Counter
from types import SimpleNamespace

import numpy as np
import pytest

from jamboree_sdv.common import EXPERIMENT, WORKSPACE, read_json, write_json, pin, local_verifiers
from jamboree_sdv.context import make_context, month
from jamboree_sdv.population import Population

REFERENCE = WORKSPACE/'reference/nccr-derived/2026-11-13-stage-test-v30-iccc-age-race-v1'


def histogram():
    return {int(k): v for k, v in read_json(REFERENCE/'analysis/quality.json')['tumor_rows_per_patient'].items()}


def test_population_uses_patient_histogram_without_two_tumor_cap():
    pop = Population(4, 51, 42, multiplicity={1: 1, 3: 1, 10: 1, 37: 1})
    assert Counter(np.bincount(pop.tumor_patient)) == {1: 1, 3: 1, 10: 1, 37: 1}
    assert len(pop.tumor_patient) == 51
    assert set(pop.tumor_numbers) == {f'{i:02d}' for i in range(1, 38)}
    again = Population(4, 51, 42, multiplicity={37: 1, 10: 1, 3: 1, 1: 1})
    assert np.array_equal(pop.tumor_patient, again.tumor_patient)


@pytest.mark.parametrize('count', [3, 10, 37])
def test_later_tumors_have_distinct_feasible_dates(count):
    class Generator:
        recipe = SimpleNamespace(spec={'seed': 42}, profiles={'model': {
            'tumor_allocation': 'pinned_patient_histogram_exact_totals'}})
        population = SimpleNamespace(ids=['SDV0000000001'], tumor_patient=np.zeros(count, dtype=int))

        def draw(self, field, size, predicate=None):
            options = {'CTC.yearOfDiagnosis': ['2022', '2021', '2020', '2019'],
                       'CTC.ageRecodeSingle90p': ['10'], 'CTC.vitalStatusRecode': ['0'],
                       'CTC.behaviorICDO3Public': ['3']}.get(field, ['9999'])
            options = [v for v in options if predicate is None or predicate(v)]
            return np.array([options[0]] * size)

    patients, tumors, groups = make_context(Generator())
    dates = [month(t['diagnosis_month']) for t in tumors]
    assert dates == sorted(set(dates))
    assert len(dates) == count and dates[-1] <= month('2022-12')
    patient = patients[0]
    assert month(patient['active_followup_end_month']) >= dates[-1]
    assert month(patient['synthetic_index_month']) + patient['death_offset_months'] >= dates[-1]
    for i, t in enumerate(tumors):
        assert t['number_of_primaries'] == count and t['tumor_order'] == i + 1
        assert t['months_from_index'] == dates[i] - dates[0]
        assert t['age_completed_years'] == (dates[i] - month(patient['synthetic_birth_month'])) // 12


def test_histogram_rounding_preserves_both_totals_and_each_bucket():
    from jamboree_sdv.tumor_multiplicity import allocate_histogram
    source = histogram()
    base = sum(source.values())
    total = sum(k*v for k, v in source.items())
    assert allocate_histogram(source, base, total) == source
    for n, rows in [(100, 109), (1000, 1088), (10000, 10881), (148392, 161469), (296783, 322937)]:
        result = allocate_histogram(source, n, rows)
        assert sum(result.values()) == n and sum(k*v for k, v in result.items()) == rows
        assert set(result) == set(source)
        assert all(abs(v*base - source[k]*n) < base for k, v in result.items())
        if n >= 1000:
            assert any(k > 2 and v for k, v in result.items())
    with pytest.raises(ValueError, match='feasible|Infeasible'):
        allocate_histogram({1: 2, 3: 2}, 4, 9)


def test_successor_recipe_selects_histogram_and_archives_evidence():
    from jamboree_sdv.full_preflight import compile_recipe
    path = EXPERIMENT/'recipes/all-sources-integrated-v3.json'
    recipe, preflight = compile_recipe(path)
    assert recipe.profiles['model']['tumor_allocation'] == 'pinned_patient_histogram_exact_totals'
    plan = preflight['adapted']['tumor_multiplicity']
    assert plan['status'] == 'allocated'
    assert any(int(k) > 2 and v for k, v in plan['patient_counts'].items())
    archived = {p['path'] for p in preflight['inputs']}
    assert str((REFERENCE/'analysis/quality.json').relative_to(WORKSPACE)) in archived


def make_rows(counts):
    return [{'nccrId':f'P{i}', 'tumorRecordNumber':f'{j+1:02d}', 'numberOfPrimaries':str(count)}
            for i, count in enumerate(counts) for j in range(count)]


def test_independent_check_rejects_legacy_shape_with_same_totals():
    local_verifiers()
    from check_tumor_multiplicity import check
    from jamboree_sdv.tumor_multiplicity import allocation_plan
    source=histogram()
    reference={'histogram':source,'patients':sum(source.values()),'rows':sum(k*v for k,v in source.items())}
    plan=allocation_plan(reference,1000,1088)
    counts=[int(k) for k,v in plan['patient_counts'].items() for _ in range(v)]
    rows=make_rows(counts)
    assert check(iter(rows),iter(rows),reference,plan,1000,1088)['status']=='passed'
    wrong=make_rows([1]*912+[2]*88)
    with pytest.raises(ValueError,match='rounding quota'):
        check(iter(wrong),iter(wrong),reference,plan,1000,1088)
    changed=[dict(row) for row in rows]
    changed[0]['numberOfPrimaries']='99'
    with pytest.raises(ValueError,match='numberOfPrimaries'):
        check(iter(changed),iter(rows),reference,plan,1000,1088)
    changed=[dict(row) for row in rows]
    changed[-1]['tumorRecordNumber']='01'
    with pytest.raises(ValueError,match='duplicate tumor key'):
        check(iter(rows),iter(changed),reference,plan,1000,1088)


@pytest.mark.parametrize('counts,patients,rows', [({0:1},1,0),({3:-1},1,3),({3:1},1,2),({100:1},1,100)])
def test_invalid_population_allocation_rejected(counts,patients,rows):
    with pytest.raises(ValueError,match='multiplicity'):
        Population(patients,rows,42,multiplicity=counts)


def test_profile_is_required_and_reference_hash_is_verified(tmp_path):
    from jamboree_sdv.full_recipe import load_full_recipe
    spec=read_json(EXPERIMENT/'recipes/all-sources-integrated-v3.json')
    del spec['profiles']['tumor-multiplicity']
    p=tmp_path/'missing.json';write_json(p,spec)
    with pytest.raises(ValueError,match='profile version|model policy'):
        load_full_recipe(p)
    spec=read_json(EXPERIMENT/'recipes/all-sources-integrated-v3.json')
    profile=read_json(WORKSPACE/spec['profiles']['tumor-multiplicity']['path'])
    profile['histogram']['sha256']='0'*64
    path=tmp_path/'profile.json';write_json(path,profile);spec['profiles']['tumor-multiplicity']=pin(path)
    p=tmp_path/'hash.json';write_json(p,spec)
    with pytest.raises(ValueError,match='hash mismatch'):
        load_full_recipe(p)


def test_reference_loads_from_archived_files_only(tmp_path,monkeypatch):
    import shutil
    from jamboree_sdv import multiplicity_reference
    spec=read_json(EXPERIMENT/'recipes/all-sources-integrated-v3.json')
    selected=spec['profiles']['tumor-multiplicity']
    loaded=multiplicity_reference.load_reference(selected)
    for item in loaded['inputs']:
        dest=tmp_path/item['path'];dest.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(WORKSPACE/item['path'],dest)
    def blocked(*args):raise AssertionError('Live input read')
    monkeypatch.setattr(multiplicity_reference,'checked_pin',blocked)
    assert multiplicity_reference.load_reference(selected,resolver=lambda p:tmp_path/p['path'])==loaded
