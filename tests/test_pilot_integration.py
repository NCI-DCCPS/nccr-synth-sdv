import csv
import io
import json
import socket
import zipfile
from pathlib import Path
import pytest
from copulas.multivariate import GaussianMultivariate
from jamboree_sdv.common import EXPERIMENT,WORKSPACE,digest,read_json,shared_tools
from jamboree_sdv.preflight import preflight
from jamboree_sdv.writer import generate,zip_info
from jamboree_sdv.audit import audit_candidate
from jamboree_sdv.assessment import assess_candidate
from jamboree_sdv.cli import main


def build(tmp_path,version='v2'):
    recipe=EXPERIMENT/f'recipes/pilot-core-100-{version}-replay.json'
    preflight(recipe,tmp_path/'preflight')
    generate(recipe,tmp_path/'preflight/preflight.json',tmp_path/'run',tmp_path/'data',tmp_path/'manifest.json')
    audit_candidate(recipe,tmp_path/'manifest.json',tmp_path/'audit')
    return recipe


@pytest.mark.parametrize('version',['v1','v2'])
def test_full_core_pipeline_offline_without_fit(tmp_path,monkeypatch,version):
    def forbidden(*args,**kwargs):raise AssertionError('Training/network forbidden')
    monkeypatch.setattr(GaussianMultivariate,'fit',forbidden)
    monkeypatch.setattr(socket,'create_connection',forbidden)
    recipe=build(tmp_path,version)
    result=assess_candidate(recipe,tmp_path/'run',tmp_path/'manifest.json',tmp_path/'audit/audit.json',tmp_path/'assessment')
    assert result['status']=='passed_core_pilot',result['findings']
    assert result['full_release_ready'] is False
    assert result['raw_audit_status']=='findings_or_incomplete'
    assert any(x['status']=='out_of_scope' for x in result['rule_scope_results'])
    assert len(result['coverage'])==128
    assert len(result['annotation_exceptions'])==12
    for entry in read_json(tmp_path/'manifest.json')['files']:
        with zipfile.ZipFile(entry['local_path']) as z:
            assert z.testzip() is None


def test_stale_preflight_refuses_all_outputs(tmp_path):
    recipe=EXPERIMENT/'recipes/pilot-core-100-v2-replay.json'
    preflight(recipe,tmp_path/'preflight')
    p=tmp_path/'preflight/preflight.json';data=read_json(p);data['adapted']['targets']['CTC']=1;p.write_text(json.dumps(data))
    with pytest.raises(ValueError,match='Stale'):
        generate(recipe,p,tmp_path/'run',tmp_path/'data',tmp_path/'manifest.json')
    assert not (tmp_path/'run').exists() and not (tmp_path/'data').exists()


def test_bad_hierarchy_caught_by_independent_audit(tmp_path):
    recipe=build(tmp_path)
    original=read_json(tmp_path/'manifest.json')
    ctc=original['files'][0];path=tmp_path/'bad-CTC.zip'
    with zipfile.ZipFile(ctc['local_path']) as z:
        reader=csv.DictReader(io.StringIO(z.read('CTC.csv').decode()));header=reader.fieldnames;rows=list(reader)
        rows[0]['icccMajor']='01' if rows[0]['icccMajor']!='01' else '02'
        content=io.StringIO(newline='');writer=csv.DictWriter(content,fieldnames=header,lineterminator='\n');writer.writeheader();writer.writerows(rows)
        with zipfile.ZipFile(path,'x') as bad:
            bad.writestr(zip_info('CTC.csv'),content.getvalue())
            bad.writestr(zip_info('ctc_metadata.json'),z.read('ctc_metadata.json'))
    ctc.update(local_path=str(path),sha256=digest(path))
    bad_manifest=tmp_path/'bad-manifest.json';bad_manifest.write_text(json.dumps(original))
    report=audit_candidate(recipe,bad_manifest,tmp_path/'bad-audit')
    hierarchy=next(r for r in report['rule_results'] if r['type']=='hierarchical_lookup')
    assert hierarchy['status']=='findings' and hierarchy['violating_rows']==1


