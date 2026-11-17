"""Shared catalog rules must constrain sampling and reject invalid raw deliveries."""
from copy import deepcopy
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import zipfile

import pytest

from jamboree_sdv import catalog_linkage
from jamboree_sdv.common import WORKSPACE, digest

RULES = 'reference/generation-rules/v2.3.0/rules.json'
PROFILE = 'experiments/gen-sdv/profiles/catalog-linkage-v1.0.0/profile.json'


def pin(root, path):
    return {'path': path, 'sha256': digest(root / path)}


@pytest.fixture
def bundle(tmp_path):
    root = Path(os.environ.get('JAMBOREE_RULE_TEST_WORKSPACE', WORKSPACE))
    for path in ['reference/generation-rules/v2.3.0',
                 'reference/generation-rules/lookups/ccdi-domains/2026-11-12',
                 'reference/nccr-public/2026-11-09']:
        shutil.copytree(root / path, tmp_path / path)
    dst = tmp_path / PROFILE
    dst.parent.mkdir(parents=True)
    shutil.copyfile(WORKSPACE / PROFILE, dst)
    return tmp_path


def load(root):
    assert hasattr(catalog_linkage, 'load_catalog_contract'), 'Shared catalog rule loader is missing'
    return catalog_linkage.load_catalog_contract(pin(root, RULES), pin(root, PROFILE), workspace=root)


