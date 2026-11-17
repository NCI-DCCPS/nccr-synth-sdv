"""Pinned declarative inputs for the three scoped CCDI/CPI rules.

Sampling and output verification share only this input reader, not algorithms.
Exact supported bundle hashes prevent new semantics from being silently ignored.
"""
from pathlib import Path

from .common import WORKSPACE, digest, read_json

SUPPORTED_RULES_SHA256 = '1ef2a47229785b209d5e1514d548739816879233a57728811e81211e2e64c3ed'
SUPPORTED_PROFILE_SHA256 = 'e99ecfb1e9ceb13fae3370740c2a66f5d69205ea7df92956a57ab0a8cddbfec0'
RULE_IDS = (
    'ccdi_cpi_resources_belong_to_catalog',
    'ccdi_cpi_mapping_identity_is_consistent',
    'ccdi_cpi_frequency_targets_remain_published_json',
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def checked(item, workspace):
    require(isinstance(item, dict) and set(item) == {'path', 'sha256'}, 'Invalid input pin')
    root = Path(workspace).resolve()
    path = (root / item['path']).resolve()
    require(path.is_relative_to(root) and path.is_file(), 'Missing input or input outside workspace')
    require(digest(path) == item['sha256'], 'Input hash mismatch: ' + item['path'])
    return path


def load_catalog_contract(rules_pin, profile_pin, *, workspace=WORKSPACE, resolver=None):
    def resolve(item):
        path=Path(resolver(item)) if resolver is not None else checked(item,workspace)
        require(digest(path)==item['sha256'],'Input hash mismatch')
        return path
    rules_path = resolve(rules_pin)
    profile_path = resolve(profile_pin)
    require(rules_pin['sha256'] == SUPPORTED_RULES_SHA256, 'Unsupported catalog rule bundle revision')
    require(profile_pin['sha256'] == SUPPORTED_PROFILE_SHA256, 'Unsupported catalog sampler profile')
    rules = read_json(rules_path)
    profile = read_json(profile_path)
    by_id = {r['id']: r for r in rules['rules']}
    membership, projection, frequency = (by_id[k] for k in RULE_IDS)
    lookup = rules['lookups'][membership['lookup_ref']]
    manifest_path = resolve(lookup['manifest'])
    manifest = read_json(manifest_path)
    catalog_path = resolve(lookup['data'])
    catalog = read_json(catalog_path)
    require(manifest['data_sha256'] == lookup['data']['sha256']
            and manifest_path.parent / manifest['data_file'] == catalog_path,
            'Catalog manifest/data mismatch')
    for entry in manifest['files']:
        path = (manifest_path.parent / entry['path']).resolve()
        require(path.is_relative_to(manifest_path.parent) and path.is_file()
                and path.stat().st_size == entry['size_bytes'] and digest(path) == entry['sha256'],
                'Catalog bundle hash mismatch')
    names = []
    require(isinstance(catalog, list) and len(catalog) == manifest['records'], 'Invalid catalog size')
    for item in catalog:
        require(isinstance(item, dict) and all(isinstance(item.get(k), str) for k in
                ['domain_name', 'domain_description', 'domain_category', 'data_location'])
                and bool(item['domain_name'].strip()) and bool(item['domain_description'].strip()),
                'Invalid catalog entry')
        names.append(item[membership['lookup_key']])
    require(len(names) == len(set(names)), 'Duplicate catalog key')

    reference = rules['frequency_references'][frequency['frequency_reference']]
    pins = {'rules': rules_pin, 'profile': profile_pin, 'catalog_manifest': lookup['manifest'],
            'catalog': lookup['data']}
    for key in ['snapshot_manifest', 'published_json', 'normalized_json']:
        resolve(reference[key])
        pins[key] = reference[key]
    normalized = read_json(resolve(reference['normalized_json']))
    published = read_json(resolve(reference['published_json']))
    chart = normalized['fields'][reference['field']]['charts'][reference['chart_index']]
    values = chart['values']
    require(all(isinstance(v[reference['value_column']], str) and v[reference['value_column']]
                and type(v[reference['count_column']]) is int and v[reference['count_column']] >= 0
                for v in values), 'Invalid published counts')
    counts = {v[reference['value_column']]: v[reference['count_column']] for v in values}
    require(len(counts) == len(values), 'Duplicate published category')
    source_field = next(f for f in published['fields'] if f['fieldName'] == reference['field'])
    source_values = source_field['charts'][reference['chart_index']]['values']
    require(len(source_values) == len(counts) and {v[0]: v[2] for v in source_values} == counts,
            'Normalized counts differ from published JSON')
    total = normalized[reference['denominator']]
    require(type(total) is int and total == published['totalCount'] and total >= sum(counts.values()) > 0,
            'Invalid frequency denominator')
    require(any(counts.get(name, 0) > 0 for name in names), 'No supported positive frequency mass')
    return {'rules_version': rules['version'], 'catalog': catalog, 'counts': counts,
            'total_records': total, 'profile': profile, 'inputs': pins,
            'membership': membership, 'projection': projection, 'frequency': frequency,
            'evaluated_rules': list(RULE_IDS),
            'not_evaluated_rules': [r['id'] for r in rules['rules'] if r['enabled'] and r['id'] not in RULE_IDS],
            'disabled_rules': [r['id'] for r in rules['rules'] if not r['enabled']]}
