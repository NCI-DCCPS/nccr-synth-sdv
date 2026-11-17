"""Run unchanged PHARM ETL ZIP/graph methods locally, with SQLite and no AWS."""
import ast
import csv
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
from types import SimpleNamespace
from typing import Any, Final
import zipfile


def extract(path, classname, methods, scope):
    node = next(n for n in ast.parse(path.read_text()).body if isinstance(n, ast.ClassDef) and n.name == classname)
    node.bases = []
    node.body = [n for n in node.body if isinstance(n, ast.FunctionDef) and n.name in methods]
    if {n.name for n in node.body} != set(methods):
        raise ValueError('ETL method inventory changed')
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), str(path), 'exec'), scope)
    return scope[classname]


def verify(payload, platform, out):
    out.mkdir(parents=True, exist_ok=False)
    sources = {'graph': 'etl/runtime/lambdas/src/gen_dq_graphs_pharm_lambda/handler.py',
               'raw': 'etl/runtime/src/glue_jobs/raw_data_validate_job/ims_dataset_verifier.py',
               'constants': 'etl/runtime/src/config/constants_aws_glue.py'}
    pins = {}
    for name, relative in sources.items():
        target = out / (name + '.py')
        shutil.copyfile(platform / relative, target)
        pins[relative] = hashlib.sha256(target.read_bytes()).hexdigest()
    constants = ast.parse((out / 'constants.py').read_text())
    supported = next(ast.literal_eval(n.value) for n in constants.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == 'ETL_DATA_SOURCES')
    scope = dict(os=os, csv=csv, shutil=shutil, tempfile=tempfile, zipfile=zipfile, Final=Final, ETL_DATA_SOURCES=supported)
    raw = extract(out / 'raw.py', 'ImsDatasetVerifier', ['__init__', 'unzip_file'], scope)
    cwd = Path.cwd()
    with tempfile.TemporaryDirectory(dir=out) as scratch:
        try:
            os.chdir(scratch)
            job = raw('local-raw', 'candidate/PHARM.zip', 'local-clean')
            if job._type != 'pharm': raise ValueError('Filename misclassified')
            csv_path, _ = job.unzip_file(str(payload))
            csv_bytes = Path(csv_path).read_bytes()
        finally:
            os.chdir(cwd)
    with zipfile.ZipFile(payload) as z:
        if csv_bytes != z.read('PHARM.csv'): raise ValueError('ETL extraction changed data')
    reader = csv.reader(io.StringIO(csv_bytes.decode()))
    header = next(reader); rows = list(reader)
    connection = sqlite3.connect(':memory:')
    connection.create_function('concat', -1, lambda *values: ''.join(map(str, values)))
    connection.execute('CREATE TABLE pharm (' + ','.join('"' + f + '" TEXT' for f in header) + ')')
    connection.executemany('INSERT INTO pharm VALUES (' + ','.join('?' for _ in header) + ')', rows)
    class DB:
        def execute(self, sql, row_type=None):
            self.dictionary = row_type == 'DICT'
            self.cursor = connection.execute(sql)
        def fetchall(self): return self.cursor.fetchall()
        def fetchone(self):
            row = self.cursor.fetchone()
            return dict(zip([c[0] for c in self.cursor.description], row)) if self.dictionary else row
    scope = dict(Any=Any, time=time, logger=logging.getLogger('pharm-local-check'), Athena=SimpleNamespace(DICT='DICT'))
    cls = extract(out / 'graph.py', 'GenPharmDqGraphs', ['_create_chart_bar', 'gen_dict_for_simple_field', 'gen_dict_for_ndc_code'], scope)
    job = cls(); job._input_table = 'pharm'; job._db = DB()
    graphs = [job.gen_dict_for_ndc_code('canmedNonProprietaryName'), job.gen_dict_for_ndc_code('rxnormDisplayName', data_type='rxnorm')]
    for field in ['canmedDrugCategory', 'canmedMajorDrugClass', 'canmedMinorDrugClass', 'rxnormAtcprodClass']:
        graphs.append(job.gen_dict_for_simple_field(field, field, force_count_sort=True))
    if any(g['emptyCounts'] or not g['charts'][0]['values'] for g in graphs):
        raise ValueError('Empty drug graph data')
    connection.close()
    for relative, expected in pins.items():
        if hashlib.sha256((platform / relative).read_bytes()).hexdigest() != expected:
            raise ValueError('Platform code changed during local check')
    result = {'status': 'passed_local_pharm_etl_methods', 'rows': len(rows), 'filename_type': 'pharm',
              'zip_extraction_byte_identical': True, 'graph_methods_exercised': 6,
              'graphs': graphs, 'platform_code': pins,
              'limits': ['SQLite executes graph SQL locally; Athena, Glue/Spark and full cloud pipeline not run.',
                         'Quantity, duration and offset graphs rely on preserved baseline values.']}
    (out / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    return result
