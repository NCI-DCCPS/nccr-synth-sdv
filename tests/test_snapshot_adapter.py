import pytest
from jamboree_sdv.adapters.snapshot import compile_distribution


def item(values):
    return {'charts':[{'values':[{'value':v,'count':c} for v,c in values]}], 'definition':{'Column_DataType_at_Source':{'Semantic_Type':'string'},'Permissible_Values':[]}}


def test_duplicates_unknown_counts_and_zero_total():
    for values in [[('01',1),('01',2)], [('01',None)], [('01',0)], [('01',-1)], [('01',float('inf'))]]:
        with pytest.raises(ValueError): compile_distribution(item(values), {'chart_index':0})


def test_ranges_and_sentinels_require_explicit_policy():
    data=item([('4 to 6',3),('Missing',1)])
    with pytest.raises(ValueError):compile_distribution(data, {'chart_index':0})
    d=compile_distribution(data, {'chart_index':0, 'range_policy':{'within_bin':'uniform_integer','open_upper_bound':999},'label_codes':{'Missing':'9999'}})
    assert d['categories']==['4','5','6','9999']
    assert d['probabilities']==[.25]*4


def test_one_explicit_chart_and_leading_zeros():
    data=item([('001',1),('999',3)])
    data['charts'].append({'values':[{'value':'bad','count':999}]})
    d=compile_distribution(data, {'chart_index':0})
    assert d['categories']==['001','999']
    assert d['probabilities']==[.25,.75]

