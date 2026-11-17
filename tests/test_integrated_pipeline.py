"""The current full recipe must enforce all rules at parameterized sizes."""
from copy import deepcopy
from decimal import Decimal
import importlib
import sys
import shutil

import pytest

from jamboree_sdv.common import EXPERIMENT, WORKSPACE, read_json, write_json, pin, shared_tools
from jamboree_sdv.sizing import scaled_targets
from jamboree_sdv.full_preflight import compile_recipe, preflight
from jamboree_sdv.full_writer import generate
from jamboree_sdv.full_audit import audit_candidate
from jamboree_sdv.full_assessment import assess_candidate

TEMPLATE = EXPERIMENT/'recipes/all-sources-integrated-v2.json'
SPEC = read_json(TEMPLATE)
CONTRACT = read_json(WORKSPACE/SPEC['contract']['path'])
COMMON = read_json(WORKSPACE/CONTRACT['common_inputs']['generation-contract.json']['path'])


def verifier(name):
    shared_tools()
    sys.path.insert(0, str(EXPERIMENT/'verifier'))
    assert (EXPERIMENT/'verifier'/(name+'.py')).exists(), 'Integrated verifier missing'
    return importlib.import_module(name)


@pytest.mark.parametrize('value,n', [('0.1',148392),('0.2',296783),('1',1483916),('1.2',1780699)])
def test_positive_scales_have_no_legacy_ceiling(value,n):
    size,patients,targets=scaled_targets(COMMON,{'kind':'scale','value':value},CONTRACT)
    assert patients==n
    assert targets['MCD']['rows']==int((Decimal('56660564')*Decimal(value)).quantize(Decimal('1')))
    independent=verifier('integrated_assessment')
    assert independent.resolve_requested_size(size,COMMON['population'],CONTRACT)==(Decimal(value),n)


def test_patient_parameter_has_no_legacy_ceiling():
    assert scaled_targets(COMMON,{'kind':'patients','value':148392},CONTRACT)[1]==148392


@pytest.mark.parametrize('value',['0','-0.1','NaN','Infinity','bad'])
def test_invalid_unbounded_scales_still_rejected(value):
    with pytest.raises(ValueError):scaled_targets(COMMON,{'kind':'scale','value':value},CONTRACT)


def test_integrated_recipe_compiles_all_new_rules_and_plans(tmp_path):
    spec=deepcopy(SPEC);spec.update(patients=100,size={'kind':'patients','value':100},template=pin(TEMPLATE))
    path=tmp_path/'recipe.json';write_json(path,spec)
    recipe,compiled=compile_recipe(path)
    assert recipe.spec['schema_version']==4 and recipe.rules_spec['version']=='2.3.0'
    adapted=compiled['adapted']
    assert adapted['fields']['CCDI.resourceName']['mode']=='catalog_sample'
    assert 'ACNS0332' not in adapted['fields']['CCDI.resourceName']['distribution']['categories']
    assert adapted['fields']['PHARM.canmedDrugCategory']['mode']=='drug_tuple'
    assert adapted['pharm_plan']['rows']==recipe.targets['PHARM']['rows']
    assert len([r for r in adapted['capabilities'] if r['status']!='disabled'])==len([r for r in recipe.rules if r['enabled']])


def test_ten_percent_reaches_prerequisite_gate_not_size_cap(tmp_path):
    spec=deepcopy(SPEC);spec.update(patients=148392,size={'kind':'scale','value':'0.1'})
    path=tmp_path/'recipe.json';write_json(path,spec)
    with pytest.raises(ValueError,match='prerequisite'):compile_recipe(path)


@pytest.fixture(scope='module', params=[TEMPLATE, EXPERIMENT/'recipes/all-sources-integrated-v3.json'], ids=['legacy','multiplicity'])
def integrated_candidate(tmp_path_factory,request):
    out=tmp_path_factory.mktemp('integrated');spec=read_json(request.param)
    spec.update(patients=100,size={'kind':'patients','value':100},template=pin(request.param))
    path=out/'recipe.json';write_json(path,spec)
    preflight(path,out/'preflight')
    generate(path,out/'preflight/preflight.json',out/'run',out/'data',out/'release.json')
    report=audit_candidate(path,out/'release.json',out/'audit')
    result=assess_candidate(path,out/'run',out/'release.json',out/'audit/audit.json',out/'assessment')
    return out,path,report,result


