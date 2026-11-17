"""Miniature ten-source releases exercise the runner with the real block sampler."""
from contextlib import redirect_stdout
from copy import deepcopy
import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from jamboree_sdv import ctc_replacement as runner


class ReplacementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ctc-replacement-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.resolver = runner.WorkspaceResolver(self.root)
        self.protected = ['indexCancer', 'yearOfDiagnosis'] + [f'context{i}' for i in range(25)]
        self.header = ['nccrId', 'tumorRecordNumber', *self.protected, 'leaf', 'parent', 'untouched']
        self.rows = []
        for patient, tumor, index in [('001', '01', '1'), ('001', '02', '0'), ('002', '01', '2')]:
            row = dict.fromkeys(self.header, '007')
            row.update(nccrId=patient, tumorRecordNumber=tumor, indexCancer=index,
                       yearOfDiagnosis='2020', leaf='001', parent='01', untouched='  quoted,\nvalue  ')
            self.rows.append(row)
        self.metadata = b'{ "unaltered": ["UTF-8: \xc3\xa9", "spaces"] }\n'
        self.ctc = self.root / 'data/r3/CTC.zip'
        self.write_zip(self.ctc, 'CTC', self.header, self.rows, self.metadata)
        entries = []
        for source in sorted(runner.SOURCES):
            path = self.root / 'data/r3' / (source + '.zip')
            if source != 'CTC':
                self.write_zip(path, source, ['nccrId', 'value'],
                               [{'nccrId': '001', 'value': '000'}, {'nccrId': '002', 'value': ''}])
            entries.append(self.entry(path, source, 3 if source == 'CTC' else 2))
        contract = self.json_file('reference/context.json',
                                  {'raw_bindings': {'CTC.' + field: {'op': 'fixture'} for field in self.protected}})
        patient = self.csv_file('runs/r3/context/patient.csv', ['nccrId'],
                                [{'nccrId': '001'}, {'nccrId': '002'}])
        tumor = self.csv_file('runs/r3/context/tumor.csv', ['nccrId', 'tumorRecordNumber'],
                              [{k: row[k] for k in runner.IDENTITY} for row in self.rows])
        context = self.json_file('runs/r3/context/manifest.json', {
            'schema_version': 1, 'context_schema_sha256': contract['sha256'],
            'patient': {'path': 'patient.csv', 'sha256': patient['sha256'], 'rows': 2},
            'tumor': {'path': 'tumor.csv', 'sha256': tumor['sha256'], 'rows': 3},
        })
        self.baseline = {'schema_version': 1, 'kind': 'gen_sdv_promoted_release',
                         'release_id': 'mini-r3', 'patients': 2, 'scope': sorted(runner.SOURCES),
                         'rules_version': '2.3.0', 'rules': self.json_file('reference/old.json', {'version': '2.3.0'}),
                         'context': context, 'context_contract': contract,
                         'files': entries, 'raw_files': deepcopy(entries),
                         'run_manifest': self.json_file('runs/r3/manifest.json', {'kind': 'gen_sdv_promoted_run'}),
                         'promotion': self.json_file('runs/r3/promotion.json', {'kind': 'gen_sdv_promotion'}),
                         'verification': {'required': True, 'assessment_path': '/old/assessment'},
                         'verified': True, 'status': 'verified', 'aws_upload': True, 'deployment': True}
        cpi = self.csv_file('data/r3/CPI.csv', ['nccrId', 'value'],
                            [{'nccrId': '001', 'value': '000'}, {'nccrId': '002', 'value': ''}])
        self.baseline['raw_files'] = [self.entry(self.root / cpi['path'], 'CPI', 2)
                                      if item['source'] == 'CPI' else item for item in self.baseline['raw_files']]
        table = self.csv_file('reference/joint/nested/counts.csv', ['yearOfDiagnosis', 'leaf', 'count'],
                              [{'yearOfDiagnosis': '2020', 'leaf': '002', 'count': '7'}])
        self.table_pin = table
        snapshot = self.json_file('reference/joint/manifest.json', {'files': [
            {'path': 'nested/counts.csv', 'sha256': table['sha256']}]})
        evidence = self.json_file('reference/v3/evidence/details.json', {'source': 'fixture'})
        parent = self.json_file('reference/v3/rules.json', {'version': '3.1.0', 'rules': [{'evidence': [evidence]}]})
        self.hierarchy = {'id': 'hierarchy', 'type': 'hierarchical_lookup', 'enabled': True,
                          'leaf_column': 'leaf', 'parent_columns': ['parent'],
                          'mapping': {'001': {'parent': '01'}, '002': {'parent': '02'}}}
        self.spec = {'version': '4.0.0', 'rules': [self.hierarchy], 'composition': {'parents': {'3.1.0': parent}}}
        rules = self.json_file('reference/v4/rules.json', self.spec)
        self.profile = {'hierarchy_rule_id': 'hierarchy', 'blocks': [{
            'id': 'draw', 'select': None, 'broadcast': None, 'rules': [{
                'id': 'leaf_draw', 'type': 'conditional_joint_table', 'type_version': 1,
                'given': ['yearOfDiagnosis'], 'outputs': ['leaf'], 'table': table,
                'count_column': 'count', 'backoff': [],
            }],
        }]}
        profile = self.json_file('experiments/gen-sdv/profiles/mini.json', self.profile)
        self.recipe = {'schema_version': 1, 'kind': 'ctc_conditional_replacement',
                       'recipe_id': 'mini-replacement', 'seed': 0, 'baseline': {},
                       'rules': rules, 'profile': profile, 'snapshot': snapshot,
                       'expected': {'patients': 2, 'rows': 3}}
        self.recipe_path = self.root / 'experiments/gen-sdv/recipes/mini.json'
        self.save_baseline()
        self.out = self.root / 'experiments/gen-sdv/work/replacement'
        self.addCleanup(patch.stopall)
        self.load = patch.object(runner, 'load_rules', return_value=(self.spec, {'3.1.0': {}})).start()
        self.validate = patch.object(runner, 'validate_profile', return_value=self.hierarchy).start()
        patch.object(runner, '_memory', return_value={'available_memory_bytes': 64 * 1024**3,
                    'total_memory_bytes': 128 * 1024**3, 'memory_method': 'test fixture'}).start()

    def json_file(self, relative, data):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + '\n')
        return self.resolver.pin(path)

    def csv_file(self, relative, header, rows):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=header, lineterminator='\n')
            writer.writeheader()
            writer.writerows(rows)
        return self.resolver.pin(path)

    def write_zip(self, path, source, header, rows, metadata=b'{"fixture":true}\n'):
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = io.StringIO(newline='')
        writer = csv.DictWriter(stream, fieldnames=header, lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(source + '.csv', stream.getvalue().encode())
            archive.writestr(source.lower() + '_metadata.json', metadata)

    def entry(self, path, source, count):
        return {'source': source, 'local_path': str(path), 'sha256': runner.digest(path),
                'rows': count, 'patients': 2, 'size_bytes': path.stat().st_size}

    def save_recipe(self):
        self.json_file(self.recipe_path, self.recipe)

    def save_baseline(self):
        self.recipe['baseline'] = self.json_file('releases/r3.json', self.baseline)
        self.save_recipe()

    def refresh_ctc(self):
        for name in ('files', 'raw_files'):
            for item in self.baseline[name]:
                if item['source'] == 'CTC':
                    item.update(sha256=runner.digest(self.ctc), size_bytes=self.ctc.stat().st_size)
        self.save_baseline()

    def run_fixture(self, out=None):
        return runner.run(self.recipe_path, self.root, out or self.out)

    def test_replacement_preserves_baseline_context_and_other_payloads(self):
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        candidate, report = self.run_fixture()
        self.assertEqual(candidate['kind'], 'gen_sdv_ctc_replacement_candidate')
        self.assertEqual(candidate['release_id'], self.recipe['recipe_id'])
        self.assertEqual(candidate['rules_version'], '4.0.0')
        self.assertEqual(report['counts'], {'patients': 2, 'rows': 3})
        self.assertEqual(set(report['protected_fields']), set(self.protected + list(runner.IDENTITY)))
        for field in ('promotion', 'verification', 'run_manifest', 'verified', 'status'):
            self.assertNotIn(field, candidate)
        for result in (candidate, report):
            self.assertEqual(result['audit_status'], 'pending')
            for field in ('promoted', 'aws_upload', 'deployment'):
                self.assertIs(result[field], False)
        for inventory in ('files', 'raw_files'):
            self.assertEqual([x for x in candidate[inventory] if x['source'] != 'CTC'],
                             [x for x in self.baseline[inventory] if x['source'] != 'CTC'])
        self.assertEqual(next(x for x in candidate['files'] if x['source'] == 'CTC'),
                         next(x for x in candidate['raw_files'] if x['source'] == 'CTC'))
        header, rows, metadata = runner._read_ctc(self.out / 'data/CTC.zip', self.recipe['expected'])
        self.assertEqual(header, self.header)
        self.assertEqual(metadata, self.metadata)
        for old, new in zip(self.rows, rows):
            self.assertEqual(new, {**old, 'leaf': '002', 'parent': '02'})
        for path, payload in before.items():
            self.assertEqual(path.read_bytes(), payload)
        for name in ('manifest.json', 'patient.csv', 'tumor.csv'):
            self.assertEqual((self.out / 'inputs/context' / name).read_bytes(),
                             (self.root / 'runs/r3/context' / name).read_bytes())
        self.assertEqual(len(report['untouched_files']), 18)
        self.assertEqual(len(report['code_pins']), 7)
        self.assertEqual(json.loads((self.out / 'generation.json').read_text()), report)
        self.assertEqual(json.loads((self.out / 'candidate.json').read_text()), candidate)

    def test_zip_deterministic_and_preserves_string_values(self):
        self.run_fixture()
        other = self.out.with_name('second')
        self.run_fixture(other)
        self.assertEqual((self.out / 'data/CTC.zip').read_bytes(), (other / 'data/CTC.zip').read_bytes())
        with zipfile.ZipFile(self.out / 'data/CTC.zip') as archive:
            self.assertIsNone(archive.testzip())
            for info in archive.infolist():
                self.assertEqual(info.date_time, (1980, 1, 1, 0, 0, 0))
                self.assertEqual(info.compress_type, zipfile.ZIP_DEFLATED)

    def test_string_pool_is_bounded_exact_and_excludes_identities(self):
        pool = runner._StringPool()
        header = ['nccrId', 'tumorRecordNumber', 'code', 'blank', 'long']
        with patch.object(runner, 'POOL_LIMIT', 2):
            first = pool.row(header, ['patient-a', 'tumor-01', ' 001 ', '', 'x' * 33])
            second = pool.row(header, ['patient-b', 'tumor-02', ''.join([' ', '001', ' ']), '', 'y' * 33])
            for number in range(10):
                pool.row(header, ['patient-c', 'tumor-03', str(number), '', 'z' * 33])
        self.assertEqual(set(pool.values), {' 001 ', ''})
        self.assertIs(first['code'], second['code'])
        self.assertIs(first['blank'], second['blank'])
        self.assertEqual(first, dict(zip(header, ['patient-a', 'tumor-01', ' 001 ', '', 'x' * 33])))
        self.assertGreater(pool.value_bytes, 0)

    def test_ctc_reader_reuses_only_exact_categorical_values(self):
        _, rows, _ = runner._read_ctc(self.ctc, self.recipe['expected'])
        self.assertEqual(rows, self.rows)
        self.assertIs(rows[0]['context0'], rows[1]['context0'])
        self.assertIs(rows[0]['context0'], rows[0]['context1'])

    def test_memory_inventory_counts_only_detached_block_projections(self):
        draw = {'given': ['yearOfDiagnosis'], 'outputs': ['leaf']}
        blocks = [{'select': None, 'rules': [draw]},
                  {'select': {'column': 'indexCancer', 'values': ['0']}, 'rules': [draw]}]
        inventory = runner._memory_inventory(self.ctc, blocks)
        self.assertEqual(inventory['block_selected_rows'], [3, 1])
        selected = inventory['block_selected_dictionary_bytes']
        self.assertEqual(inventory['peak_temporary_dictionary_bytes'], 2 * max(selected))
        self.assertLess(selected[0], inventory['row_dictionary_bytes'])
        self.assertEqual(inventory['block_projection_fields'], [['leaf', 'yearOfDiagnosis']] * 2)
        self.assertEqual(inventory['estimated_memory_bytes'], 2 * inventory['row_dictionary_bytes'] +
                         inventory['peak_temporary_dictionary_bytes'] + inventory['unique_or_unpooled_value_bytes'] +
                         inventory['overhead_reserve_bytes'])

    def test_archival_replay_keeps_snapshot_layout_and_recursive_evidence(self):
        _, report = self.run_fixture()
        mapping = {item['original']['path']: item['archive'] for item in report['archived_inputs']}
        snapshot = self.resolver(mapping[self.recipe['snapshot']['path']])
        table = self.resolver(mapping[self.table_pin['path']])
        self.assertEqual(snapshot.parent / 'nested/counts.csv', table)
        self.assertIn('reference/v3/rules.json', mapping)
        self.assertIn('reference/v3/evidence/details.json', mapping)
        for item in report['archived_inputs'] + report['code_pins']:
            self.assertEqual(item['original']['sha256'], item['archive']['sha256'])
            self.resolver(item['archive'])
        # The sampler can replay from archives after original selected references vanish.
        for path in (self.root / 'reference').rglob('*'):
            if path.is_file():
                path.unlink()
        resolver = lambda pin: self.resolver(mapping[pin['path']])
        hierarchy = {key: self.hierarchy[key] for key in ('leaf_column', 'parent_columns', 'mapping')}
        rows, _ = runner.apply_blocks(self.rows, self.profile['blocks'], resolver, 0,
                                      protected_fields=self.protected, hierarchy=hierarchy)
        self.assertTrue(all(row['leaf'] == '002' and row['parent'] == '02' for row in rows))

    def test_snapshot_inventory_does_not_follow_historical_source_pins(self):
        historical = {'path': '/outside-workspace/large-source.csv', 'sha256': '0' * 64}
        evidence = self.json_file('reference/joint/analysis/previous.json', {'source': historical,
                                  'files': [{'path': 'missing-old-source.csv', 'sha256': '1' * 64}]})
        snapshot = {'source_provenance': historical, 'files': [
            {'path': 'nested/counts.csv', 'sha256': self.table_pin['sha256']},
            {'path': 'analysis/previous.json', 'sha256': evidence['sha256']}]}
        self.recipe['snapshot'] = self.json_file('reference/joint/manifest.json', snapshot)
        self.save_recipe()
        _, report = self.run_fixture()
        archived = {item['original']['path']: item['archive'] for item in report['archived_inputs']}
        self.assertIn(evidence['path'], archived)
        self.assertNotIn(historical['path'], archived)
        self.assertEqual(json.loads(self.resolver(archived[evidence['path']]).read_text())['source'], historical)

    def test_absolute_and_relative_pins_have_one_canonical_archive_binding(self):
        self.spec['rules'].append({'reference_snapshot': self.recipe['snapshot']})
        self.recipe['rules'] = self.json_file('reference/v4/rules.json', self.spec)
        self.recipe['snapshot'] = {**self.recipe['snapshot'],
                                   'path': str(self.root / self.recipe['snapshot']['path'])}
        self.save_recipe()
        _, report = self.run_fixture()
        keys = [(str(self.resolver.path(item['original']['path'])), item['original']['sha256'])
                for item in report['archived_inputs']]
        self.assertEqual(len(keys), len(set(keys)))

    def test_refuses_existing_output_without_changes(self):
        self.out.mkdir(parents=True)
        sentinel = self.out / 'sentinel'
        sentinel.write_text('keep')
        with self.assertRaisesRegex(ValueError, 'existing output'):
            self.run_fixture()
        self.assertEqual(list(self.out.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_text(), 'keep')

    def test_output_must_be_under_explicit_workspace_work(self):
        for out in (self.root / 'work/x', self.root / 'data/new', self.root / 'experiments/gen-sdv/work'):
            with self.subTest(out=out), self.assertRaises(ValueError):
                self.run_fixture(out)
        self.run_fixture(Path('experiments/gen-sdv/work/relative'))

    def test_symlink_and_traversal_escape_rejected(self):
        with tempfile.TemporaryDirectory() as external:
            path = Path(external) / 'outside'
            path.write_text('outside')
            pin = {'path': str(path), 'sha256': runner.digest(path)}
            with self.assertRaisesRegex(ValueError, 'escapes workspace'):
                self.resolver(pin)
            link = self.root / 'escape'
            link.symlink_to(path)
            with self.assertRaisesRegex(ValueError, 'escapes workspace'):
                self.resolver({**pin, 'path': 'escape'})
            work = self.root / 'experiments/gen-sdv/work'
            work.parent.mkdir(parents=True, exist_ok=True)
            work.symlink_to(external, target_is_directory=True)
            with self.assertRaises(ValueError):
                self.run_fixture()

    def test_recipe_rejects_unknown_keys_and_boolean_counts(self):
        original = deepcopy(self.recipe)
        variants = [dict(original, surprise=True), dict(original, schema_version=True),
                    dict(original, seed=True), dict(original, seed=-1),
                    dict(original, expected={'patients': True, 'rows': 3}),
                    dict(original, expected={'patients': 2, 'rows': 0}),
                    dict(original, expected={'patients': 2, 'rows': 3, 'other': 1})]
        for recipe in variants:
            with self.subTest(recipe=recipe):
                self.recipe = recipe
                self.save_recipe()
                with self.assertRaises(ValueError):
                    self.run_fixture()
                self.assertFalse(self.out.exists())

    def test_duplicate_json_keys_rejected(self):
        self.recipe_path.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaisesRegex(ValueError, 'Duplicate JSON'):
            self.run_fixture()

    def test_pin_hash_and_shape_tampering_rejected(self):
        original = deepcopy(self.recipe)
        for pin in ({**original['profile'], 'sha256': '0' * 64},
                    {**original['profile'], 'extra': True},
                    {**original['profile'], 'sha256': 42}):
            self.recipe = deepcopy(original)
            self.recipe['profile'] = pin
            self.save_recipe()
            with self.assertRaises(ValueError):
                self.run_fixture()

    def test_all_inventories_require_unique_full_source_sets(self):
        original = deepcopy(self.baseline)
        for name in ('files', 'raw_files'):
            for change in ('missing', 'duplicate', 'count', 'size', 'hash', 'path'):
                with self.subTest(name=name, change=change):
                    self.baseline = deepcopy(original)
                    items = self.baseline[name]
                    if change == 'missing':
                        items.pop()
                    elif change == 'duplicate':
                        items[-1] = deepcopy(items[0])
                    elif change == 'count':
                        items[0]['rows'] += 1
                    elif change == 'size':
                        items[0]['size_bytes'] += 1
                    elif change == 'hash':
                        items[0]['sha256'] = '0' * 64
                    elif change == 'path':
                        items[1].update(local_path=items[0]['local_path'], sha256=items[0]['sha256'],
                                        size_bytes=items[0]['size_bytes'])
                    self.save_baseline()
                    with self.assertRaises(ValueError):
                        self.run_fixture()
                    self.assertFalse(self.out.exists())

    def test_ctc_inventory_paths_must_agree(self):
        duplicate = self.ctc.with_name('copy.zip')
        duplicate.write_bytes(self.ctc.read_bytes())
        for item in self.baseline['raw_files']:
            if item['source'] == 'CTC':
                item['local_path'] = str(duplicate)
        self.save_baseline()
        with self.assertRaisesRegex(ValueError, 'CTC inventories disagree'):
            self.run_fixture()

    def test_ctc_actual_counts_must_match(self):
        self.write_zip(self.ctc, 'CTC', self.header, self.rows[:-1])
        self.refresh_ctc()
        with self.assertRaisesRegex(ValueError, 'Actual CTC count mismatch'):
            self.run_fixture()

    def test_ctc_identity_and_anchor_invariants(self):
        original = deepcopy(self.rows)
        for mutate in ('blank', 'duplicate', 'no_anchor', 'two_anchors'):
            with self.subTest(mutate=mutate):
                rows = deepcopy(original)
                if mutate == 'blank':
                    rows[0]['nccrId'] = ' '
                elif mutate == 'duplicate':
                    rows[1]['tumorRecordNumber'] = '01'
                elif mutate == 'no_anchor':
                    rows[0]['indexCancer'] = '0'
                else:
                    rows[1]['indexCancer'] = '2'
                self.write_zip(self.ctc, 'CTC', self.header, rows)
                self.refresh_ctc()
                with self.assertRaises(ValueError):
                    self.run_fixture()
                self.assertFalse(self.out.exists())

    def test_zip_members_header_width_and_crc_checked(self):
        malformed = [b'nccrId,nccrId\n001,001\n', b'nccrId,tumorRecordNumber\n001\n']
        for payload in malformed:
            with zipfile.ZipFile(self.ctc, 'w') as archive:
                archive.writestr('CTC.csv', payload)
                archive.writestr('ctc_metadata.json', '{}')
            self.refresh_ctc()
            with self.assertRaises(ValueError):
                self.run_fixture()
        self.write_zip(self.ctc, 'CTC', self.header, self.rows)
        with zipfile.ZipFile(self.ctc, 'a') as archive:
            archive.writestr('extra.txt', 'reject')
        self.refresh_ctc()
        with self.assertRaisesRegex(ValueError, 'Unexpected CTC ZIP'):
            self.run_fixture()
        with zipfile.ZipFile(self.ctc, 'w', compression=zipfile.ZIP_STORED) as archive:
            archive.writestr('CTC.csv', 'nccrId,tumorRecordNumber,indexCancer\n001,01,1\n')
            archive.writestr('ctc_metadata.json', '{}')
        payload = self.ctc.read_bytes().replace(b'001,01,1', b'002,01,1')
        self.ctc.write_bytes(payload)
        self.refresh_ctc()
        with self.assertRaises(zipfile.BadZipFile):
            self.run_fixture()

    def test_context_sidecar_tamper_rejected(self):
        (self.root / 'runs/r3/context/patient.csv').write_text('nccrId\nother\n')
        with self.assertRaisesRegex(ValueError, 'Input hash mismatch'):
            self.run_fixture()

    def test_context_contract_requires_exact_27_fields(self):
        pin = self.json_file('reference/context.json', {'raw_bindings': {'CTC.indexCancer': {}}})
        self.baseline['context_contract'] = pin
        context = json.loads(self.resolver(self.baseline['context']).read_text())
        context['context_schema_sha256'] = pin['sha256']
        self.baseline['context'] = self.json_file('runs/r3/context/manifest.json', context)
        self.save_baseline()
        with self.assertRaisesRegex(ValueError, '27 CTC context'):
            self.run_fixture()

    def test_no_candidate_on_protected_or_non_owned_change(self):
        for field in ('nccrId', 'context0', 'untouched'):
            rows = deepcopy(self.rows)
            rows[0][field] = 'changed'
            with self.subTest(field=field), patch.object(runner, 'apply_blocks', return_value=(rows, {})):
                out = self.out.with_name('bad-' + field)
                with self.assertRaisesRegex(ValueError, 'Sampler changed'):
                    self.run_fixture(out)
                self.assertFalse((out / 'candidate.json').exists())

    def test_sampler_receives_minimal_selected_hierarchy(self):
        with patch.object(runner, 'apply_blocks', wraps=runner.apply_blocks) as sampler:
            self.run_fixture()
        self.assertEqual(set(sampler.call_args.kwargs['hierarchy']), {'leaf_column', 'parent_columns', 'mapping'})
        self.assertEqual(set(sampler.call_args.kwargs['protected_fields']), set(self.protected + list(runner.IDENTITY)))

    def test_resource_preflight_refuses_insufficient_disk_or_memory(self):
        class Disk:
            free = 0
        with patch.object(runner.shutil, 'disk_usage', return_value=Disk()):
            with self.assertRaisesRegex(ValueError, 'Insufficient disk'):
                self.run_fixture()
        with patch.object(runner, '_memory', return_value={'available_memory_bytes': 1}):
            with self.assertRaisesRegex(ValueError, 'memory exceeds'):
                self.run_fixture()
        self.assertFalse(self.out.exists())

    def test_cli_explicit_workspace(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(runner.main(['--recipe', str(self.recipe_path), '--workspace', str(self.root),
                                          '--out', str(self.out)]), 0)
        self.assertEqual(json.loads(output.getvalue())['audit_status'], 'pending')
        self.assertTrue((self.out / 'candidate.json').exists())


if __name__ == '__main__':
    unittest.main()
