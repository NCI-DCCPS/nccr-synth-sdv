"""Runtime sizes, exact scale arithmetic, and independent streaming assessment."""
from copy import deepcopy
from decimal import Decimal,ROUND_HALF_UP
import subprocess
import sys
import pytest
from jamboree_sdv.common import EXPERIMENT,WORKSPACE,read_json,write_json,pin,shared_tools
from jamboree_sdv.sizing import resolve_size,scaled_targets
from jamboree_sdv.full_preflight import preflight,compile_recipe
from jamboree_sdv.full_writer import generate
from jamboree_sdv.full_audit import audit_candidate
from jamboree_sdv.full_assessment import assess_candidate

TEMPLATE=EXPERIMENT/'recipes/all-sources-v2.json'
CONTRACT=read_json(WORKSPACE/read_json(TEMPLATE)['contract']['path'])
COMMON=read_json(WORKSPACE/CONTRACT['common_inputs']['generation-contract.json']['path'])
SCALABLE_TEMPLATE=EXPERIMENT/'recipes/all-sources-scalable-v2.json'
SCALABLE_CONTRACT=read_json(WORKSPACE/read_json(SCALABLE_TEMPLATE)['contract']['path'])


@pytest.mark.parametrize('value,patients,raw_rows,cpi_rows',[
    ('0.01',14839,1416959,697),
    ('0.02',29678,2833918,1395),
    ('0.05',74196,7084794,3487),
])
def test_configurable_scales_have_independently_rounded_targets(value,patients,raw_rows,cpi_rows):
    size,n,targets=scaled_targets(COMMON,{'kind':'scale','value':value},SCALABLE_CONTRACT)
    assert n==patients and size=={'kind':'scale','value':value}
    assert sum(t['rows'] for t in targets.values())==raw_rows
    assert targets['CCDI']['rows']==cpi_rows
    assert all(t['row_adjustment']==t['patient_adjustment']==0 for t in targets.values())
    shared_tools();from assess_scaled_candidate import resolve_requested_size,expected_source_counts
    factor,observed_n=resolve_requested_size(size,COMMON['population'],SCALABLE_CONTRACT)
    assert observed_n==n and factor==Decimal(value)
    for source,target in targets.items():
        assert expected_source_counts(COMMON,source,n,factor,True)==(target['rows'],target['patients'])


def test_new_contract_keeps_patient_and_five_percent_targets():
    for request in [{'kind':'patients','value':100},{'kind':'patients','value':1000},
                    {'kind':'patients','value':10000},{'kind':'scale','value':'0.050'}]:
        assert scaled_targets(COMMON,request,SCALABLE_CONTRACT)==scaled_targets(COMMON,request)
    with pytest.raises(ValueError,match='0.05'):
        scaled_targets(COMMON,{'kind':'scale','value':'0.01'},CONTRACT)


def test_one_percent_is_not_rounded_patient_rescaling():
    _,n,targets=scaled_targets(COMMON,{'kind':'scale','value':'0.01'},SCALABLE_CONTRACT)
    assert targets['MCD']['rows']==566606
    wrong=int((Decimal(COMMON['sources']['MCD']['counts']['public_rows'])*n/Decimal(COMMON['population']['base_patients'])).quantize(Decimal(1),rounding=ROUND_HALF_UP))
    assert wrong!=targets['MCD']['rows']


@pytest.mark.parametrize('bounds',[
    {'minimum':'0','maximum':'0.05'}, {'minimum':'0.05','maximum':'0.001'},
    {'minimum':'NaN','maximum':'0.05'}, {'minimum':'0.001','maximum':'0.1'},
    {'minimum':'0.001'}, {'minimum':0.001,'maximum':'0.05'},
])
def test_malformed_contract_range_cannot_relax_limits(bounds):
    shared_tools();from assess_scaled_candidate import resolve_requested_size
    contract=deepcopy(SCALABLE_CONTRACT);contract['scale_range']=bounds
    request={'kind':'scale','value':'0.01'}
    with pytest.raises(ValueError):scaled_targets(COMMON,request,contract)
    with pytest.raises(ValueError):resolve_requested_size(request,COMMON['population'],contract)


