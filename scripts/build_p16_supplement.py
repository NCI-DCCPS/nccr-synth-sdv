#!/usr/bin/env python3
"""Append independently checked 2023 schema/p16 counts to a frozen G1 reference.

Standalone, identifier-free output. Existing bundles are never modified. The
archived builder replays using its retained base bundle and the pinned source.
"""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys

COLS = ['yearOfDiagnosis', 'schemaId', 'p16Recode']
RULE_SHA = '0ab9a6a00706c407fb8528393974b724d27ffa38d2ef83dfdfe491ba2fa198f8'
CHECKER_SHA = 'ea68d321e3cb0ac13eba157f54d5725938ce85fdb83b99e6cf640bdd028f79e2'


def require(ok, message):
    if not ok:
        raise ValueError(message)


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read(path):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'Duplicate JSON key')
            result[key] = value
        return result
    return json.loads(Path(path).read_bytes(), object_pairs_hook=pairs)


def save(path, data):
    with Path(path).open('x') as stream:
        json.dump(data, stream, indent=2, sort_keys=True)
        stream.write('\n')


def member(root, name):
    require(isinstance(name, str) and name not in ('', '.'), 'Invalid archive member')
    p = Path(name)
    require(not p.is_absolute() and '..' not in p.parts and p.as_posix() == name, 'Unsafe archive member')
    result = (root/p).resolve()
    require(result.is_relative_to(root.resolve()) and result != root.resolve(), 'Archive member escapes bundle')
    return result


def verify_bundle(root, expected):
    root = Path(root)
    require(sha(root/'manifest.json') == expected, 'Manifest hash mismatch')
    manifest = read(root/'manifest.json')
    listed = set()
    for item in manifest['files']:
        require(item['path'] not in listed and item['path'] != 'manifest.json', 'Duplicate or recursive inventory')
        listed.add(item['path'])
        path = member(root, item['path'])
        require(path.is_file() and path.stat().st_size == item['size_bytes'] and sha(path) == item['sha256'],
                'Bundle file hash/size mismatch')
    actual = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
    require(actual == listed | {'manifest.json'}, 'Bundle contains unlisted files')
    return manifest


def load_checker(rules_path, checker_path):
    require(sha(rules_path) == RULE_SHA and sha(checker_path) == CHECKER_SHA, 'Unsupported frozen p16 inputs')
    rule = next(r for r in read(rules_path)['rules'] if r['id'] == 'ctc_p16_applicability')
    namespace = {'__name__': 'frozen_p16_checker', '__file__': str(checker_path)}
    exec(compile(Path(checker_path).read_bytes(), str(checker_path), 'exec'), namespace)
    return rule, namespace['P16Check']


def read_counts(path):
    counts = Counter()
    with Path(path).open(newline='') as stream:
        reader = csv.reader(stream, strict=True)
        require(next(reader, []) == COLS + ['count'], 'Unexpected count header')
        previous = None
        for row in reader:
            require(len(row) == 4, 'Malformed count row')
            key, value = tuple(row[:3]), row[3]
            require(value.isascii() and value.isdigit() and str(int(value)) == value and int(value) > 0,
                    'Invalid count')
            require(previous is None or previous < key, 'Duplicate or unsorted count tuple')
            previous = key
            counts[key] = int(value)
    return counts


def aggregate_2023(source, rule, checker_class):
    checker = checker_class(rule)
    counts, years, reasons = Counter(), Counter(), Counter()
    coverage = defaultdict(Counter)
    with Path(source).open(newline='', encoding='utf-8-sig') as stream:
        reader = csv.reader(stream, strict=True)
        header = next(reader, [])
        require(header and len(header) == len(set(header)) and set(COLS) <= set(header), 'Invalid source header')
        positions = [header.index(k) for k in COLS]
        for row in reader:
            require(len(row) == len(header), 'Malformed source row width')
            year, schema, value = (row[i] for i in positions)
            require(len(year) == 4 and year.isascii() and year.isdigit() and '1995' <= year <= '2023',
                    'Invalid source diagnosis year')
            years[year] += 1
            if year != '2023':
                continue
            status, reason = checker.add(dict(zip(COLS, (year, schema, value))))
            reasons[(status, reason)] += 1
            coverage[(year, schema)][status] += 1
            if status == 'passed':
                counts[(year, schema, value)] += 1
    return {'tuples': counts, 'year_counts': years, 'reasons': reasons, 'coverage': dict(coverage)}


