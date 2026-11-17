import copy
import pytest
from jamboree_sdv.common import EXPERIMENT, read_json
from jamboree_sdv.preflight import compile_recipe, preflight
from jamboree_sdv.adapters.rules import topological_order, adapt_rules


def test_dependency_cycles_and_unknown_rules():
    with pytest.raises(ValueError,match='Cyclic'):
        topological_order({'a':['b'],'b':['a']})
    recipe,data=compile_recipe(EXPERIMENT/'recipes/pilot-core-100-v1-replay.json')
    recipe.rules=copy.deepcopy(recipe.rules)
    recipe.rules.append({'id':'invented','enabled':True,'type':'patient_tumor_grain'})
    with pytest.raises(ValueError,match='No generator implementation'):
        adapt_rules(recipe,data['adapted'])


def test_hierarchy_and_source_discrepancies(tmp_path):
    for version in ['v1','v2']:
        result=preflight(EXPERIMENT/f'recipes/pilot-core-100-{version}-replay.json',tmp_path/version)
        hierarchy=result['adapted']['hierarchy']
        assert bool(hierarchy)==(version=='v2')
        if hierarchy:
            assert len(hierarchy['mapping'])==116
            assert result['adapted']['iccc_discrepancies']['unreported_valid_leaf_codes']==['090']
        assert read_json(tmp_path/version/'preflight.json')['inputs']==result['inputs']
        with pytest.raises(ValueError):preflight(EXPERIMENT/f'recipes/pilot-core-100-{version}-replay.json',tmp_path/version)

