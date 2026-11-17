"""Pinned patient-count evidence and policy, without generation dependencies."""
from pathlib import Path
from .common import checked_pin, digest, read_json

ALLOCATION = 'pinned_patient_histogram_exact_totals'


def load_reference(profile_pin, resolver=None):
    resolve = resolver or checked_pin

    def read(item):
        path = Path(resolve(item))
        if digest(path) != item['sha256']:
            raise ValueError('Multiplicity input hash mismatch')
        return read_json(path)

    profile = read(profile_pin)
    expected = {'schema_version': 1, 'version': '1.0.0',
                'allocation': 'floor_or_ceiling_exact_patient_and_tumor_totals',
                'rounding_objective': 'minimum_absolute_patient_count_error',
                'patient_assignment': 'seeded_uniform_permutation',
                'source_population': 'all_tumor_rows_in_frozen_stage_test_reference',
                'clinical_representativeness': 'unverified'}
    if set(profile) != set(expected) | {'manifest', 'histogram'} or any(profile[k] != v for k, v in expected.items()):
        raise ValueError('Unsupported tumor multiplicity profile')
    manifest, quality = read(profile['manifest']), read(profile['histogram'])
    relative = Path(profile['histogram']['path']).relative_to(Path(profile['manifest']['path']).parent)
    records = [p for p in manifest['files'] if p['path'] == str(relative)]
    if len(records) != 1 or records[0]['sha256'] != profile['histogram']['sha256']:
        raise ValueError('Multiplicity histogram is not bound by its frozen manifest')
    if manifest.get('status') != 'frozen' or manifest.get('immutable') is not True:
        raise ValueError('Multiplicity reference must be frozen')
    values = quality['tumor_rows_per_patient']
    if not isinstance(values, dict) or not values:
        raise ValueError('Missing patient multiplicity histogram')
    if any(not k.isascii() or not k.isdigit() or str(int(k)) != k or not 1 <= int(k) <= 99
           or type(v) is not int or v <= 0 for k, v in values.items()):
        raise ValueError('Multiplicity support outside the context schema or invalid counts')
    histogram = {int(k): v for k, v in values.items()}
    patients = sum(histogram.values())
    rows = sum(k*v for k, v in histogram.items())
    if (patients != quality['distinct_nonblank_patient_ids'] or rows != quality['total_rows']
            or rows != quality['distinct_complete_patient_tumor_keys']):
        raise ValueError('Multiplicity reference totals disagree')
    return {'histogram': histogram, 'patients': patients, 'rows': rows,
            'inputs': [profile_pin, profile['manifest'], profile['histogram']]}
