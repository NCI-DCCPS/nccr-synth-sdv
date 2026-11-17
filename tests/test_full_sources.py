"""All-source boundaries, independent validation and tamper rejection."""
from copy import deepcopy
import csv
import io
from pathlib import Path
import zipfile
import pytest
from jamboree_sdv.common import EXPERIMENT,WORKSPACE,digest,pin,read_json,write_json,shared_tools
from jamboree_sdv.full_preflight import compile_recipe,preflight
from jamboree_sdv.full_writer import generate
from jamboree_sdv.full_audit import audit_candidate
from jamboree_sdv.full_recipe import load_full_recipe
from jamboree_sdv.numeric_bins import compile_numeric,draw_numeric
from jamboree_sdv.context import mce_code

RECIPE=EXPERIMENT/'recipes/pilot-all-100-v2.json'


def numeric(labels,domain,step='1'):
    item={'definition':{'Permissible_Values':[{'Value':v} for v in domain]},'charts':[{'values':[{'value':v,'count':c} for v,c in labels]}]}
    policy={'field':'test','chart_index':0,'numeric_policy':{'step':step,'lower':-999,'upper':10000,'within_bin':'uniform_over_permitted_grid','ambiguous_sentinel_mass':'uniform_over_permitted_grid'},'label_codes':{'Missing':'9999'}}
    return compile_numeric(item,policy)


def test_signed_numeric_bins_and_missing_sentinel():
    d=numeric([('< -120',1),('-120 to -1',1),('0',1),('1 to 120',1),('> 120',1),('Missing',1)],['-999-999','9999'])
    assert draw_numeric(d,0,0)=='-999'
    assert draw_numeric(d,.999,.5)=='9999'
    for u in [0,.25,.5,.75,.99]:assert -2<=int(draw_numeric(d,u,.7,-2,2))<=2
    assert draw_numeric(d,.999,.5,0,1,('9999',))=='9999'


def test_numeric_domain_intersection_and_invalid_bins():
    d=numeric([('90.001 to 100.0',4)],['0-50','99'])
    assert draw_numeric(d,.2,.8)=='99'
    d=numeric([('> 100.0',4)],['0-100','999'],'0.001')
    assert draw_numeric(d,.2,.8)=='999'
    with pytest.raises(ValueError,match='Overlapping'):numeric([('1 to 10',1),('5 to 20',1)],['0-100'])
    with pytest.raises(ValueError,match='no permissible'):numeric([('101 to 200',1)],['0-100'])
    with pytest.raises(ValueError,match='Unknown'):numeric([('0',None)],['0-100'])


def test_mce_bidirectional_timing_boundaries():
    p={'synthetic_index_month':'2000-01','coverage_end_month':'2001-01','synthetic_birth_month':'1990-01',
       'index_year':2000,'index_age_completed_years':10,'deceased':True,'death_offset_months':3}
    assert mce_code(p,-1,False,0,.5)=='6'
    assert mce_code(p,3,True,0,.5)=='1'
    assert mce_code(p,4,False,0,.5)=='8'
    assert mce_code(p,20,False,0,.5)=='8'
    p.update(index_year=1999,synthetic_index_month='1999-01')
    assert mce_code(p,1,False,0,.5)=='6'
    assert mce_code(p,4,True,0,.5)=='8'  # death precedence over year-1999 code 6
    p.update(deceased=False,death_offset_months=None,index_year=2001,synthetic_index_month='2001-01')
    assert mce_code(p,1,True,0,.5)=='7'  # after coverage
    assert mce_code(p,0,False,0,.5)=='7'


def test_full_recipe_rejects_stale_or_incomplete_scope(tmp_path):
    recipe=read_json(RECIPE);recipe['sources']=recipe['sources'][:-1];path=tmp_path/'bad.json';write_json(path,recipe)
    with pytest.raises(ValueError,match='nine'):load_full_recipe(path)
    recipe=read_json(RECIPE);recipe['rules']['sha256']='0'*64;path=tmp_path/'hash.json';write_json(path,recipe)
    with pytest.raises(ValueError,match='hash|SHA|mismatch'):load_full_recipe(path)


