"""Lossless ETL delivery copies of a verified release; no upload or deployment."""
from collections import Counter
from datetime import date
from decimal import Decimal
import hashlib
import io
import csv
from pathlib import Path
import re
import shutil
import zipfile

from .common import WORKSPACE, digest, now, read_json, write_json
from .promotion_evidence import SOURCES, require, verify_raw_files

MAPPING_HEADER = ['nccrId', 'resourceParticipantId', 'resourceName']


def pin(path):
    return {'path': str(Path(path).absolute()), 'sha256': digest(path)}


def read_mappings(payload):
    rows = list(csv.reader(io.StringIO(payload.decode('utf-8'), newline='')))
    require(bool(rows) and rows[0] == MAPPING_HEADER, 'Unexpected mapping CSV header')
    records = rows[1:]
    require(bool(records), 'Empty mapping input is unsupported by the ETL')
    require(all(len(row) == 3 and all(row) for row in records), 'Incomplete mapping row')
    return [tuple(row) for row in records]


def wide_ccdi(payload):
    """Keep all mappings and leading zeros; only group rows and pad empty pairs."""
    records = read_mappings(payload)
    patients = {}
    case_keys = {}
    for patient, participant, resource in records:
        require(case_keys.setdefault(patient.lower(), patient) == patient,
                'Patient IDs collide under the ETL case-insensitive comparison')
        patients.setdefault(patient, []).append((participant, resource))
    width = max(map(len, patients.values()))
    output = io.StringIO(newline='')
    writer = csv.writer(output, quoting=csv.QUOTE_ALL, lineterminator='\n')
    writer.writerow(['nccrId'] + [f'{column}{i}' for i in range(1, width + 1)
                                for column in MAPPING_HEADER[1:]])
    for patient, mappings in patients.items():
        writer.writerow([patient] + [value for pair in mappings for value in pair]
                        + [''] * (2 * (width - len(mappings))))
    return output.getvalue().encode('utf-8')


def flatten_delivery(payload):
    """Independently check the wide representation, including empty pair slots."""
    rows = list(csv.reader(io.StringIO(payload.decode('utf-8'), newline='')))
    require(bool(rows) and len(rows[0]) >= 3 and len(rows[0]) % 2 == 1,
            'Invalid wide CCDI column count')
    width = (len(rows[0]) - 1) // 2
    expected = ['nccrId'] + [f'{c}{i}' for i in range(1, width + 1) for c in MAPPING_HEADER[1:]]
    require(rows[0] == expected, 'Invalid wide CCDI headers')
    seen, mappings = set(), []
    for row in rows[1:]:
        require(len(row) == len(expected) and bool(row[0]), 'Invalid wide CCDI row')
        require(row[0].lower() not in seen, 'Duplicate CCDI patient row')
        seen.add(row[0].lower())
        empty = False
        for i in range(1, len(row), 2):
            pair = row[i:i + 2]
            if pair == ['', '']:
                require(i != 1, 'Missing first CCDI mapping')
                empty = True
            else:
                require(all(pair) and not empty, 'Incomplete or noncontiguous CCDI mapping')
                mappings.append((row[0], *pair))
    return mappings, len(seen), width


def zip_csv(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)), 'Duplicate ZIP members')
        require(all(Path(n).name == n for n in names), 'Nested ZIP members are unsupported')
        require(archive.testzip() is None, 'ZIP CRC failure')
        data = [n for n in names if n.endswith('.csv') and 'config' not in n.lower()]
        require(len(data) == 1, 'Expected exactly one data CSV in ZIP')
        return data[0], archive.read(data[0])