def checker():
    path = WORKSPACE / 'experiments/gen-sdv/verifier/check_catalog_rules.py'
    assert path.exists(), 'Independent shared catalog checker is missing'
    spec = importlib.util.spec_from_file_location('catalog_rule_verifier', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def csv_bytes(rows):
    stream = io.StringIO(newline='')
    csv.writer(stream).writerows(rows)
    return stream.getvalue().encode()


def payloads(rows):
    patients = {}
    for patient, participant, name in rows:
        patients.setdefault(patient, []).extend([participant, name])
    width = max(map(len, patients.values())) // 2
    raw = [['nccrId'] + [f'{field}{i}' for i in range(1, width+1)
                       for field in ['resourceParticipantId', 'resourceName']]]
    raw += [[p] + values + [''] * (2*width-len(values)) for p, values in patients.items()]
    cpi = [['nccrId', 'resourceParticipantId', 'resourceName'], *rows]
    return csv_bytes(raw), csv_bytes(cpi)


def generated(root, n=2000):
    assert hasattr(catalog_linkage, 'regenerate_from_rules'), 'Rule-driven sampler is missing'
    rows = [(f'{i//4:06}', f'{i:08}', 'old') for i in range(n)]
    result, policy = catalog_linkage.regenerate_from_rules(
        rows, pin(root, RULES), pin(root, PROFILE), 20261110, workspace=root)
    return rows, result, policy


def test_global_inputs_preserve_published_counts_and_unknown_mass(bundle):
    contract = load(bundle)
    assert len(contract['catalog']) == 52
    assert sum(contract['counts'].values()) == 69705
    assert contract['total_records'] == 69734
    assert contract['counts']['USI'] == 17029
    assert 'COG USI' not in contract['counts']
    assert 'ACNS0332' not in {r['domain_name'] for r in contract['catalog']}


def test_rule_driven_sampling_keeps_ids_and_reports_conditioning(bundle):
    original, rows, policy = generated(bundle)
    assert [r[:2] for r in rows] == [r[:2] for r in original]
    assert (rows, policy) == generated(bundle)[1:]
    assert policy['unreported_count'] == 29
    assert policy['excluded_counts']['ACNS0332'] == 49
    assert policy['retained_known_mass'] == pytest.approx(.7168065418549602)
    assert policy['rules']['sha256'] == digest(bundle / RULES)
    assert policy['full_rule_set_qualified'] is False


@pytest.mark.parametrize('change', ['lookup_bytes', 'comparison', 'disabled', 'new_rule', 'profile'])
def test_unsupported_or_changed_inputs_fail_closed(bundle, change):
    rules = json.loads((bundle / RULES).read_text())
    if change == 'lookup_bytes':
        path = bundle / rules['lookups']['ccdi_mock_domains_2026_11_12']['data']['path']
        path.write_bytes(path.read_bytes() + b' ')
    elif change == 'profile':
        obj = json.loads((bundle / PROFILE).read_text())
        obj['selection'] = 'uniform_catalog'
        (bundle / PROFILE).write_text(json.dumps(obj))
    else:
        if change == 'comparison': rules['rules'][-3]['comparison'] = 'casefold'
        if change == 'disabled': rules['rules'][-3]['enabled'] = False
        if change == 'new_rule': rules['rules'].append({'id': 'ccdi_new', 'type': 'unknown', 'enabled': True, 'sources': ['CCDI']})
        (bundle / RULES).write_text(json.dumps(rules))
    with pytest.raises(ValueError): load(bundle)


def test_stale_rule_pin_rejected(bundle):
    assert hasattr(catalog_linkage, 'load_catalog_contract'), 'Shared catalog rule loader is missing'
    with pytest.raises(ValueError, match='hash'):
        catalog_linkage.load_catalog_contract({'path': RULES, 'sha256': '0'*64}, pin(bundle, PROFILE), workspace=bundle)


def test_fresh_generated_pair_passes_scoped_independent_check(bundle):
    _, rows, _ = generated(bundle)
    report = checker().check(*payloads(rows), {r[0] for r in rows}, load(bundle))
    assert report['status'] == 'passed'
    assert report['mapping_count'] == 2000
    assert len(report['evaluated_rules']) == 3
    assert report['full_rule_set_qualified'] is False
    assert 'pharm_canmed_ndc_tuple_is_consistent' in report['not_evaluated_rules']


@pytest.mark.parametrize('corruption,reason', [
    ('unknown', 'catalog'), ('missing', 'projection'), ('orphan', 'CTC'),
    ('duplicate', 'duplicate'), ('ambiguous', 'identity'), ('partial', 'pair'),
    ('gap', 'pair'), ('patient_case', 'patient'), ('frequency', 'frequency'),
])
def test_bad_delivery_is_rejected(bundle, corruption, reason):
    _, rows, _ = generated(bundle)
    rows = list(rows)
    parents = {r[0] for r in rows}
    if corruption == 'unknown': rows[0] = (*rows[0][:2], 'ACNS0332')
    if corruption == 'orphan': parents.remove(rows[0][0])
    if corruption == 'duplicate': rows.append(rows[0])
    if corruption == 'ambiguous': rows[4] = (rows[4][0], rows[0][1], rows[0][2])
    if corruption == 'frequency': rows = [(p, i, 'USI') for p, i, _ in rows]
    raw, cpi = payloads(rows)
    if corruption == 'missing': cpi = payloads(rows[1:])[1]
    if corruption in ('partial', 'gap', 'patient_case'):
        data = list(csv.reader(io.StringIO(raw.decode())))
        if corruption == 'partial': data[1][2] = ''
        if corruption == 'gap': data[1][1:3] = ['', '']
        if corruption == 'patient_case':
            data[1][0] = 'Patient'; data[2][0] = 'patient'
        raw = csv_bytes(data)
    with pytest.raises(ValueError, match=reason):
        checker().check(raw, cpi, parents, load(bundle))


def test_namespace_specific_ids_and_leading_zeros_are_valid(bundle):
    rows = [('0001', '0002', 'USI'), ('0003', '0002', 'PCDC')]
    report = checker().check(*payloads(rows), {'0001', '0003'}, load(bundle))
    assert report['mapping_count'] == 2


def delivery_manifest(root, rows):
    raw, cpi = payloads(rows)
    ctc = csv_bytes([['nccrId'], *[[p] for p in sorted({r[0] for r in rows})]])
    files = []
    for source, payload in [('CTC', ctc), ('CCDI', raw), ('CPI', cpi)]:
        path = root / (source + ('.csv' if source == 'CPI' else '.zip'))
        if source == 'CPI': path.write_bytes(payload)
        else:
            with zipfile.ZipFile(path, 'x') as z: z.writestr(source+'.csv', payload)
        files.append({'source': source, 'local_path': str(path), 'sha256': digest(path),
                      'size_bytes': path.stat().st_size})
    path = root / 'release.json'
    path.write_text(json.dumps({'files': files}))
    return pin(root, 'release.json')


def test_verifier_reads_pinned_release_and_archives_evidence(bundle):
    module = checker()
    assert hasattr(module, 'verify_release'), 'Pinned release verification is missing'
    assert hasattr(module, 'main'), 'Auditor command is missing'
    _, rows, _ = generated(bundle)
    manifest = delivery_manifest(bundle, rows)
    report = module.verify_release(manifest, pin(bundle, RULES), pin(bundle, PROFILE), workspace=bundle)
    assert report['status'] == 'passed'
    out = bundle / 'audit'
    args = ['--workspace', str(bundle), '--manifest', manifest['path'], '--manifest-sha256', manifest['sha256'],
            '--rules', RULES, '--rules-sha256', digest(bundle/RULES),
            '--profile', PROFILE, '--profile-sha256', digest(bundle/PROFILE), '--out', str(out)]
    assert module.main(args) == 0
    audit = json.loads((out/'audit.json').read_text())
    assert audit['status'] == 'passed' and audit['full_rule_set_qualified'] is False
    assert (out/'inputs/catalog.json').exists() and (out/'verifier/check_catalog_rules.py').exists()
    with pytest.raises(ValueError, match='existing'): module.main(args)


def test_release_input_corruption_cannot_pass(bundle):
    module = checker()
    assert hasattr(module, 'verify_release'), 'Pinned release verification is missing'
    _, rows, _ = generated(bundle)
    manifest = delivery_manifest(bundle, rows)
    (bundle/'CPI.csv').write_bytes((bundle/'CPI.csv').read_bytes()+b'\n')
    with pytest.raises(ValueError, match='hash'):
        module.verify_release(manifest, pin(bundle, RULES), pin(bundle, PROFILE), workspace=bundle)

