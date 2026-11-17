"""Exercise archived ETL methods locally. No AWS clients or full pipeline run."""
import argparse
import ast
from collections import Counter, defaultdict
import csv
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from types import SimpleNamespace
from typing import Final, Iterator
import zipfile

FILES = {
    'verifier': 'etl/runtime/src/glue_jobs/raw_data_validate_job/ims_dataset_verifier.py',
    'constants': 'etl/runtime/src/config/constants_aws_glue.py',
    'infrastructure': 'etl/infrastructure/etl_glue_construct.py',
    'defaults': 'etl/runtime/lambdas/src/default_values_lambda/handler.py',
    'cpi_defaults': 'etl/runtime/lambdas/src/cpi_default_values_lambda/handler.py',
    'submission': 'etl/runtime/lambdas/src/gen_cpi_submission_file_lambda/handler.py',
    'cpi_reader': 'etl/runtime/lambdas/src/ccdi_cpi_enricher_lambda/handler.py',
}


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def execute(nodes, scope, filename):
    code = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    exec(compile(code, str(filename), 'exec'), scope)


def select_class(tree, name, methods):
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
    # Only replace the cloud-connected base class; method bodies remain unchanged.
    node.bases = []
    node.body = [n for n in node.body if isinstance(n, ast.FunctionDef) and n.name in methods]
    require({n.name for n in node.body} == set(methods), 'Expected ETL methods changed')
    return node


def zip_payload(path):
    with zipfile.ZipFile(path) as archive:
        names = [n for n in archive.namelist() if n.endswith('.csv') and 'config' not in n.lower()]
        require(len(names) == 1, 'Expected one data CSV')
        require(all(Path(n).name == n for n in archive.namelist()), 'Unsafe archive member')
        return archive.read(names[0])


class LocalS3:
    """Only byte reads and upload recording; this object cannot access AWS."""
    def __init__(self, objects):
        self.objects = objects
        self.uploads = []

    def get_object(self, *, Bucket, Key):
        return {'Body': io.BytesIO(self.objects[Key])}

    def upload_file(self, *args):
        self.uploads.append(args)


def platform_revision(platform, pins, provenance=None):
    if provenance is None:
        return subprocess.check_output(['git', '-C', str(platform), 'rev-parse', 'HEAD'], text=True).strip()
    evidence = json.loads(provenance.read_bytes())
    require(evidence['status'] == 'passed_etl_delivery_compatibility', 'Unqualified platform archive')
    require(evidence['platform_code'] == pins, 'Archived platform code differs from provenance')
    head = evidence['platform_head']
    require(isinstance(head, str) and len(head) == 40 and all(c in '0123456789abcdef' for c in head), 'Invalid archived platform revision')
    return head


