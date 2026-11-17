"""Delivery regressions: lossless grouping, CPI extraction and immutable inputs."""
from collections import Counter
import csv
import io
import json
from pathlib import Path
import zipfile

import pytest

from jamboree_sdv import delivery


def csv_bytes(rows):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, lineterminator='\n')
    writer.writerows([delivery.MAPPING_HEADER, *rows])
    return stream.getvalue().encode()


def test_wide_ccdi_preserves_multiplicity_strings_quotes_and_variable_width():
    rows = [('0002', '001', 'A, B'), ('0001', '002', '"quoted"'),
            ('0002', '003', 'C'), ('0002', '001', 'A, B')]
    result = delivery.wide_ccdi(csv_bytes(rows))
    restored, patients, width = delivery.flatten_delivery(result)
    assert Counter(restored) == Counter(rows)
    assert (patients, width) == (2, 3)
    assert result == delivery.wide_ccdi(csv_bytes(rows))
    parsed = list(csv.reader(io.StringIO(result.decode())))
    assert parsed[2] == ['0001', '002', '"quoted"', '', '', '', '']


@pytest.mark.parametrize('payload,message', [
    (b'nccrId,resourceParticipantId,resourceName\n', 'Empty'),
    (b'nccrId,resourceParticipantId,resourceName\na,b,\n', 'Incomplete'),
    (b'nccrId,resourceParticipantId,resourceName\na,b,c,d\n', 'Incomplete'),
    (b'nccrId,resourceName,resourceParticipantId\na,b,c\n', 'header'),
    (csv_bytes([('ABC', '001', 'R'), ('abc', '002', 'R')]), 'collide'),
])
def test_invalid_mapping_input_is_rejected(payload, message):
    with pytest.raises(ValueError, match=message):
        delivery.wide_ccdi(payload)


def fixture_release(root):
    manifest_path = root / 'releases/example.json'
    manifest_path.parent.mkdir()
    run = root / 'runs/gen-sdv/example'
    run.mkdir(parents=True)
    (run / 'manifest.json').write_text('{}')
    (run / 'promotion.json').write_text('{}')
    assessment_path = root / 'runs/validation/example/assessment.json'
    assessment_path.parent.mkdir(parents=True)
    data = root / 'data/2026-data-jamboree/example'
    data.mkdir(parents=True)
    mappings = csv_bytes([('0001', '001', 'R'), ('0001', '002', 'S'), ('0002', '003', 'R')])
    files = []
    for source in delivery.SOURCES:
        path = data / (source + '.zip')
        with zipfile.ZipFile(path, 'x') as z:
            z.writestr(source + '.csv', mappings if source in ('CCDI', 'CPI') else b'nccrId\n0001\n')
            z.writestr(source.lower() + '_metadata.json', '{}')
        files.append({'source': source, 'local_path': str(path),
                      'sha256': delivery.digest(path), 'size_bytes': path.stat().st_size})
    manifest = {'kind': 'gen_sdv_promoted_release', 'release_id': 'example',
                'rules_version': '2.1.0', 'size': {'kind': 'scale', 'value': '0.01'},
                'files': files, 'verification': {'assessment_path': str(assessment_path)},
                'run_manifest': delivery.pin(run / 'manifest.json'),
                'promotion': delivery.pin(run / 'promotion.json')}
    manifest_path.write_text(json.dumps(manifest))
    assessment_path.write_text(json.dumps({'status': 'passed_shared_release', 'findings': [],
        'release_id': 'example', 'bindings': {'manifest': delivery.pin(manifest_path),
        'run': manifest['run_manifest'], 'promotion': manifest['promotion']}}))
    return manifest_path, root / 'experiments/gen-sdv/work/delivery'