@pytest.mark.parametrize('value',['0','-0.01','0.000999','0.050001','1','NaN','Infinity','bad',0.01,True])
def test_both_sizing_implementations_reject_unsupported_scales(value):
    shared_tools();from assess_scaled_candidate import resolve_requested_size
    request={'kind':'scale','value':value}
    with pytest.raises(ValueError):scaled_targets(COMMON,request,SCALABLE_CONTRACT)
    with pytest.raises(ValueError):resolve_requested_size(request,COMMON['population'],SCALABLE_CONTRACT)


def test_scale_boundaries_and_canonical_decimal():
    for value in ['0.001','0.0100','1E-2','0.05']:
        size,n,targets=scaled_targets(COMMON,{'kind':'scale','value':value},SCALABLE_CONTRACT)
        assert size['value']==str(Decimal(value).normalize())
        assert min(t['patients'] for t in targets.values())>=1
    shared_tools();from candidate_evidence import required_sizes
    for n in [1484,14839,74196]:
        assert required_sizes({'size':{'kind':'scale','value':'0.001'},'patients':n})==[100,1000,10000]


def test_independent_assessor_preserves_full_reference_checks():
    shared_tools();from assess_scaled_candidate import expected_source_counts
    altered=deepcopy(COMMON);altered['sources']['CTC']['counts']['full_rows']+=1
    with pytest.raises(ValueError,match='Full target'):
        expected_source_counts(altered,'CTC',74196,Decimal('0.05'),True)
    assert expected_source_counts(altered,'CTC',14839,Decimal('0.01'),True)[0]==16147


def test_resource_forecast_never_drops_below_benchmark(monkeypatch):
    from types import SimpleNamespace
    from jamboree_sdv import resources
    shared_tools();import candidate_evidence
    evidence=[{'patients':n,'generation':{'peak_rss_bytes':rss,'disk_payload_bytes':n*100},
               'audit':{'peak_rss_bytes':rss},'assessment_metrics':{'peak_rss_bytes':rss}}
              for n,rss in [(1000,1000000),(10000,10000000)]]
    monkeypatch.setattr(candidate_evidence,'validate_prerequisites',lambda *args:evidence)
    monkeypatch.setattr(resources,'snapshot',lambda:{'available_memory_bytes':10**10,'disk_free_bytes':10**11})
    def budget(n):return resources.check_budget(SimpleNamespace(spec={'size':{'kind':'scale'},'patients':n},common={}))
    assert budget(1484)['forecast']==budget(10000)['forecast']
    assert budget(14839)['forecast']['peak_rss_bytes']['generation']>budget(10000)['forecast']['peak_rss_bytes']['generation']
    monkeypatch.setattr(resources,'snapshot',lambda:{'available_memory_bytes':1000,'disk_free_bytes':10**11})
    with pytest.raises(ValueError,match='memory forecast'):budget(14839)
    monkeypatch.setattr(resources,'snapshot',lambda:{'available_memory_bytes':10**10,'disk_free_bytes':1000})
    with pytest.raises(ValueError,match='free disk'):budget(14839)


def test_scoped_fingerprint_ignores_unrelated_tools_but_tracks_transitive_imports(tmp_path,monkeypatch):
    from jamboree_sdv import common
    experiment=tmp_path/'experiment';experiment.mkdir()
    for name in ['pyproject.toml','uv.lock']:(experiment/name).write_text('')
    directory=tmp_path/'tools';directory.mkdir()
    (directory/'entry.py').write_text('from helper import value\n')
    (directory/'helper.py').write_text('import dependency\nvalue = 1\n')
    (directory/'dependency.py').write_text('value = 1\n')
    unrelated=directory/'other_generator.py';unrelated.write_text('value = 1\n')
    monkeypatch.setattr(common,'WORKSPACE',tmp_path)
    monkeypatch.setattr(common,'EXPERIMENT',experiment)
    before=common.code_pins(['entry'])
    unrelated.write_text('value = 2\n')
    assert common.code_pins(['entry'])==before
    (directory/'dependency.py').write_text('value = 2\n')
    assert common.code_pins(['entry'])!=before
    assert 'tools/other_generator.py' not in {p['path'] for p in before}


