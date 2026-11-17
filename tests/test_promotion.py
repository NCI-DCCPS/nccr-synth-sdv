"""Promotion boundary, immutable copies and independent read isolation."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import pytest
from jamboree_sdv import promotion
from jamboree_sdv.common import EXPERIMENT, WORKSPACE
from jamboree_sdv.promotion_evidence import LocalEvidence, SharedEvidence, sha


def test_copy_refuses_existing_and_symlink_destinations(tmp_path):
    source=tmp_path/'source';source.write_bytes(b'original')
    target=tmp_path/'target';target.write_bytes(b'user content')
    with pytest.raises(ValueError,match='existing'):promotion.copy_checked(source,target,sha(source))
    assert target.read_bytes()==b'user content'
    directory=tmp_path/'redirect';directory.symlink_to(tmp_path,target_is_directory=True)
    with pytest.raises(ValueError,match='symlink'):promotion.copy_checked(source,directory/'new',sha(source))


def test_copy_failure_retains_identifiable_partial_copy(tmp_path,monkeypatch):
    source=tmp_path/'source';source.write_bytes(b'original');target=tmp_path/'partial'
    monkeypatch.setattr(promotion.shutil,'copyfileobj',lambda src,dst:dst.write(b'incomplete'))
    with pytest.raises(ValueError,match='hash mismatch'):promotion.copy_checked(source,target,sha(source))
    assert source.read_bytes()==b'original' and target.read_bytes()==b'incomplete'


@pytest.mark.parametrize('release_id',['../escape','/absolute','a/b','', '.hidden'])
def test_unsafe_release_ids_fail_before_reading_candidate(tmp_path,release_id):
    with pytest.raises(ValueError,match='Unsafe release ID'):
        promotion.promote(tmp_path/'missing-assessment',release_id,workspace=tmp_path)


def test_existing_release_is_never_replaced(tmp_path):
    directory=tmp_path/'releases';directory.mkdir();manifest=directory/'example.json';manifest.write_text('user release')
    with pytest.raises(ValueError,match='existing destination'):
        promotion.promote(tmp_path/'missing-assessment','example',workspace=tmp_path)
    assert manifest.read_text()=='user release'


def test_local_evidence_cannot_read_live_shared_definitions(tmp_path):
    p=tmp_path/'reference/input.json';p.parent.mkdir();p.write_text('{}')
    with pytest.raises(ValueError,match='outside gen-sdv work'):
        LocalEvidence(tmp_path).path(p,sha(p))


def test_shared_resolver_checks_hashes_and_never_falls_back(tmp_path):
    copied=tmp_path/'runs/copied.json';copied.parent.mkdir();copied.write_text('{}')
    record={'original_workspace':'/unavailable/original','copies':[
        {'destination':'runs/copied.json','sha256':sha(copied),'original_paths':['/unavailable/original/experiments/gen-sdv/work/record.json']} ]}
    reader=SharedEvidence(tmp_path,record)
    assert reader.path('experiments/gen-sdv/work/record.json',sha(copied))==copied
    with pytest.raises(ValueError,match='not copied'):reader.path('missing.json','0'*64)
    broken=deepcopy(record);broken['copies'][0]['sha256']='0'*64
    with pytest.raises(ValueError,match='hash mismatch'):SharedEvidence(tmp_path,broken)
    escaped=deepcopy(record);escaped['copies'][0]['destination']='../escape'
    with pytest.raises(ValueError,match='escape'):SharedEvidence(tmp_path,escaped)


def test_promote_copy_failure_never_publishes_manifest(tmp_path,monkeypatch):
    from types import SimpleNamespace
    source=tmp_path/'source';source.write_text('payload')
    record={'assessment_pin':{'path':'candidate','sha256':'a'*64},'manifest':{'files':[]}}
    monkeypatch.setattr(promotion,'collect',lambda p:(SimpleNamespace(root=tmp_path),record))
    monkeypatch.setattr(promotion,'_layout',lambda *args:[{'source':str(source),'destination':'runs/partial','sha256':sha(source),'original_paths':[]}])
    def interrupt(*args):raise OSError('injected copy interruption')
    monkeypatch.setattr(promotion,'copy_checked',interrupt)
    with pytest.raises(OSError,match='interruption'):promotion.promote(source,'test-release',workspace=tmp_path)
    assert not (tmp_path/'releases/test-release.json').exists()
    failures=list((tmp_path/'runs/gen-sdv').glob('.promotion-*/failure.json'))
    assert len(failures)==1 and json.loads(failures[0].read_text())['status']=='incomplete_promotion'
    assert source.read_text()=='payload'


def test_final_boundary_blocks_original_work_and_live_tools(tmp_path):
    shared=tmp_path/'runs/copied';shared.mkdir(parents=True);allowed=shared/'data';allowed.write_text('shared')
    forbidden=tmp_path/'experiments/gen-sdv/work/data';forbidden.parent.mkdir(parents=True);forbidden.write_text('local')
    script=EXPERIMENT/'verifier/verify_promoted.py'
    code='''import importlib.util,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('verify',sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
root=Path(sys.argv[2]);shared=root/'runs/copied'
m.install_boundary(root,root,[shared])
assert (shared/'data').read_text()=='shared'
for p in [root/'experiments/gen-sdv/work/data',root/'tools/live.py',root/'reference/live.json']:
 try:p.read_text()
 except PermissionError:pass
 else:raise AssertionError('original path read was allowed')
'''
    result=subprocess.run([sys.executable,'-I','-S','-B','-c',code,str(script),str(tmp_path)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr


def test_operational_snapshot_and_input_inventory_are_bound(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from jamboree_sdv import promotion_evidence
    monkeypatch.setitem(sys.modules,'promotion_evidence',promotion_evidence)
    spec=importlib.util.spec_from_file_location('verify_promoted_test',EXPERIMENT/'verifier/verify_promoted.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    snapshot=tmp_path/'snapshot.json';snapshot.write_text('{}')
    original_pin={'path':'reference/snapshot.json','sha256':sha(snapshot)}
    reader=SimpleNamespace(path=lambda name,digest:snapshot)
    original={'recipe':{'snapshot':original_pin},'run':{'archives':{'inputs':[original_pin]}}}
    run={'snapshot':{'path':str(snapshot),'sha256':sha(snapshot)},'inputs':[{'path':str(snapshot),'sha256':sha(snapshot)}]}
    module.verify_operational_inputs(run,original,reader)
    altered=deepcopy(run);altered['inputs']=[]
    with pytest.raises(ValueError,match='inventory mismatch'):module.verify_operational_inputs(altered,original,reader)
    other=tmp_path/'other.json';other.write_text('{}')
    altered=deepcopy(run);altered['snapshot']['path']=str(other)
    with pytest.raises(ValueError,match='snapshot pin mismatch'):module.verify_operational_inputs(altered,original,reader)
    snapshot.write_text('{"tampered":true}')
    with pytest.raises(ValueError,match='snapshot pin mismatch'):module.verify_operational_inputs(run,original,reader)
