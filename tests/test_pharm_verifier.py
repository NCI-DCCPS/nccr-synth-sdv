import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('pharm_check', Path(__file__).parents[1] / 'verifier/check_pharm_enrichment.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_excluded_key_and_fabricated_unknown_are_errors():
    row = {'medicationRequestNdc9': '000000001', 'medicationRequestNdc11': '00000000101'}
    with pytest.raises(ValueError, match='excluded'):
        module.check_drug_row(row, {}, {'00000000101'}, {})
    with pytest.raises(ValueError, match='evidence'):
        module.check_drug_row(row, {}, set(), {})


def test_canmed_tuple_mismatch_and_mixed_rxnorm_state_are_errors():
    fields = {'medicationRequestNdc11': '00000000101', 'medicationRequestNdc9': '000000001',
              **{k: 'A' for k in module.CANMED}}
    row = {**fields, **module.NOT_SOUGHT}
    assert module.check_drug_row(row, {fields['medicationRequestNdc11']: fields}, set(fields.values()), {}) == 'canmed_matched'
    row['rxnormRxcui'] = '12345'
    with pytest.raises(ValueError, match='not-sought'):
        module.check_drug_row(row, {fields['medicationRequestNdc11']: fields}, set(), {})
    row.update(module.NOT_SOUGHT)
    row['canmedMajorDrugClass'] = 'wrong'
    with pytest.raises(ValueError, match='tuple'):
        module.check_drug_row(row, {fields['medicationRequestNdc11']: fields}, set(), {})


def test_unknown_requires_consistent_explicit_missing_state():
    row = {'medicationRequestNdc9': 'UNK', 'medicationRequestNdc11': 'UNK',
           **dict.fromkeys(module.CANMED, module.NO_CANMED), **module.MISSING}
    assert module.check_drug_row(row, {}, set(), {}) == 'unresolved'
    row['rxnormDisplayName'] = 'invented'
    with pytest.raises(ValueError, match='missing'):
        module.check_drug_row(row, {}, set(), {})
