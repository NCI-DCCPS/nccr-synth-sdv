"""Invented rows; writes confined to the explicitly authorized candidate tree."""
from collections import Counter
from contextlib import redirect_stdout
import csv
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / 'experiments/gen-sdv/scripts/build_p16_supplement.py'
BASE = ROOT / 'reference/nccr-derived/2026-11-13-stage-test-v30-g1-v1'
SCRATCH = ROOT / 'experiments/gen-sdv/work/horizon-2023/p16-supplement-candidate'
COLS = ['yearOfDiagnosis', 'schemaId', 'p16Recode']


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


class P16SupplementTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), 'The requested p16 supplement builder is missing')
        spec = importlib.util.spec_from_file_location('p16_supplement_test', SCRIPT)
        self.builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.builder)
        self.created_scratch = not SCRATCH.exists()
        SCRATCH.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(prefix='fixture-', dir=SCRATCH)
        self.root = Path(temp.name)
        self.addCleanup(self.cleanup, temp)
        self.rule, self.checker = self.builder.load_checker(
            BASE / 'inputs/rules-v3.1.0.json', BASE / 'provenance/p16_rules.py')

    def cleanup(self, temp):
        temp.cleanup()
        if self.created_scratch and not any(SCRATCH.iterdir()):
            SCRATCH.rmdir()

    def source(self, rows, header=None):
        path = self.root / 'source.csv'
        with path.open('w', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['nccrId', *COLS] if header is None else header)
            for i, row in enumerate(rows):
                writer.writerow([f'private-person-{i}', *row])
        return path

    def aggregate(self, path):
        return self.builder.aggregate_2023(path, self.rule, self.checker)

    def fixture(self):
        # Two historical rows, then five valid 2023 rows and three exclusions.
        rows = [('1995', '00360', ''), ('2022', '09520', '9'),
                ('2023', '09210', '0'), ('2023', '09210', '1'),
                ('2023', '09210', '9'), ('2023', '09520', '1'),
                ('2023', '00360', ''), ('2023', '09210', ''),
                ('2023', '09520', ''), ('2023', '', '')]
        source = self.source(rows)
        base = self.root / 'base'
        base.mkdir()
        for name in ['inputs/rules-v3.1.0.json', 'provenance/p16_rules.py',
                     'inputs/reference/inputs/ctc-normalized.json']:
            target = base / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((BASE / name).read_bytes())
        for evidence in self.rule['evidence']:
            name = 'inputs/p16-evidence/' + evidence['path']
            target = base / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((BASE / name).read_bytes())
        (base / 'through2022_schema_p16.csv').write_bytes(
            b'yearOfDiagnosis,schemaId,p16Recode,count\r\n1995,00360,,1\r\n2022,09520,9,1\r\n')
        def reasons(blank, applicable, findings, unknown):
            return [{'status': s, 'reason': r, 'count': n} for s, r, n in [
                ('passed', 'p16_not_applicable_blank', blank),
                ('passed', 'p16_applicable_result', applicable),
                ('findings', 'p16_blank_when_applicable', findings),
                ('not_evaluated', 'p16_unknown_schema', unknown)] if n]
        save(base / 'p16_report.json', {
            'rule_version': '3.1.0',
            'all_source': {'rows_evaluated': 10, 'passed_rows': 7, 'finding_rows': 2,
                           'not_evaluated_rows': 1, 'reasons': reasons(2, 5, 2, 1)},
            'through2022': {'rows': 2, 'passed_rows': 2, 'finding_rows': 0,
                            'not_evaluated_rows': 0, 'reasons': reasons(1, 1, 0, 0)}})
        save(base / 'support.json', {'observed_marginals': {
            'yearOfDiagnosis': {'1995': 1, '2022': 1, '2023': 8}}})
        with (base / 'p16_schema_year_coverage.csv').open('w', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow([*COLS[:2], 'source_rows', 'passed_rows', 'finding_rows',
                             'not_evaluated_rows', 'retained_through2022_rows'])
            writer.writerows([('1995', '00360', 1, 1, 0, 0, 1), ('2022', '09520', 1, 1, 0, 0, 1),
                              ('2023', '', 1, 0, 0, 1, 0), ('2023', '00360', 1, 1, 0, 0, 0),
                              ('2023', '09210', 4, 3, 1, 0, 0), ('2023', '09520', 2, 1, 1, 0, 0)])
        self.refresh_base(base, source)
        return base, source

    def refresh_base(self, base, source):
        files = [{'path': str(p.relative_to(base)), 'sha256': sha(p), 'size_bytes': p.stat().st_size}
                 for p in sorted(base.rglob('*')) if p.is_file() and p.name != 'manifest.json']
        save(base / 'manifest.json', {'snapshot_schema_version': 1, 'kind': 'file_derived_joint_counts',
             'source': {'expected': {'sha256': sha(source), 'size_bytes': source.stat().st_size,
                                     'rows': 10, 'patients': 10}}, 'files': files})

    def args(self, base, source, out):
        return self.builder.parser().parse_args([
            '--base-bundle', str(base), '--base-manifest-sha256', sha(base / 'manifest.json'),
            '--source-csv', str(source), '--source-sha256', sha(source), '--out', str(out)])

    def test_only_2023_is_newly_checked_and_no_identifiers_reach_checker(self):
        source = self.source([('2022', '09520', 'legacy-not-checked'), ('2023', '09210', '0'),
                              ('2023', '00360', ''), ('2023', '09520', ''), ('2023', '', '')])
        calls = []
        parent = self.checker
        class Check(parent):
            def add(inner, row):
                self.assertEqual(set(row), set(COLS))
                calls.append(row['yearOfDiagnosis'])
                return super().add(row)
        result = self.builder.aggregate_2023(source, self.rule, Check)
        self.assertEqual(calls, ['2023'] * 4)
        self.assertEqual(result['tuples'], Counter({('2023', '09210', '0'): 1, ('2023', '00360', ''): 1}))
        self.assertEqual(result['year_counts'], Counter({'2022': 1, '2023': 4}))
        self.assertEqual(result['reasons'][('findings', 'p16_blank_when_applicable')], 1)

    def test_bad_source_shapes_and_years_fail_without_patient_values(self):
        cases = [('nccrId,yearOfDiagnosis,schemaId,p16Recode\nprivate-x,2023,09210\n'),
                 ('nccrId,yearOfDiagnosis,schemaId,schemaId,p16Recode\n'),
                 ('nccrId,yearOfDiagnosis,schemaId\n'), '']
        for year in ['2024', '1994', '２０２３', '2023 ', '2023.0', '']:
            cases.append(f'nccrId,yearOfDiagnosis,schemaId,p16Recode\nprivate-x,{year},09210,1\n')
        for text in cases:
            with self.subTest(text=text):
                source = self.root / 'bad.csv'; source.write_text(text)
                with self.assertRaises(ValueError) as raised:
                    self.aggregate(source)
                self.assertNotIn('private-x', str(raised.exception))

    def test_count_table_rejects_invalid_counts_duplicates_and_headers(self):
        header = ','.join(COLS + ['count']) + '\n'
        for body in ['2022,09520,9,0\n', '2022,09520,9,-1\n', '2022,09520,9,1.0\n',
                     '2022,09520,9,01\n', '2022,09520,9,1\n2022,09520,9,1\n',
                     '2022,09520,9,1\n1995,00360,,1\n', '2022,09520,9\n']:
            with self.subTest(body=body):
                path = self.root / 'counts.csv'; path.write_text(header + body)
                with self.assertRaises(ValueError): self.builder.read_counts(path)

    def test_build_preserves_base_bytes_and_validates_combined_tuples(self):
        base, source = self.fixture(); out = self.root / 'out'
        with redirect_stdout(io.StringIO()) as log:
            result = self.builder.build(self.args(base, source, out))
        combined = out / 'through2023_schema_p16.csv'
        self.assertTrue(combined.read_bytes().startswith((base / 'through2022_schema_p16.csv').read_bytes()))
        self.assertEqual((out / 'inputs/base/through2022_schema_p16.csv').read_bytes(),
                         (base / 'through2022_schema_p16.csv').read_bytes())
        self.assertEqual(sum(self.builder.read_counts(combined).values()), 7)
        self.assertEqual(result['counts'], {'pre2023_retained': 2, 'source_2023': 8,
                         'retained_2023': 5, 'findings_2023': 2, 'unresolved_2023': 1, 'combined_retained': 7})
        for key in self.builder.read_counts(combined):
            self.assertEqual(self.checker(self.rule).add(dict(zip(COLS, key)))[0], 'passed')
        self.builder.verify_bundle(out, sha(out / 'manifest.json'))
        for path in out.rglob('*'):
            if path.is_file(): self.assertNotIn('private-person-', path.read_text())
        self.assertNotIn('private-person-', log.getvalue())

    def test_archived_cli_replay_works_without_original_base_or_builder_imports(self):
        base, source = self.fixture(); out = self.root / 'first'
        self.builder.build(self.args(base, source, out))
        base.rename(self.root / 'base-unavailable')
        replay = self.root / 'replay'
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONPATH='')
        proc = subprocess.run([sys.executable, '-B', str(out / 'provenance/build_p16_supplement.py'),
            '--replay-bundle', str(out), '--replay-manifest-sha256', sha(out / 'manifest.json'),
            '--source-csv', str(source), '--source-sha256', sha(source), '--out', str(replay)],
            cwd=self.root, env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for name in ['through2023_schema_p16.csv', 'year2023_schema_p16.csv', 'year_schema_report.csv', 'exclusion_report.csv']:
            self.assertEqual((out / name).read_bytes(), (replay / name).read_bytes())
        self.builder.verify_bundle(replay, sha(replay / 'manifest.json'))

    def test_wrong_source_pin_and_existing_destination_are_rejected(self):
        base, source = self.fixture(); out = self.root / 'out'
        args = self.args(base, source, out); args.source_sha256 = '0' * 64
        with self.assertRaisesRegex(ValueError, 'source'):
            self.builder.build(args)
        self.assertFalse(out.exists())
        out.mkdir()
        with self.assertRaisesRegex(ValueError, 'existing'):
            self.builder.build(self.args(base, source, out))
        self.assertEqual(list(out.iterdir()), [])

    def test_explicit_zero_schema_cells_match_absent_source_rows(self):
        base, source = self.fixture(); out = self.root / 'out'
        with (base / 'p16_schema_year_coverage.csv').open('a', newline='') as stream:
            csv.writer(stream).writerow(('2023', '00060', 0, 0, 0, 0, 0))
        self.refresh_base(base, source)
        result = self.builder.build(self.args(base, source, out))
        self.assertEqual(result['counts']['retained_2023'], 5)

    def test_source_changed_during_scan_cannot_publish_manifest(self):
        base, source = self.fixture(); out = self.root / 'out'
        real = self.builder.aggregate_2023
        def change(*args):
            result = real(*args)
            with source.open('a') as stream: stream.write('\n')
            return result
        with patch.object(self.builder, 'aggregate_2023', side_effect=change):
            with self.assertRaisesRegex(ValueError, 'changed'):
                self.builder.build(self.args(base, source, out))
        self.assertFalse((out / 'manifest.json').exists())
        self.assertTrue((out / 'source_verification.json').is_file())

    def test_report_mismatch_and_invalid_legacy_tuple_cannot_publish(self):
        for variant in ['year_marginal', 'reason', 'schema_coverage', 'legacy_tuple']:
            with self.subTest(variant=variant):
                # Each variant gets an independently fresh fixture directory.
                old = self.root
                self.root = old / variant; self.root.mkdir()
                try:
                    base, source = self.fixture(); out = self.root / 'out'
                    if variant == 'year_marginal':
                        save(base / 'support.json', {'observed_marginals': {'yearOfDiagnosis': {'1995': 1, '2022': 1, '2023': 9}}})
                    elif variant == 'reason':
                        report = json.loads((base / 'p16_report.json').read_text())
                        report['all_source']['reasons'][0]['count'] += 1; save(base / 'p16_report.json', report)
                    elif variant == 'schema_coverage':
                        p = base / 'p16_schema_year_coverage.csv'
                        p.write_text(p.read_text().replace('2023,09210,4,3,1,0,0', '2023,09210,4,2,2,0,0'))
                    else:
                        p = base / 'through2022_schema_p16.csv'
                        p.write_text(p.read_text().replace('2022,09520,9,1', '2022,09520,,1'))
                    self.refresh_base(base, source)
                    with self.assertRaises(ValueError): self.builder.build(self.args(base, source, out))
                    self.assertFalse((out / 'manifest.json').exists())
                finally: self.root = old

    def test_closed_inventory_detects_extra_changed_or_escaping_files(self):
        base, source = self.fixture(); out = self.root / 'out'
        self.builder.build(self.args(base, source, out)); pin = sha(out / 'manifest.json')
        extra = out / 'unlisted.txt'; extra.write_text('unlisted')
        with self.assertRaises(ValueError): self.builder.verify_bundle(out, pin)
        extra.unlink()
        (out / 'year2023_schema_p16.csv').write_text('tampered')
        with self.assertRaises(ValueError): self.builder.verify_bundle(out, pin)
        for name in ['../escape', '/absolute', '.', 'a/../../escape']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.builder.member(out, name)


if __name__ == '__main__':
    unittest.main()