def load_release(manifest_path, workspace):
    manifest_path = Path(manifest_path).absolute()
    require(manifest_path.parent == workspace / 'releases' and
            manifest_path.resolve() == manifest_path, 'Expected a shared release manifest')
    manifest = read_json(manifest_path)
    require(manifest['kind'] == 'gen_sdv_promoted_release', 'Expected a promoted gen-sdv release')
    require([f['source'] for f in manifest['files']] == SOURCES, 'Incomplete source inventory')
    assessment_path = Path(manifest['verification']['assessment_path'])
    require(assessment_path.resolve() == assessment_path and
            assessment_path.is_relative_to(workspace / 'runs/validation'), 'Unsafe assessment path')
    assessment = read_json(assessment_path)
    require(assessment['status'] == 'passed_shared_release' and not assessment['findings'] and
            assessment['release_id'] == manifest['release_id'] and
            assessment['bindings']['manifest'] == pin(manifest_path),
            'Release assessment does not bind this passing release')
    for key, item in [('run', manifest['run_manifest']), ('promotion', manifest['promotion'])]:
        p = Path(item['path'])
        require(p.resolve() == p and p.is_relative_to(workspace / 'runs/gen-sdv') and
                pin(p) == item and assessment['bindings'][key] == item, 'Release provenance mismatch')
    for item in manifest['files']:
        path = Path(item['local_path'])
        boundary = (Path(manifest['run_manifest']['path']).parent / 'processed'
                    if 'raw_files' in manifest and item['source'] in ('CCDI','CPI')
                    else workspace / 'data/2026-data-jamboree')
        require(path.resolve() == path and path.is_relative_to(boundary),
                'Unsafe release payload path')
        require(digest(path) == item['sha256'] and path.stat().st_size == item['size_bytes'],
                'Release payload hash/size mismatch: ' + item['source'])
    if 'raw_files' in manifest:
        require(verify_raw_files(manifest['raw_files'], manifest['files']) == assessment['raw_layout_checks'],
                'Release raw layout assessment mismatch')
    return manifest, assessment_path


def delivery_label(manifest):
    version = manifest['rules_version']
    require(bool(re.fullmatch(r'\d+\.\d+\.\d+', version)), 'Unsupported rule version for delivery naming')
    require(manifest['size']['kind'] == 'scale', 'Delivery expects a promoted scale release')
    scale = Decimal(manifest['size']['value'])
    require(scale.is_finite() and 0 < scale <= 1, 'Invalid delivery scale')
    percent = format((scale * 100).normalize(), 'f').replace('.', 'p')
    return f'jamboree_sdv_rules_v{version.replace(".", "_")}_scale_{percent}pct'


def verify_delivery(record):
    """Read the produced copies back; this does not certify a full AWS ETL run."""
    release_path = Path(record['source_release']['path'])
    require(pin(release_path) == record['source_release'], 'Source release changed')
    manifest, assessment_path = load_release(release_path, release_path.parent.parent)
    require(pin(assessment_path) == record['source_assessment'], 'Source assessment changed')
    original = {f['source']: f for f in manifest['files']}
    require([f['source'] for f in record['files']] == SOURCES, 'Incomplete delivery inventory')
    for f in record['files']:
        path = Path(f['local_path'])
        require(digest(path) == f['sha256'] and path.stat().st_size == f['size_bytes'],
                'Delivery payload hash/size mismatch: ' + f['source'])
        require(f['original'] == {'path': original[f['source']]['local_path'],
                                 'sha256': original[f['source']]['sha256']}, 'Original payload pin mismatch')
        if f['source'] not in ('CCDI', 'CPI'):
            require(f['sha256'] == original[f['source']]['sha256'], 'Unchanged raw ZIP was altered')
            zip_csv(path)
    by_source = {f['source']: f for f in record['files']}
    _, ccdi = zip_csv(original['CCDI']['local_path'])
    _, wide = zip_csv(by_source['CCDI']['local_path'])
    triples, patients, width = flatten_delivery(wide)
    require(Counter(triples) == Counter(read_mappings(ccdi)), 'CCDI mapping round-trip mismatch')
    with zipfile.ZipFile(original['CCDI']['local_path']) as source, \
            zipfile.ZipFile(by_source['CCDI']['local_path']) as target:
        require(source.namelist() == target.namelist(), 'CCDI ZIP member inventory changed')
        for name in source.namelist():
            if not name.endswith('.csv'):
                require(source.read(name) == target.read(name), 'CCDI metadata changed')
    member, cpi = zip_csv(original['CPI']['local_path'])
    require(Path(by_source['CPI']['local_path']).read_bytes() == cpi, 'CPI extraction changed bytes')
    cpi_records = read_mappings(cpi)
    require(Counter(cpi_records) == Counter(set(triples)), 'CPI does not equal distinct CCDI mappings')
    return {'status': 'passed_lossless_delivery_checks', 'unchanged_raw_zips': 8,
            'ccdi_patient_rows': patients, 'ccdi_mapping_rows': len(triples),
            'ccdi_mapping_pairs_per_row': width, 'cpi_rows': len(cpi_records),
            'cpi_member': member, 'cpi_member_sha256': hashlib.sha256(cpi).hexdigest(),
            'full_etl_run': False}


