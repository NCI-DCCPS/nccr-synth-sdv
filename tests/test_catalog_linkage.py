"""Catalog-constrained sampling preserves identities and declared conditional weights."""
from collections import Counter
from copy import deepcopy

import pytest

from jamboree_sdv.catalog_linkage import conditioned_distribution, regenerate_mappings


def catalog(*names):
    return [{'domain_name': name, 'domain_description': name+' description',
             'domain_category': 'study', 'data_location': 'https://example.test/'+name}
            for name in names]


def test_regeneration_keeps_patient_and_participant_ids_and_uses_catalog_only():
    rows=[('0001','0002','unsupported'),('0001','0003','A'),('0004','0005','unsupported')]
    distribution={'categories':['A','unsupported','B'],'probabilities':[.2,.6,.2]}
    before=deepcopy((rows,distribution))
    result,policy=regenerate_mappings(rows,distribution,catalog('A','B','catalog_only'),20261110)
    assert [(r[0],r[1]) for r in result]==[(r[0],r[1]) for r in rows]
    assert len(result)==3 and {r[2] for r in result}<={'A','B'}
    assert (rows,distribution)==before
    assert (result,policy)==regenerate_mappings(rows,distribution,catalog('A','B','catalog_only'),20261110)
    assert policy['excluded_categories']==['unsupported']
    assert policy['retained_probability_mass']==pytest.approx(.4)
    assert policy['probabilities']==pytest.approx([.5,.5])


def test_conditional_sampling_preserves_relative_public_weights():
    rows=[(f'patient{i//4}',f'participant{i}','old') for i in range(20000)]
    distribution={'categories':['A','unsupported','B'],'probabilities':[.1,.5,.4]}
    result,_=regenerate_mappings(rows,distribution,catalog('A','B'),20261110)
    counts=Counter(r[2] for r in result)
    assert counts['A']/len(rows)==pytest.approx(.2,abs=.02)
    assert counts['B']/len(rows)==pytest.approx(.8,abs=.02)


@pytest.mark.parametrize('domains,distribution,error',[
    (catalog('A','A'),{'categories':['A'],'probabilities':[1]},'Duplicate catalog'),
    (catalog('B'),{'categories':['A'],'probabilities':[1]},'No supported'),
    (catalog('A'),{'categories':['A','A'],'probabilities':[.5,.5]},'distribution'),
    (catalog('A'),{'categories':['A'],'probabilities':[-1]},'distribution'),
    (catalog('A'),{'categories':['A'],'probabilities':[float('nan')]},'distribution'),
    ([{'domain_name':'A'}],{'categories':['A'],'probabilities':[1]},'catalog entry'),
])
def test_invalid_inputs_fail_before_generation(domains,distribution,error):
    with pytest.raises(ValueError,match=error):conditioned_distribution(distribution,domains)


def test_ambiguous_participant_identity_is_rejected():
    rows=[('patient1','same','A'),('patient2','same','B')]
    with pytest.raises(ValueError,match='participant'):
        regenerate_mappings(rows,{'categories':['A'],'probabilities':[1]},catalog('A'),42)