def test_full_preflight_rejects_unimplemented_distribution_policy(tmp_path):
    recipe=read_json(RECIPE);profile=read_json(WORKSPACE/recipe['profiles']['field-policies']['path'])
    field=next(p for p in profile['fields'] if p['source']=='ABM' and p['mode']=='sample')
    field['count_policy']='uniform_ignore_public_counts'
    path=tmp_path/'profile.json';write_json(path,profile);recipe['profiles']['field-policies']=pin(path)
    target=tmp_path/'recipe.json';write_json(target,recipe)
    with pytest.raises(ValueError,match='distribution policy'):compile_recipe(target)


@pytest.fixture(scope='module')
def candidate(tmp_path_factory):
    out=tmp_path_factory.mktemp('all-source');preflight(RECIPE,out/'preflight')
    generate(RECIPE,out/'preflight/preflight.json',out/'run',out/'data',out/'release.json')
    report=audit_candidate(RECIPE,out/'release.json',out/'audit');return out,report


def test_full_pilot_passes_independent_assessment(candidate):
    out,report=candidate;shared_tools();from assess_all_source_pilot import assess
    result=assess(out/'release.json',out/'run',out/'audit/audit.json',RECIPE)
    assert result['status']=='passed_all_source_pilot',result['findings']
    assert len(result['coverage'])==538 and len(result['observed_counts'])==10
    assert all(r['status'] in ['passed','disabled'] for r in report['rule_results'])
    assert result['full_release_ready'] is False


def test_raw_ccdi_has_one_patient_row_and_processed_metadata_projection(candidate):
    out, _ = candidate
    with zipfile.ZipFile(out/'data/CCDI.zip') as archive:
        rows = list(csv.reader(io.StringIO(archive.read('CCDI.csv').decode())))
    assert rows[0][:3] == ['nccrId', 'resourceParticipantId1', 'resourceName1']
    assert len(rows) - 1 == len({r[0].lower() for r in rows[1:]})
    manifest = read_json(out/'release.json')
    assert Path(next(f['local_path'] for f in manifest['files'] if f['source']=='CCDI')) == out/'run/processed/CCDI.zip'
    assert {p.name for p in (out/'data').iterdir()} == {
        *(s+'.zip' for s in ['CTC','ABM','COG','MCE','MCD','MCP','PHARM','RO','CCDI']), 'CPI.csv'}
    assert len(manifest['raw_files']) == 10
    from jamboree_sdv.promotion_evidence import verify_raw_files
    checks = verify_raw_files(manifest['raw_files'], manifest['files'])
    assert checks['ccdi_patient_rows'] == len(rows)-1
    assert checks['ccdi_mapping_rows'] == 5


def test_validation_rejects_broken_raw_ccdi_even_when_processed_view_is_valid(candidate,tmp_path):
    from jamboree_sdv.promotion_evidence import verify_raw_files
    out,_=candidate;manifest=read_json(out/'release.json')
    raw=next(f for f in manifest['raw_files'] if f['source']=='CCDI')
    changed=tmp_path/'CCDI.zip'
    changed_zip('CCDI',raw['local_path'],changed,lambda rows:rows[0].update(resourceParticipantId1='BROKEN'))
    raw.update(local_path=str(changed),sha256=digest(changed),size_bytes=changed.stat().st_size)
    with pytest.raises(ValueError,match='mapping mismatch'):
        verify_raw_files(manifest['raw_files'],manifest['files'])
    target=tmp_path/'manifest.json';write_json(target,manifest)
    with pytest.raises(ValueError,match='mapping mismatch'):
        audit_candidate(RECIPE,target,tmp_path/'audit')


def test_raw_row_count_means_patients_not_mapping_pairs(candidate):
    from jamboree_sdv.promotion_evidence import verify_raw_files
    out,_=candidate;manifest=read_json(out/'release.json')
    raw=next(f for f in manifest['raw_files'] if f['source']=='CCDI')
    raw['rows']+=1
    with pytest.raises(ValueError,match='raw CCDI count'):
        verify_raw_files(manifest['raw_files'],manifest['files'])