def write_csv(path, header, rows):
    with Path(path).open('x', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(header)
        writer.writerows(rows)


def validate_against_base(base, result, legacy, rule, checker_class):
    support = read(base/'support.json')['observed_marginals']['yearOfDiagnosis']
    require(dict(result['year_counts']) == support, 'Diagnosis year marginals differ from frozen base')
    report = read(base/'p16_report.json')
    def reasons(section):
        rows = report[section]['reasons']
        require(len({(r['status'], r['reason']) for r in rows}) == len(rows), 'Duplicate reason record')
        return Counter({(r['status'], r['reason']): r['count'] for r in rows})
    all_reasons, old_reasons = reasons('all_source'), reasons('through2022')
    require(all_reasons == old_reasons + result['reasons'], 'p16 reason counts differ from frozen base')
    for label, key in [('passed', 'passed_rows'), ('findings', 'finding_rows'), ('not_evaluated', 'not_evaluated_rows')]:
        current = sum(n for (status, _), n in result['reasons'].items() if status == label)
        require(report['all_source'][key] - report['through2022'][key] == current, 'p16 status totals differ')
    require(report['all_source']['rows_evaluated'] == sum(result['year_counts'].values())
            and report['through2022']['rows'] == sum(n for k, n in result['year_counts'].items() if k != '2023'),
            'p16 report row totals differ')
    expected = {}
    with (base/'p16_schema_year_coverage.csv').open(newline='') as stream:
        for row in csv.DictReader(stream):
            if row['yearOfDiagnosis'] != '2023':
                continue
            key = (row['yearOfDiagnosis'], row['schemaId'])
            require(key not in expected and int(row['retained_through2022_rows']) == 0, 'Invalid coverage partition')
            expected[key] = Counter({status: int(row[field]) for status, field in
                                    [('passed', 'passed_rows'), ('findings', 'finding_rows'), ('not_evaluated', 'not_evaluated_rows')]})
            require(sum(expected[key].values()) == int(row['source_rows']), 'Schema coverage row mismatch')
    require(expected == result['coverage'], 'Schema coverage differs from frozen base')
    require(sum(legacy.values()) == report['through2022']['passed_rows'], 'Legacy retained total mismatch')
    checker = checker_class(rule)
    for key in legacy:
        require('1995' <= key[0] <= '2022', 'Invalid legacy diagnosis year')
        require(checker.add(dict(zip(COLS, key)))[0] == 'passed', 'Invalid legacy tuple')


def build(args):
    source, out = Path(args.source_csv), Path(args.out)
    require(not out.exists(), 'Refusing existing output directory')
    if args.replay_bundle:
        replay = Path(args.replay_bundle)
        old = verify_bundle(replay, args.replay_manifest_sha256)
        base, base_pin = replay/'inputs/base', old['base']['sha256']
    else:
        require(args.base_bundle and args.base_manifest_sha256, 'Pinned base bundle is required')
        base, base_pin = Path(args.base_bundle), args.base_manifest_sha256
    manifest = verify_bundle(base, base_pin)
    expected = manifest['source']['expected']
    before = {'sha256': sha(source), 'size_bytes': source.stat().st_size}
    require(before['sha256'] == args.source_sha256 == expected['sha256']
            and before['size_bytes'] == expected['size_bytes'], 'Wrong source pin')
    rule, checker_class = load_checker(base/'inputs/rules-v3.1.0.json', base/'provenance/p16_rules.py')
    legacy = read_counts(base/'through2022_schema_p16.csv')
    out.mkdir(parents=True, exist_ok=False)
    result = aggregate_2023(source, rule, checker_class)
    after = {'sha256': sha(source), 'size_bytes': source.stat().st_size}
    save(out/'source_verification.json', {'before': before, 'after': after, 'expected': expected})
    require(before == after, 'Source changed during scan')
    require(sum(result['year_counts'].values()) == expected['rows'], 'Source row count mismatch')
    validate_against_base(base, result, legacy, rule, checker_class)
    # Preserve and independently revalidate the complete base bundle for replay.
    shutil.copytree(base, out/'inputs/base')
    verify_bundle(out/'inputs/base', base_pin)
    verify_bundle(base, base_pin)
    (out/'provenance').mkdir()
    shutil.copyfile(Path(__file__), out/'provenance/build_p16_supplement.py')
    shutil.copyfile(base/'provenance/p16_rules.py', out/'provenance/p16_rules.py')
    write_csv(out/'year2023_schema_p16.csv', COLS+['count'], [(*k, n) for k, n in sorted(result['tuples'].items())])
    combined = out/'through2023_schema_p16.csv'
    with combined.open('xb') as stream:
        stream.write((base/'through2022_schema_p16.csv').read_bytes())
        text = io.StringIO(newline='')
        csv.writer(text).writerows([(*k, n) for k, n in sorted(result['tuples'].items())])
        stream.write(text.getvalue().encode())
    require(read_counts(combined) == legacy + result['tuples'], 'Combined tuple mismatch')
    write_csv(out/'year_schema_report.csv', ['yearOfDiagnosis', 'schemaId', 'source_rows', 'passed_rows', 'finding_rows', 'not_evaluated_rows'],
              [(*k, sum(c.values()), c['passed'], c['findings'], c['not_evaluated']) for k, c in sorted(result['coverage'].items())])
    write_csv(out/'exclusion_report.csv', ['status', 'reason', 'count'],
              [(*k, n) for k, n in sorted(result['reasons'].items()) if k[0] != 'passed'])
    counts = {'pre2023_retained': sum(legacy.values()), 'source_2023': result['year_counts']['2023'],
              'retained_2023': sum(result['tuples'].values()),
              'findings_2023': sum(n for (s, _), n in result['reasons'].items() if s == 'findings'),
              'unresolved_2023': sum(n for (s, _), n in result['reasons'].items() if s == 'not_evaluated'),
              'combined_retained': sum(legacy.values()) + sum(result['tuples'].values())}
    files = [{'path': p.relative_to(out).as_posix(), 'sha256': sha(p), 'size_bytes': p.stat().st_size}
             for p in sorted(out.rglob('*')) if p.is_file()]
    output = {'snapshot_schema_version': 1, 'kind': 'file_derived_p16_supplement', 'builder_version': '1.0.0',
              'created_at': datetime.now().astimezone().isoformat(), 'base': {'sha256': base_pin},
              'source': {'expected': expected}, 'counts': counts, 'files': files,
              'contains_patient_identifiers': False, 'clinical_representativeness': 'unverified',
              'status': 'complete', 'command': sys.argv}
    save(out/'manifest.json', output)
    verify_bundle(out, sha(out/'manifest.json'))
    print(json.dumps({'status': 'complete', 'counts': counts, 'manifest_sha256': sha(out/'manifest.json')}))
    return output


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base-bundle', type=Path)
    p.add_argument('--base-manifest-sha256')
    p.add_argument('--replay-bundle', type=Path)
    p.add_argument('--replay-manifest-sha256')
    p.add_argument('--source-csv', required=True, type=Path)
    p.add_argument('--source-sha256', required=True)
    p.add_argument('--out', required=True, type=Path)
    return p


if __name__ == '__main__':
    build(parser().parse_args())