def test_exact_scale_does_not_rescale_rounded_patient_count():
    size,n,targets=scaled_targets(COMMON,{'kind':'scale','value':'0.050'})
    assert size=={'kind':'scale','value':'0.05'} and n==74196
    assert targets['CTC']['rows']==80734
    assert targets['MCD']['rows']==2833028
    assert targets['MCP']['rows']==3287275
    assert sum(t['rows'] for t in targets.values())==7084794
    assert targets['RO']['patients']==133 and targets['CCDI']['rows']==3487
    assert all(t['row_adjustment']==0 and t['patient_adjustment']==0 for t in targets.values())
    wrong=int((Decimal(56660564)*n/Decimal(COMMON['population']['base_patients'])).quantize(Decimal(1),rounding=ROUND_HALF_UP))
    assert wrong!=targets['MCD']['rows']


def test_patient_targets_preserve_pilot_counts():
    _,n,t=scaled_targets(COMMON,{'kind':'patients','value':1000})
    assert n==1000 and t['MCD']['rows']==38183 and t['CTC']['rows']==1088
    _,_,t=scaled_targets(COMMON,{'kind':'patients','value':100})
    assert t['RO']['rows']==1 and t['RO']['row_adjustment']==1


@pytest.mark.parametrize('size_request',[
    {'kind':'scale','value':'NaN'},{'kind':'scale','value':'Infinity'},
    {'kind':'scale','value':'0.1'},{'kind':'scale','value':0.05},
    {'kind':'patients','value':True},{'kind':'patients','value':0},
    {'kind':'patients','value':10001},{'kind':'rows','value':1000}])
def test_invalid_size_fails_explicitly(size_request):
    with pytest.raises(ValueError):resolve_size(size_request,COMMON['population'])


def test_cli_size_parameters_are_mutually_exclusive():
    p=subprocess.run([sys.executable,'-B','-m','jamboree_sdv.cli','run','--patients','1000','--scale','0.05'],capture_output=True,text=True)
    assert p.returncode==2 and 'not allowed with argument' in p.stderr


def test_benchmark_and_full_candidate_require_prior_passes(tmp_path):
    spec=read_json(TEMPLATE);spec.update(size={'kind':'patients','value':10000},patients=10000)
    path=tmp_path/'benchmark.json';write_json(path,spec)
    with pytest.raises(ValueError,match='Missing passing prerequisite'):compile_recipe(path)
    spec.update(size={'kind':'scale','value':'0.05'},patients=74196)
    path=tmp_path/'full.json';write_json(path,spec)
    with pytest.raises(ValueError,match='Missing passing prerequisite'):compile_recipe(path)


@pytest.fixture(scope='module')
def scaled_candidate(tmp_path_factory):
    out=tmp_path_factory.mktemp('scaled-candidate');spec=read_json(TEMPLATE)
    spec.update(size={'kind':'patients','value':100},patients=100,template=pin(TEMPLATE))
    recipe=out/'effective.json';write_json(recipe,spec)
    preflight(recipe,out/'preflight');generate(recipe,out/'preflight/preflight.json',out/'run',out/'data',out/'release.json')
    audit_candidate(recipe,out/'release.json',out/'audit')
    result=assess_candidate(recipe,out/'run',out/'release.json',out/'audit/audit.json',out/'assessment')
    return out,recipe,result


def test_streaming_assessment_checks_all_fields_and_rules(scaled_candidate):
    out,recipe,result=scaled_candidate
    assert result['status']=='passed_all_source_pilot',result['findings']
    assert len(result['coverage'])==538 and len(result['observed_counts'])==10
    assert result['resource_checks']['status']=='passed'
    assert all(r['status'] in ['passed','disabled'] for r in result['rule_results'])
    assert result['full_release_ready'] is False


def test_prerequisite_report_is_bound_to_actual_counts(scaled_candidate,tmp_path):
    out,recipe,result=scaled_candidate;shared_tools();from candidate_evidence import verify_prerequisite
    spec=read_json(recipe);common={k:read_json(WORKSPACE/p['path']) for k,p in CONTRACT['common_inputs'].items()}
    proof=verify_prerequisite(pin(out/'assessment/assessment.json'),spec,common)
    assert proof['patients']==100
    # Change the count in an otherwise matching report: audit/run hashes still bind it.
    altered=deepcopy(result);altered['observed_counts']['CTC']['distinct_patients']=10000
    path=out/'assessment/altered-count.json';write_json(path,altered)
    with pytest.raises(ValueError,match='count mismatch'):verify_prerequisite(pin(path),spec,common)