def test_complete_delivery_and_readback_preserve_inputs_and_refuse_reuse(tmp_path):
    manifest, out = fixture_release(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    record = delivery.prepare_delivery(manifest, out, '2026-11-16', workspace=tmp_path)
    assert record['checks']['ccdi_patient_rows'] == 2
    assert record['checks']['ccdi_mapping_rows'] == record['checks']['cpi_rows'] == 3
    assert delivery.verify_delivery(record) == record['checks']
    assert all(p.read_bytes() == data for p, data in before.items())
    assert len(record['files']) == 10
    for f in record['files']:
        assert Path(f['s3_key']).name.count('.') == 1
        assert f"/{f['source']}/2026/11-16/" in f['s3_key']
    with pytest.raises(ValueError, match='existing'):
        delivery.prepare_delivery(manifest, out, '2026-11-16', workspace=tmp_path)
    cpi = next(f for f in record['files'] if f['source'] == 'CPI')
    Path(cpi['local_path']).write_text('corrupted')
    with pytest.raises(ValueError, match='hash/size mismatch'):
        delivery.verify_delivery(record)


def test_stale_assessment_and_tampered_original_are_rejected(tmp_path):
    manifest, out = fixture_release(tmp_path)
    data = json.loads(manifest.read_text())
    manifest.write_text(json.dumps(data) + '\n')
    with pytest.raises(ValueError, match='assessment'):
        delivery.prepare_delivery(manifest, out, '2026-11-16', workspace=tmp_path)
    assert not out.exists()
    manifest.write_text(json.dumps(data))
    Path(data['files'][0]['local_path']).write_text('changed')
    with pytest.raises(ValueError, match='hash/size mismatch'):
        delivery.prepare_delivery(manifest, out, '2026-11-16', workspace=tmp_path)
    assert not out.exists()


def test_incomplete_conversion_keeps_failure_evidence_without_success_manifest(tmp_path, monkeypatch):
    manifest, out = fixture_release(tmp_path)
    def fail(payload):
        raise ValueError('injected conversion failure')
    monkeypatch.setattr(delivery, 'wide_ccdi', fail)
    with pytest.raises(ValueError, match='injected'):
        delivery.prepare_delivery(manifest, out, '2026-11-16', workspace=tmp_path)
    assert not (out / 'delivery.json').exists()
    assert json.loads((out / 'failure.json').read_text())['status'] == 'incomplete_etl_delivery'


def test_delivery_name_for_fractional_percentage_and_invalid_date(tmp_path):
    manifest, out = fixture_release(tmp_path)
    value = json.loads(manifest.read_text())
    value['size']['value'] = '0.001'
    assert delivery.delivery_label(value) == 'jamboree_sdv_rules_v2_1_0_scale_0p1pct'
    with pytest.raises(ValueError):
        delivery.prepare_delivery(manifest, out, '2026-02-30', workspace=tmp_path)
    assert not out.exists()


def test_wrong_cpi_projection_is_rejected_even_with_updated_delivery_hash(tmp_path):
    manifest, out = fixture_release(tmp_path)
    record = delivery.prepare_delivery(manifest, out, '2026-11-16', workspace=tmp_path)
    item = next(f for f in record['files'] if f['source'] == 'CPI')
    path = Path(item['local_path'])
    path.write_bytes(csv_bytes([('0001', 'wrong', 'R')]))
    item.update(sha256=delivery.digest(path), size_bytes=path.stat().st_size)
    with pytest.raises(ValueError, match='CPI extraction changed bytes'):
        delivery.verify_delivery(record)


def test_incomplete_wide_pair_is_rejected():
    payload = b'nccrId,resourceParticipantId1,resourceName1\n001,002,\n'
    with pytest.raises(ValueError, match='Incomplete'):
        delivery.flatten_delivery(payload)


def test_release_folder_contains_etl_ready_ccdi_under_ordinary_filename(tmp_path):
    manifest, out = fixture_release(tmp_path)
    original = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    record = delivery.prepare_delivery(manifest, out, workspace=tmp_path,
                                       package_id='jamboree_sdv_rules_v2_1_0_scale_1pct_r2')
    files = {f['source']: f for f in record['files']}
    expected_folder = 'data/2026-data-jamboree/jamboree_sdv_rules_v2_1_0_scale_1pct_r2'
    assert {str(Path(f['s3_key']).parent) for f in files.values()} == {expected_folder}
    assert {Path(f['s3_key']).name for f in files.values()} == {
        *(s + '.zip' for s in delivery.SOURCES if s != 'CPI'), 'CPI.csv'}
    _, payload = delivery.zip_csv(files['CCDI']['local_path'])
    rows = list(csv.reader(io.StringIO(payload.decode())))
    assert rows == [
        ['nccrId', 'resourceParticipantId1', 'resourceName1', 'resourceParticipantId2', 'resourceName2'],
        ['0001', '001', 'R', '002', 'S'], ['0002', '003', 'R', '', '']]
    assert delivery.read_mappings(Path(files['CPI']['local_path']).read_bytes()) == [
        ('0001', '001', 'R'), ('0001', '002', 'S'), ('0002', '003', 'R')]
    assert record['checks']['ccdi_patient_rows'] == 2
    assert record['checks']['ccdi_mapping_rows'] == 3
    assert all(p.read_bytes() == data for p, data in original.items())
    assert delivery.verify_delivery(record) == record['checks']


@pytest.mark.parametrize('package_id', ['../escape', 'with.dots', 'with space', '/absolute', ''])
def test_package_id_cannot_escape_or_introduce_unsafe_names(tmp_path, package_id):
    manifest, out = fixture_release(tmp_path)
    with pytest.raises(ValueError, match='package ID'):
        delivery.prepare_delivery(manifest, out, workspace=tmp_path, package_id=package_id)
    assert not out.exists()
