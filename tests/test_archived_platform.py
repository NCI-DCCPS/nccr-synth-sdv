"""Archived ETL checks require provenance for the exact code being executed."""
import importlib.util
import json
from pathlib import Path

import pytest

spec=importlib.util.spec_from_file_location('archived_etl_checker',Path(__file__).resolve().parents[1]/'verifier/check_etl_delivery.py')
checker=importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def test_archived_revision_requires_matching_code_and_passed_provenance(tmp_path):
    pins=[{'path':'handler.py','sha256':'a'*64}]
    evidence={'status':'passed_etl_delivery_compatibility','platform_code':pins,'platform_head':'b'*40}
    path=tmp_path/'provenance.json'
    path.write_text(json.dumps(evidence))
    assert checker.platform_revision(tmp_path,pins,path)=='b'*40
    with pytest.raises(ValueError,match='differs'):
        checker.platform_revision(tmp_path,[{'path':'handler.py','sha256':'c'*64}],path)
    evidence['status']='failed_etl_delivery_compatibility'
    path.write_text(json.dumps(evidence))
    with pytest.raises(ValueError,match='Unqualified'):
        checker.platform_revision(tmp_path,pins,path)