def test_full_pipeline_assesses_all_rules_and_populates_pharm(integrated_candidate):
    out,path,audit,result=integrated_candidate
    assert result['status']=='passed_all_source_pilot',result['findings']
    assert len(result['coverage'])==538 and len(result['observed_counts'])==10
    rules=read_json(WORKSPACE/SPEC['rules']['path'])
    expected={r['id']:'passed' if r['enabled'] else 'disabled' for r in rules['rules']+rules['biological_constraints']}
    assert {r['id']:r['status'] for r in result['rule_results']}==expected
    assert {r['id']:r['status'] for r in audit['rule_results']}==expected
    assert all(row['blank_cells']==0 for row in result['coverage'] if row['source']=='PHARM')
    assert result['raw_layout_checks']['ccdi_mapping_rows']==result['observed_counts']['CPI']['rows']
    if 'tumor-multiplicity' in read_json(path)['profiles']:
        check=audit['extension_checks']['tumor_multiplicity']
        assert check['status']=='passed' and check['maximum_observed']>2
        assert check['patients']==100 and check['tumor_rows']==109


def test_lookup_inputs_can_be_checked_using_only_archived_copies(tmp_path,monkeypatch):
    from jamboree_sdv import integrated_inputs
    from jamboree_sdv.common import digest
    loaded=integrated_inputs.load_inputs(SPEC)
    for item in loaded['inputs']:
        target=tmp_path/item['path'];target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(WORKSPACE/item['path'],target)
    def blocked(*args):raise AssertionError('Live input read')
    monkeypatch.setattr(integrated_inputs,'checked_pin',blocked)
    def resolve(item):
        path=tmp_path/item['path'];assert digest(path)==item['sha256'];return path
    replay=integrated_inputs.load_inputs(SPEC,resolver=resolve)
    assert replay['catalog']['counts']==loaded['catalog']['counts']
    assert replay['pharm']['canmed']==loaded['pharm']['canmed']


def test_streaming_pharm_matches_existing_seeded_allocation():
    from jamboree_sdv.pharm_enrichment import iter_enriched,enrich_rows
    plan={'rows':5,'groups':[{'count':3,'tuples':[{'medicationRequestNdc11':'00000000001'},{'medicationRequestNdc11':'00000000002'}]},
                           {'count':2,'tuples':[{'medicationRequestNdc11':'UNK'}]}]}
    rows=[{'nccrId':str(i),'monthsFromIndexDxtoDispense':'0'} for i in range(5)]
    assert list(iter_enriched(iter(rows),plan,42))==enrich_rows(rows,plan,42)
    with pytest.raises(ValueError,match='Too few'):list(iter_enriched(iter(rows[:-1]),plan,42))
    with pytest.raises(ValueError,match='Too many'):list(iter_enriched(iter(rows+rows),plan,42))


def test_promotion_archives_standalone_integrated_verifiers(integrated_candidate):
    from pathlib import Path
    from jamboree_sdv.promotion_evidence import LocalEvidence,validate_record
    from jamboree_sdv.promotion import _layout
    out,_,_,_=integrated_candidate
    reader=LocalEvidence(WORKSPACE)
    record=validate_record(reader,pin(out/'assessment/assessment.json'))
    copies=_layout(reader,record,Path('runs/gen-sdv/test'),Path('data/2026-data-jamboree/test'))
    paths={c['destination'] for c in copies}
    for path in ['integrated_shared.py','integrated_promoted_science.py','integrated_rules.py',
                 'jamboree_sdv/catalog_contract.py','jamboree_sdv/integrated_inputs.py']:
        assert 'runs/gen-sdv/test/verifier/'+path in paths