def check(record_path, platform, out, platform_provenance=None):
    out.mkdir(parents=True, exist_ok=False)
    record = json.loads(record_path.read_bytes())
    snapshot = out / 'platform'
    pins = []
    for name, relative in FILES.items():
        src = platform / relative
        dst = snapshot / relative
        dst.parent.mkdir(parents=True, exist_ok=True)
        with dst.open('xb') as stream:
            stream.write(src.read_bytes())
        pins.append({'path': relative, 'sha256': sha(dst)})
    shutil.copyfile(__file__, out / 'check_etl_delivery.py')
    shutil.copyfile(record_path, out / 'delivery-input.json')
    trees = {k: ast.parse((snapshot / p).read_text()) for k, p in FILES.items()}
    sources = next(ast.literal_eval(n.value) for n in trees['constants'].body
                   if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
                   and n.target.id == 'ETL_DATA_SOURCES')
    scope = dict(os=os, csv=csv, shutil=shutil, tempfile=tempfile, zipfile=zipfile,
                 Final=Final, ETL_DATA_SOURCES=sources)
    execute([select_class(trees['verifier'], 'ImsDatasetVerifier',
                          ['__init__', 'unzip_file', '_flatten_ccdi_csv', 'copy_files'])],
            scope, FILES['verifier'])
    verifier = scope['ImsDatasetVerifier']
    helpers = []
    for source, function in [('infrastructure', 'zip2csv'), ('defaults', 'cvt_zip_to_csv_name'),
                             ('cpi_defaults', 'cvt_zip_to_csv_name')]:
        node = next(n for n in trees[source].body if isinstance(n, ast.FunctionDef) and n.name == function)
        ns = {'os': os}
        execute([node], ns, FILES[source])
        helpers.append(ns[function])
    files = {f['source']: f for f in record['files']}
    require(set(files) == {s.upper() for s in sources} | {'CPI'}, 'Incomplete source inventory')
    for item in record['files']:
        require(sha(item['local_path']) == item['sha256'], 'Delivery hash mismatch')
        require(sha(item['original']['path']) == item['original']['sha256'], 'Original hash mismatch')
    source_ccdi = list(csv.reader(io.StringIO(zip_payload(files['CCDI']['original']['path']).decode())))[1:]
    expected = Counter(tuple(r) for r in source_ccdi)
    results = {'filename_checks': 0, 'raw_zip_unpack_checks': 0}
    cwd, old_temp = Path.cwd(), tempfile.tempdir
    try:
        with tempfile.TemporaryDirectory(prefix='etl-check-', dir=out) as scratch:
            tempfile.tempdir = scratch
            for source in sources:
                item = files[source.upper()]
                key = item['s3_key']
                require(Path(key).name.count('.') == 1, 'Expected only the extension dot')
                require(Path(key).suffix == '.zip', 'Raw source must use lowercase .zip')
                job = verifier('local-raw', key, 'local-clean')
                require(job._type == source, 'Filename misclassified by ETL')
                clean_key = key[:-4] + '.csv'
                require(all(helper(key) == clean_key for helper in helpers), 'ETL filename conversion mismatch')
                job._s3 = LocalS3({})
                job.copy_files(key, source + '.csv', None)
                require(job._s3.uploads == [(source + '.csv', 'local-clean', clean_key)], 'ETL clean key mismatch')
                results['filename_checks'] += 1
                directory = Path(scratch) / source
                directory.mkdir()
                os.chdir(directory)
                zip_payload(item['local_path'])  # Reject unsafe members before extractall.
                csv_name, _ = job.unzip_file(item['local_path'])
                results['raw_zip_unpack_checks'] += 1
                if source == 'ccdi':
                    with open(csv_name, newline='') as stream:
                        wide_rows = list(csv.reader(stream))
                    ids = [row[0].lower() for row in wide_rows[1:]]
                    require(len(ids) == len(set(ids)), 'ETL would reject duplicate CCDI patient IDs')
                    job._flatten_ccdi_csv(csv_name)
                    with open(csv_name, newline='') as stream:
                        flattened = list(csv.reader(stream))
                    require(flattened[0] == ['nccrId', 'resourceParticipantId', 'resourceName'], 'Flattened schema mismatch')
                    require(Counter(tuple(r) for r in flattened[1:]) == expected, 'ETL lost or changed CCDI mappings')
                    results.update(ccdi_patient_rows=len(ids), ccdi_mapping_rows=len(flattened) - 1)
                os.chdir(cwd)
            # Run the CPI submission ZIP reader against local bytes as well.
            logger = logging.getLogger('local-etl-delivery-check')
            logger.disabled = True
            ns = dict(Counter=Counter, csv=csv, io=io, zipfile=zipfile, logger=logger)
            execute([select_class(trees['submission'], 'GenCPISubmissionFile', ['_read_mapping_file'])],
                    ns, FILES['submission'])
            reader = ns['GenCPISubmissionFile']()
            reader._s3 = LocalS3({'ccdi': Path(files['CCDI']['local_path']).read_bytes()})
            reader._raw_bucket, reader._ccdi_raw_path, reader._result = 'local', 'ccdi', {}
            submitted = reader._read_mapping_file()
            require(submitted == [row[1:] for row in wide_rows], 'CPI submission reader changed mapping pairs')
            results['cpi_submission_patient_rows'] = len(submitted) - 1
            # Execute the unchanged mock-CPI parsing block with an in-memory S3 stub.
            handler = next(n for n in trees['cpi_reader'].body
                           if isinstance(n, ast.FunctionDef) and n.name == 'lambda_handler')
            block = next(n for n in handler.body if isinstance(n, ast.If)
                         and isinstance(n.test, ast.Name) and n.test.id == 'mock_api_calls')
            cpi_bytes = Path(files['CPI']['local_path']).read_bytes()
            require(cpi_bytes == zip_payload(files['CPI']['original']['path']), 'CPI bytes changed')
            stub = LocalS3({'cpi': cpi_bytes})
            ns = dict(mock_api_calls=True, logger=logger, raw_bucket='local', mock_api_file='cpi',
                      boto3=SimpleNamespace(client=lambda service: stub), defaultdict=defaultdict,
                      csv=csv, io=io, Iterator=Iterator, threading=threading, job=SimpleNamespace())
            execute([block], ns, FILES['cpi_reader'])
            actual = {(patient, *pair) for patient, pairs in ns['nccr_id_to_mappings'].items() for pair in pairs}
            require(actual == set(expected), 'ETL CPI reader mapping mismatch')
            results['cpi_mapping_rows'] = len(actual)
    finally:
        os.chdir(cwd)
        tempfile.tempdir = old_temp
    for item in pins:
        require(sha(platform / item['path']) == item['sha256'], 'ETL code changed during verification')
    result = {'status': 'passed_etl_delivery_compatibility', 'checks': results,
              'delivery': {'path': str(record_path), 'sha256': sha(record_path)},
              'platform_worktree': str(platform), 'platform_code': pins,
              'checker_sha256': sha(__file__),
              'scope': 'Isolated live ETL methods with local S3 stubs; Spark uniqueness checked locally.',
              'not_run': ['Full Glue/Spark ETL', 'AWS upload', 'Deployment', 'External CPI API'],
              'platform_head': platform_revision(platform, pins, platform_provenance)}
    if platform_provenance is not None:
        result['platform_revision_provenance'] = {'path': str(platform_provenance), 'sha256': sha(platform_provenance)}
    with (out / 'assessment.json').open('x') as stream:
        json.dump(result, stream, indent=2)
        stream.write('\n')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--record', required=True, type=Path)
    parser.add_argument('--platform', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--platform-provenance', type=Path, help='Passing assessment pinning archived platform code and its original Git revision')
    args = parser.parse_args()
    if args.out.exists() or args.out.is_symlink():
        raise ValueError('Refusing existing assessment destination: ' + str(args.out))
    try:
        result = check(args.record.resolve(), args.platform.resolve(), args.out.absolute(), args.platform_provenance)
        print(json.dumps({'status': result['status'], 'checks': result['checks']}))
    except Exception as exc:
        if args.out.is_dir() and not (args.out / 'failure.json').exists():
            with (args.out / 'failure.json').open('x') as stream:
                json.dump({'status': 'failed_etl_delivery_compatibility', 'error': str(exc)}, stream, indent=2)
        raise