def test_promotion_keeps_processed_views_out_of_raw_data_folder(candidate):
    from types import SimpleNamespace
    from jamboree_sdv.promotion import _layout
    out,_=candidate;manifest=read_json(out/'release.json')
    reader=SimpleNamespace(root=WORKSPACE,aliases={},files={
        f['local_path']:f['sha256'] for f in manifest['files']+manifest['raw_files']})
    record={'manifest':manifest,'run_original':str(out/'run/run-manifest.json'),
            'audit_original':str(out/'audit/audit.json'),'manifest_original':str(out/'release.json'),
            'assessment_pin':{'path':str(out/'assessment/assessment.json')},'run':{'archives':{'code':[]}}}
    copies=_layout(reader,record,Path('runs/gen-sdv/release'),Path('data/2026-data-jamboree/release'))
    destinations={f['source']:f['destination'] for f in copies}
    for item in manifest['raw_files']:
        name=item['source']+('.csv' if item['source']=='CPI' else '.zip')
        assert destinations[item['local_path']]=='data/2026-data-jamboree/release/'+name
    for item in manifest['files'][-2:]:
        assert destinations[item['local_path']]=='runs/gen-sdv/release/processed/'+item['source']+'.zip'


def changed_zip(source,path,target,change):
    with zipfile.ZipFile(path) as archive:
        blobs={n:archive.read(n) for n in archive.namelist()}
    reader=csv.DictReader(io.StringIO(blobs[source+'.csv'].decode()));rows=list(reader);change(rows)
    stream=io.StringIO(newline='');writer=csv.DictWriter(stream,fieldnames=reader.fieldnames,lineterminator='\n');writer.writeheader();writer.writerows(rows)
    blobs[source+'.csv']=stream.getvalue().encode()
    with zipfile.ZipFile(target,'x') as archive:
        for n,b in blobs.items():archive.writestr(n,b)


@pytest.mark.parametrize('code',['6','8'])
def test_independent_mce_checker_rejects_invalid_special_codes(candidate,tmp_path,code):
    out,report=candidate;changed=tmp_path/'MCE.zip'
    # Index month permits only 1/7, never 6 or 8, regardless of context.
    changed_zip('MCE',out/'data/MCE.zip',changed,lambda rows:rows[0].update(enrollIndexDxMonth=code))
    sources=deepcopy(report['sources'])
    for s in sources:
        if s['source']=='MCE':s['resolved_path']=str(changed)
    shared_tools();from context_rules import audit_context
    manifest=read_json(out/'release.json')
    result=audit_context(sources,manifest['context'],WORKSPACE/manifest['context_contract']['path'])
    assert result['mce']['status']=='findings'
    assert result['mce']['violations']


def test_context_missing_or_corrupted_is_not_a_pass(candidate,tmp_path):
    out,report=candidate;shared_tools();from context_rules import audit_context
    manifest=read_json(out/'release.json');contract=WORKSPACE/manifest['context_contract']['path']
    assert audit_context(report['sources'],None,contract)['context']['status']=='not_evaluated'
    bad={**manifest['context'],'sha256':'0'*64}
    assert audit_context(report['sources'],bad,contract)['context']['status']=='findings'


@pytest.mark.parametrize('source,change,expected',[
    ('CPI',lambda rows:rows[0].update(resourceParticipantId='BROKEN'),'CPI projection'),
    ('MCD',lambda rows:rows[0].update(nccrId='ORPHAN'),'Orphan patient'),
    ('RO',lambda rows:rows[0].update(months_from_dx_to_tx_start='20',months_from_dx_to_treatment_end='10'),'Radiation start/end'),
])
def test_independent_assessor_rejects_semantic_corruption(candidate,tmp_path,source,change,expected):
    out,_=candidate;changed=tmp_path/(source+'.zip')
    original=next(f['local_path'] for f in read_json(out/'release.json')['files'] if f['source']==source)
    changed_zip(source,original,changed,change)
    manifest=read_json(out/'release.json')
    for f in manifest['files']:
        if f['source']==source:f.update(local_path=str(changed),sha256=digest(changed))
    target=tmp_path/'manifest.json';write_json(target,manifest);shared_tools();from assess_all_source_pilot import assess
    result=assess(target,out/'run',out/'audit/audit.json',RECIPE)
    assert any(expected in f for f in result['findings']),result['findings']


def test_chunk_size_does_not_change_payload_or_context(candidate,tmp_path):
    out,_=candidate;spec=read_json(RECIPE);spec['chunk_size']=13;recipe=tmp_path/'recipe.json';write_json(recipe,spec)
    preflight(recipe,tmp_path/'preflight');result=generate(recipe,tmp_path/'preflight/preflight.json',tmp_path/'run',tmp_path/'data',tmp_path/'release.json')
    original=read_json(out/'run/run-manifest.json')
    assert {f['source']:f['sha256'] for f in result['files']}=={f['source']:f['sha256'] for f in original['files']}
    assert result['context']['sha256']==original['context']['sha256']