def test_integrated_shared_checks_use_archived_copies_only(integrated_candidate,tmp_path):
    from pathlib import Path
    import subprocess,json
    from jamboree_sdv.promotion_evidence import LocalEvidence,validate_record,canonical,sha
    from jamboree_sdv.promotion import _layout,_mapped
    out,_,_,_=integrated_candidate
    reader=LocalEvidence(WORKSPACE);record=validate_record(reader,pin(out/'assessment/assessment.json'))
    rel=Path('runs/gen-sdv/test');copies=_layout(reader,record,rel,Path('data/2026-data-jamboree/test'))
    for item in copies:
        target=tmp_path/item['destination'];target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(item['source'],target)
    def mapped(item):
        result=_mapped(copies,canonical(item['path'],reader.root),item['sha256'])
        return {**result,'path':str(tmp_path/result['path'])}
    manifest=deepcopy(record['manifest'])
    for group in ['files','raw_files']:
        manifest[group]=[{**f,'local_path':mapped({'path':f['local_path'],'sha256':f['sha256']})['path']} for f in manifest[group]]
    for k in ['rules','context','context_contract']:manifest[k]=mapped(manifest[k])
    run={**deepcopy(record['run']),**{k:manifest[k] for k in ['files','raw_files','rules','context','context_contract']},
         'snapshot':mapped(record['recipe']['snapshot']),
         'inputs':[mapped(item) for item in record['run']['archives']['inputs']],
         'coverage':mapped({'path':str(out/'run/coverage.json'),'sha256':record['run']['coverage_sha256']}),
         'origin':{k:mapped({'path':record[k+'_original'],'sha256':record['assessment']['bindings'][k+'_sha256']}) for k in ['run','manifest']},
         'verifier':[{'path':str(tmp_path/item['destination']),'sha256':item['sha256']} for item in copies if Path(item['destination']).is_relative_to(rel/'verifier')]}
    shared_manifest=tmp_path/'release.json';write_json(shared_manifest,manifest)
    write_json(tmp_path/'run.json',run)
    write_json(tmp_path/'promotion.json',{'original_workspace':str(WORKSPACE),'copies':[{k:v for k,v in item.items() if k!='source'} for item in copies]})
    script='''import sys,json
from pathlib import Path
root=Path(sys.argv[1]);original=Path(sys.argv[2]);sys.path[:0]=[str(root/'runs/gen-sdv/test/verifier'),str(root/'runs/gen-sdv/test/verifier/tools')]
from verify_promoted import install_boundary
install_boundary(root,original,[root])
from promotion_evidence import SharedEvidence,read
from integrated_shared import audit_shared
from integrated_promoted_science import assess_outputs
promotion=read(root/'promotion.json');reader=SharedEvidence(root,promotion)
manifest=read(root/'release.json');run=read(root/'run.json')
code=audit_shared(manifest,run,root/'release.json',Path(run['snapshot']['path']).parent/'metadata',root/'fresh-audit',reader)
assert code in [0,1]
audit=read(root/'fresh-audit/audit.json')
result=assess_outputs(manifest,run,audit,{'resources':{'available_memory_bytes':10**12}},reader)
assert not result['findings'],result['findings']
assert all(r['status'] in ['passed','disabled'] for r in result['rule_results'])
print('archived integrated checks passed')
'''
    process=subprocess.run([sys.executable,'-I','-S','-B','-c',script,str(tmp_path),str(WORKSPACE)],capture_output=True,text=True)
    assert process.returncode==0,process.stdout+process.stderr


def test_integrated_assessor_rejects_omitted_new_rule_results(integrated_candidate,tmp_path):
    out,path,report,_=integrated_candidate
    changed=deepcopy(report);changed['rule_results']=[r for r in changed['rule_results'] if not r['id'].startswith('pharm_')]
    directory=tmp_path/'audit';directory.mkdir()
    write_json(directory/'audit.json',changed)
    provenance=read_json(out/'audit/provenance.json')
    from jamboree_sdv.common import digest
    provenance['audit_sha256']=digest(directory/'audit.json');write_json(directory/'provenance.json',provenance)
    result=verifier('integrated_assessment').assess(out/'release.json',out/'run',directory/'audit.json',path)
    assert any('Fresh rule evaluation' in f for f in result['findings'])


def test_pharm_rule_rejects_invented_classification(integrated_candidate,tmp_path):
    import csv,io,zipfile
    out,path,_,_=integrated_candidate
    module=verifier('integrated_rules')
    from jamboree_sdv.integrated_inputs import load_inputs
    source=out/'data/PHARM.zip';target=tmp_path/'PHARM.zip'
    with zipfile.ZipFile(source) as z:
        table=list(csv.reader(io.StringIO(z.read('PHARM.csv').decode())))
        table[1][table[0].index('canmedMajorDrugClass')]='INVENTED'
        stream=io.StringIO(newline='');csv.writer(stream).writerows(table)
        with zipfile.ZipFile(target,'x') as dest:
            dest.writestr('PHARM.csv',stream.getvalue());dest.writestr('pharm_metadata.json',z.read('pharm_metadata.json'))
    with (out/'run/context/patient.csv').open() as stream:patients={r['nccrId']:r for r in csv.DictReader(stream)}
    plan=read_json(out/'run/inputs/adapted/preflight.json')['adapted']['pharm_plan']
    with pytest.raises(ValueError,match='tuple mismatch|invented CanMED|missing state'):
        module.check_pharm(target,load_inputs(read_json(path))['pharm'],plan,patients)


def test_resource_gate_applies_to_large_patient_requests(monkeypatch):
    from types import SimpleNamespace
    from jamboree_sdv import resources
    module=verifier('integrated_prerequisites')
    proofs=[{'patients':n,'generation':{'peak_rss_bytes':n*100,'disk_payload_bytes':n*10},
             'audit':{'peak_rss_bytes':n*100},'assessment_metrics':{'peak_rss_bytes':n*100}}
            for n in [1000,10000]]
    monkeypatch.setattr(module,'validate_prerequisites',lambda *args:proofs)
    monkeypatch.setattr(resources,'snapshot',lambda:{'available_memory_bytes':1000,'disk_free_bytes':10**12})
    recipe=SimpleNamespace(spec={'schema_version':4,'size':{'kind':'patients','value':148392},'patients':148392},common={})
    with pytest.raises(ValueError,match='memory forecast'):resources.check_budget(recipe)