def prepare_delivery(manifest_path, out, delivery_date=None, workspace=WORKSPACE, *, package_id=None):
    workspace = Path(workspace).resolve()
    out = Path(out).absolute()
    require(out.resolve() == out and out.is_relative_to(workspace / 'experiments/gen-sdv/work') and
            out != workspace / 'experiments/gen-sdv/work', 'Delivery staging must be below gen-sdv/work')
    require(not out.exists() and not out.is_symlink(), 'Refusing existing delivery destination')
    if package_id is not None:
        require(bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,119}', package_id)), 'Unsafe package ID')
        require(delivery_date is None, 'Choose a package ID or a source/date layout')
    else:
        require(delivery_date is not None, 'A package ID or delivery date is required')
    day = date.fromisoformat(delivery_date) if delivery_date is not None else None
    manifest, assessment = load_release(manifest_path, workspace)
    label = delivery_label(manifest)
    record = {'schema_version': 1, 'kind': 'gen_sdv_etl_delivery', 'created_at': now(),
              'release_id': manifest['release_id'], 'source_release': pin(manifest_path),
              'source_assessment': pin(assessment), 'delivery_date': day.isoformat() if day else None,
              'package_id': package_id,
              'label': label, 'files': [], 'aws_upload': False, 'deployment': False,
              'notes': ['CCDI metadata describes the post-flattening schema and is preserved verbatim.',
                        'Delivery date selects the existing event folder, not the generation date.']}
    out.mkdir(parents=True)
    try:
        for item in manifest['files']:
            source = item['source']
            if package_id:
                name = source + ('.csv' if source == 'CPI' else '.zip')
                key = f'data/2026-data-jamboree/{package_id}/{name}'
            else:
                name = f'mock_cpi_data_{label}.csv' if source == 'CPI' else f'{source}_{label}.zip'
                key = f'data/2026-data-jamboree/{source}/{day.year}/{day:%m-%d}/{name}'
            target = out / 'payloads' / key
            target.parent.mkdir(parents=True, exist_ok=True)
            original = Path(item['local_path'])
            if source == 'CCDI':
                member, payload = zip_csv(original)
                with zipfile.ZipFile(original) as src, zipfile.ZipFile(target, 'x') as dst:
                    for entry in src.infolist():
                        dst.writestr(entry, wide_ccdi(payload) if entry.filename == member else src.read(entry))
                conversion = 'group_mappings_into_one_patient_row'
            elif source == 'CPI':
                member, payload = zip_csv(original)
                with target.open('xb') as stream:
                    stream.write(payload)
                conversion = 'extract_csv_byte_for_byte'
            else:
                with original.open('rb') as src, target.open('xb') as dst:
                    shutil.copyfileobj(src, dst)
                conversion = 'unchanged_zip'
            record['files'].append({'source': source, 'local_path': str(target), 's3_key': key,
                                    'sha256': digest(target), 'size_bytes': target.stat().st_size,
                                    'original': {'path': str(original), 'sha256': item['sha256']},
                                    'conversion': conversion})
        record['checks'] = verify_delivery(record)
        record['status'] = 'prepared_etl_delivery'
        write_json(out / 'delivery.json', record)
    except Exception as exc:
        write_json(out / 'failure.json', {'status': 'incomplete_etl_delivery', 'error': str(exc)})
        raise
    return record
