"""Failure completion is mandatory even before source evaluation starts."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from jamboree_sdv.common import digest


@pytest.fixture
def checker(monkeypatch):
    directory = Path(__file__).parents[1] / 'verifier'
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location('rule4_audit_under_test', directory / 'check_rule4_replacement.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'archive_verifier', lambda *args: [])
    return module


def save(path, obj):
    path.write_text(json.dumps(obj))
    return {'path': str(path), 'sha256': digest(path)}


@pytest.mark.parametrize('archives', [None, 1, 'not_an_inventory', [{'original': {}}]])
def test_malformed_archives_emit_incomplete_nonzero_evidence(tmp_path, checker, archives):
    root = tmp_path / 'workspace'
    run = root / 'experiments/gen-sdv/work/run'
    run.mkdir(parents=True)
    recipe = run / 'recipe.json'
    save(recipe, {})
    generation = save(run / 'generation.json', {'archived_inputs': archives})
    candidate = run / 'candidate.json'
    save(candidate, {'replacement': {'generation': generation}})
    report = checker.audit(recipe, candidate, root, run / 'audit')
    assert report['status'] == 'error' and report['exit_code'] == 2 and report['complete'] is False
    completion = json.loads((run / 'audit/completion.json').read_text())
    assert completion['complete'] is False and completion['exit_code'] == 2
    assert digest(completion['error_report']['path']) == completion['error_report']['sha256']


def test_changed_generation_report_cannot_supply_archive_aliases(tmp_path, checker):
    root = tmp_path / 'workspace'
    run = root / 'experiments/gen-sdv/work/run'
    run.mkdir(parents=True)
    recipe = run / 'recipe.json'
    save(recipe, {})
    generation = save(run / 'generation.json', {'archived_inputs': []})
    candidate = run / 'candidate.json'
    save(candidate, {'replacement': {'generation': generation}})
    save(run / 'generation.json', {'archived_inputs': [], 'tampered': True})
    report = checker.audit(recipe, candidate, root, run / 'audit')
    assert report['status'] == 'error'
    assert 'Unbound generation evidence' in report['findings'][0]


def test_existing_audit_evidence_is_never_overwritten(tmp_path, checker):
    root = tmp_path / 'workspace'
    out = root / 'experiments/gen-sdv/work/existing'
    out.mkdir(parents=True)
    marker = out / 'audit.json'
    marker.write_text('immutable')
    with pytest.raises(ValueError, match='fresh'):
        checker.audit('missing', 'missing', root, out)
    assert marker.read_text() == 'immutable'