def test_missing_rule_and_stale_hash_cannot_pass_assessment(tmp_path):
    recipe=build(tmp_path)
    report=read_json(tmp_path/'audit/audit.json');report['rule_results']=[r for r in report['rule_results'] if r['type']!='hierarchical_lookup']
    bad=tmp_path/'bad-audit.json';bad.write_text(json.dumps(report))
    result=assess_candidate(recipe,tmp_path/'run',tmp_path/'manifest.json',bad,tmp_path/'assessment-bad')
    assert result['status']=='findings'
    assert any('rule' in s.lower() for s in result['findings'])
    payload=Path(read_json(tmp_path/'manifest.json')['files'][0]['local_path'])
    with payload.open('ab') as stream:stream.write(b'changed')
    result=assess_candidate(recipe,tmp_path/'run',tmp_path/'manifest.json',tmp_path/'audit/audit.json',tmp_path/'assessment-hash')
    assert result['status']=='findings' and any('Payload hash' in s for s in result['findings'])


def test_cli_refuses_shared_outputs_before_writing():
    recipe=str(EXPERIMENT/'recipes/pilot-core-100-v2-replay.json')
    out=WORKSPACE/'runs/gen-sdv-forbidden-test'
    assert not out.exists()
    assert main(['preflight','--recipe',recipe,'--out',str(out)])==2
    assert main(['audit','--recipe',recipe,'--manifest','unused','--out',str(out)])==2
    assert not out.exists()


def test_logical_export_and_zip_bytes_invariant_to_chunk_size(tmp_path):
    recipe=read_json(EXPERIMENT/'recipes/pilot-core-100-v2-replay.json')
    hashes=[]
    for size in [137,10000]:
        directory=tmp_path/str(size);directory.mkdir()
        recipe['chunk_size']=size
        path=directory/'recipe.json';path.write_text(json.dumps(recipe))
        preflight(path,directory/'preflight')
        generate(path,directory/'preflight/preflight.json',directory/'run',directory/'data',directory/'manifest.json')
        hashes.append([r['sha256'] for r in read_json(directory/'manifest.json')['files']])
    assert hashes[0]==hashes[1]


def test_interrupted_writer_retains_failure_without_manifest(tmp_path,monkeypatch):
    from jamboree_sdv.sources import CoreSources
    recipe=EXPERIMENT/'recipes/pilot-core-100-v2-replay.json'
    preflight(recipe,tmp_path/'preflight')
    def broken(self,source):
        raise RuntimeError('simulated write failure')
        yield {}
    monkeypatch.setattr(CoreSources,'rows',broken)
    with pytest.raises(RuntimeError,match='simulated'):
        generate(recipe,tmp_path/'preflight/preflight.json',tmp_path/'run',tmp_path/'data',tmp_path/'manifest.json')
    assert (tmp_path/'run/failure.json').exists()
    assert not (tmp_path/'manifest.json').exists()


def test_orphan_is_rejected_even_with_fresh_raw_audit(tmp_path):
    recipe=build(tmp_path)
    manifest=read_json(tmp_path/'manifest.json');cog=next(f for f in manifest['files'] if f['source']=='COG')
    path=tmp_path/'orphan-COG.zip'
    with zipfile.ZipFile(cog['local_path']) as original:
        reader=csv.DictReader(io.StringIO(original.read('COG.csv').decode()));header=reader.fieldnames;rows=list(reader)
        rows[0]['nccrId']='orphan'
        content=io.StringIO(newline='');writer=csv.DictWriter(content,fieldnames=header,lineterminator='\n');writer.writeheader();writer.writerows(rows)
        with zipfile.ZipFile(path,'x') as bad:
            bad.writestr(zip_info('COG.csv'),content.getvalue());bad.writestr(zip_info('cog_metadata.json'),original.read('cog_metadata.json'))
    cog.update(local_path=str(path),sha256=digest(path))
    bad_manifest=tmp_path/'orphan-manifest.json';bad_manifest.write_text(json.dumps(manifest))
    audit_candidate(recipe,bad_manifest,tmp_path/'orphan-audit')
    result=assess_candidate(recipe,tmp_path/'run',bad_manifest,tmp_path/'orphan-audit/audit.json',tmp_path/'orphan-assessment')
    assert result['status']=='findings'
    assert 'Patient coverage mismatch: COG' in result['findings']


def test_audit_archive_tampering_is_detected(tmp_path):
    recipe=build(tmp_path)
    provenance=read_json(tmp_path/'audit/provenance.json')
    archive=Path(provenance['inputs'][0]['archived_path'])
    assert archive.is_relative_to(tmp_path/'audit/inputs')
    archive.write_text('altered test copy')
    result=assess_candidate(recipe,tmp_path/'run',tmp_path/'manifest.json',tmp_path/'audit/audit.json',tmp_path/'assessment')
    assert result['status']=='findings'
    assert any('Audit archived bytes changed' in f for f in result['findings'])
