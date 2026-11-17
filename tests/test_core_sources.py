from collections import Counter
import pytest
from jamboree_sdv.common import EXPERIMENT
from jamboree_sdv.preflight import compile_recipe
from jamboree_sdv.population import Population
from jamboree_sdv.sources import CoreSources


def rows(version,chunk=137):
    recipe,pre=compile_recipe(EXPERIMENT/f'recipes/pilot-core-100-{version}-replay.json')
    recipe.spec['chunk_size']=chunk
    generator=CoreSources(recipe,pre['adapted'])
    return {s:list(generator.rows(s)) for s in recipe.spec['sources']}


def test_linkage_counts_and_conditional_rules():
    data=rows('v2')
    ids={r['nccrId'] for r in data['CTC']}
    assert len(ids)==100 and len(data['CTC'])==109 and len(data['ABM'])==109
    assert {r['nccrId'] for r in data['ABM']}==ids=={r['nccrId'] for r in data['COG']}
    assert len(data['COG'])==100
    assert {(r['nccrId'],r['tumorRecordNumber']) for r in data['CTC']}=={(r['nccrId'],r['tumorRecordNumber']) for r in data['ABM']}
    for row in data['COG']:
        counts=[v for k,v in row.items() if k.endswith('Count')]
        assert all(v=='7777' for v in counts) if row['reportedCOGEnrollment']=='0' else all(0<=int(v)<=999 for v in counts)


def test_v2_paths_and_independent_streams():
    v1,v2=rows('v1'),rows('v2')
    _,pre=compile_recipe(EXPERIMENT/'recipes/pilot-core-100-v2-replay.json')
    mapping=pre['adapted']['hierarchy']['mapping']
    for a,b in zip(v1['CTC'],v2['CTC']):
        for key in a:
            if key not in ['icccMajor','iccc2017Recode']:assert a[key]==b[key]
        assert mapping[b['iccc2017ExtendedRecode']]=={k:b[k] for k in ['icccMajor','iccc2017Recode']}
    assert v1['ABM']==v2['ABM'] and v1['COG']==v2['COG']
    assert rows('v2',chunk=1)==v2==rows('v2',chunk=10000)


def test_patient_attributes_stable_across_tumors():
    data=rows('v2');seen={}
    for row in data['CTC']:
        attrs=tuple(row[k] for k in ['sex','raceAndOriginRecode','vitalStatusRecode'])
        assert seen.setdefault(row['nccrId'],attrs)==attrs

