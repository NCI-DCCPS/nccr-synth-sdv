"""Offline, immutable-baseline CTC replacement; independent audit is a later gate.

``run(recipe_path, workspace, out) -> (candidate, generation_report)`` writes
candidate.json last. generation.json contains ``archived_inputs`` records with
``original`` and ``archive`` file pins. Shared reference paths retain their full
workspace-relative layout below inputs/workspace, including snapshot siblings.
Only this module's seven Python dependencies are archived, never the old sampler
or its transient work. No subprocesses, network requests, promotion or audit run.
"""
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
import argparse
import csv
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import re
import resource
import shutil
import sys
import time
import zipfile
import importlib.metadata

from .common import digest, now, read_json, write_json
from .joint_blocks import apply_blocks
from .rule4_contract import load_rules, validate_profile

SOURCES = frozenset(('CTC', 'ABM', 'COG', 'MCE', 'MCD', 'MCP', 'PHARM', 'RO', 'CCDI', 'CPI'))
IDENTITY = ('nccrId', 'tumorRecordNumber')
CODE_MODULES = ('__init__', 'common', 'random_streams', 'conditional_joint',
                'joint_blocks', 'rule4_contract', 'ctc_replacement')
RECIPE_KEYS = {'schema_version', 'kind', 'recipe_id', 'seed', 'baseline', 'rules',
               'profile', 'snapshot', 'expected'}
POOL_LIMIT = 65536
POOL_MAX_LENGTH = 32


def _require(ok, message):
    if not ok:
        raise ValueError(message)


def _integer(value, label, minimum=1):
    _require(type(value) is int and value >= minimum, f'{label}: invalid integer')
    return value


def _pin_shape(pin):
    _require(type(pin) is dict and set(pin) == {'path', 'sha256'}, 'Invalid file pin keys')
    _require(isinstance(pin['path'], str) and pin['path'].strip() and '\0' not in pin['path'],
             'Invalid pin path')
    _require(isinstance(pin['sha256'], str) and re.fullmatch('[0-9a-f]{64}', pin['sha256']),
             'Invalid SHA-256 pin')


class WorkspaceResolver:
    """Explicit workspace boundary, independent of the imported code's checkout."""

    def __init__(self, workspace):
        self.root = Path(workspace).resolve(strict=True)
        _require(self.root.is_dir(), 'Workspace must be a directory')

    def path(self, value):
        _require(isinstance(value, (str, Path)) and str(value).strip(), 'Invalid input path')
        path = (self.root / value).resolve()
        _require(path.is_relative_to(self.root), 'Input escapes workspace')
        return path

    def __call__(self, pin):
        _pin_shape(pin)
        path = self.path(pin['path'])
        _require(path.is_file(), 'Input file missing: ' + pin['path'])
        _require(digest(path) == pin['sha256'], 'Input hash mismatch: ' + pin['path'])
        return path

    def pin(self, path):
        path = self.path(path)
        return {'path': str(path.relative_to(self.root)), 'sha256': digest(path)}


def _output(out, resolver):
    original = resolver.root / out
    boundary = resolver.root / 'experiments/gen-sdv/work'
    _require(boundary.resolve() == boundary, 'Output work directory may not redirect')
    path = resolver.path(original)
    _require(path.is_relative_to(boundary) and path != boundary, 'Output must be below experiments/gen-sdv/work')
    _require(not original.exists() and not original.is_symlink(), 'Refusing existing output')
    return path


def _recipe(path, resolver):
    path = resolver.path(path)
    pin = resolver.pin(path)
    spec = read_json(resolver(pin))
    _require(type(spec) is dict and set(spec) == RECIPE_KEYS, 'Unknown or missing recipe keys')
    _require(type(spec['schema_version']) is int and spec['schema_version'] == 1,
             'Unsupported recipe schema')
    _require(spec['kind'] == 'ctc_conditional_replacement', 'Unsupported recipe kind')
    _require(isinstance(spec['recipe_id'], str) and
             re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', spec['recipe_id']), 'Invalid recipe ID')
    _integer(spec['seed'], 'Seed', 0)
    _require(type(spec['expected']) is dict and set(spec['expected']) == {'patients', 'rows'},
             'Invalid expected counts')
    for key, value in spec['expected'].items():
        _integer(value, 'Expected ' + key)
    for key in ('baseline', 'rules', 'profile', 'snapshot'):
        resolver(spec[key])
    return spec, pin


@contextmanager
def _csv_stream(path, source):
    if path.suffix.lower() == '.zip':
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            _require(len(names) == len(set(names)), 'Duplicate ZIP members')
            if source == 'CTC':
                _require(set(names) == {'CTC.csv', 'ctc_metadata.json'}, 'Unexpected CTC ZIP members')
            _require(source + '.csv' in names, 'Missing source CSV member')
            # Exhausting each member, including metadata, checks its CRC.
            for name in names:
                if name != source + '.csv':
                    with archive.open(name) as stream:
                        while stream.read(1024 * 1024):
                            pass
            with archive.open(source + '.csv') as binary:
                with io.TextIOWrapper(binary, encoding='utf-8', newline='') as stream:
                    yield stream
    else:
        _require(source == 'CPI' and path.suffix.lower() == '.csv', 'Expected source ZIP or raw CPI CSV')
        with path.open(encoding='utf-8', newline='') as stream:
            yield stream


def _csv_rows(stream):
    reader = csv.reader(stream, strict=True)
    header = next(reader, None)
    _require(header and all(field.strip() for field in header) and len(set(header)) == len(header),
             'CSV header must have unique nonblank columns')
    _require('nccrId' in header, 'Missing nccrId column')

    def rows():
        for values in reader:
            _require(len(values) == len(header), 'Malformed CSV row width')
            yield values
    return header, rows()


def _measure_payload(path, source):
    with _csv_stream(path, source) as stream:
        header, rows = _csv_rows(stream)
        patient_index = header.index('nccrId')
        patients, count = set(), 0
        for values in rows:
            patient = values[patient_index]
            _require(patient.strip(), 'Blank patient ID')
            patients.add(patient)
            count += 1
    return {'rows': count, 'patients': len(patients)}


def _inventories(baseline, resolver, expected):
    _require(baseline.get('kind') == 'gen_sdv_promoted_release', 'Baseline must be a promoted release')
    _require(baseline.get('schema_version') == 1 and type(baseline.get('schema_version')) is int,
             'Invalid baseline schema')
    _integer(baseline.get('patients'), 'Baseline patients')
    _require(baseline['patients'] == expected['patients'], 'Baseline patient count mismatch')
    _require(type(baseline.get('scope')) is list and len(baseline['scope']) == len(SOURCES)
             and set(baseline['scope']) == SOURCES, 'Baseline must contain all ten sources')
    inventories, measured, paths = {}, {}, {}
    for name in ('files', 'raw_files'):
        items = baseline.get(name)
        _require(type(items) is list and len(items) == len(SOURCES), 'Invalid ' + name + ' inventory')
        by_source, seen_paths = {}, set()
        for item in items:
            _require(type(item) is dict and {'source', 'local_path', 'sha256', 'rows', 'patients', 'size_bytes'} <= set(item),
                     'Incomplete payload entry')
            source = item['source']
            _require(source in SOURCES and source not in by_source, 'Duplicate or unknown inventory source')
            for field in ('rows', 'patients', 'size_bytes'):
                _integer(item[field], 'Payload ' + field)
            _require(item['patients'] <= item['rows'] and item['patients'] <= baseline['patients'],
                     'Invalid payload counts')
            pin = {'path': item['local_path'], 'sha256': item['sha256']}
            path = resolver(pin)
            _require(path not in seen_paths, 'Duplicate payload path in inventory')
            seen_paths.add(path)
            if path in paths:
                _require(paths[path] == (source, item['sha256']), 'Conflicting payload reference')
            paths[path] = source, item['sha256']
            _require(path.stat().st_size == item['size_bytes'], 'Payload size mismatch')
            if source != 'CTC':
                if path not in measured:
                    measured[path] = _measure_payload(path, source)
                _require(all(item[k] == v for k, v in measured[path].items()), 'Payload count mismatch')
            else:
                _require(all(item[k] == v for k, v in expected.items()), 'CTC expected count mismatch')
            by_source[source] = item
        inventories[name] = by_source
    left, right = (inventories[n]['CTC'] for n in ('files', 'raw_files'))
    _require(resolver.path(left['local_path']) == resolver.path(right['local_path']) and
             all(left[k] == right[k] for k in ('sha256', 'rows', 'patients', 'size_bytes')),
             'CTC inventories disagree')
    return inventories


def _memory():
    """Read available memory through OS APIs, without launching commands."""
    if sys.platform == 'darwin':
        lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
        total = ctypes.c_uint64()
        size = ctypes.c_size_t(ctypes.sizeof(total))
        _require(lib.sysctlbyname(b'hw.memsize', ctypes.byref(total), ctypes.byref(size), None, 0) == 0,
                 'Cannot measure total memory')
        stats = (ctypes.c_uint32 * 64)()
        count = ctypes.c_uint32(64)
        _require(lib.host_statistics64(lib.mach_host_self(), 4, stats, ctypes.byref(count)) == 0,
                 'Cannot measure available memory')
        available = (stats[0] + stats[2]) * os.sysconf('SC_PAGE_SIZE')
        return {'total_memory_bytes': total.value, 'available_memory_bytes': available,
                'memory_method': 'host_statistics64 free + inactive pages; excludes speculative/purgeable'}
    values = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key, value = line.split(':', 1)
        values[key] = int(value.split()[0]) * 1024
    return {'total_memory_bytes': values['MemTotal'], 'available_memory_bytes': values['MemAvailable'],
            'memory_method': '/proc/meminfo MemAvailable'}


class _StringPool:
    """Bounded exact-string reuse; identities and long text never enter the pool."""

    def __init__(self):
        self.values = {}
        self.value_bytes = 0

    def row(self, header, values):
        row = {}
        for field, value in zip(header, values):
            if field not in IDENTITY and len(value) <= POOL_MAX_LENGTH:
                shared = self.values.get(value)
                if shared is not None:
                    row[field] = shared
                    continue
                if len(self.values) < POOL_LIMIT:
                    self.values[value] = value
            # Account once for pooled strings, per occurrence for unpooled values.
            # This deliberately overcounts CPython's cached one-character strings.
            self.value_bytes += sys.getsizeof(value)
            row[field] = value
        return row


def _memory_inventory(ctc, blocks):
    """Streaming census: no table of rows is retained during resource estimation."""
    pool = _StringPool()
    dictionaries, count = 0, 0
    selected_bytes = [0 for _ in blocks]
    selected_counts = [0 for _ in blocks]
    projected_fields = [{field for rule in block['rules'] for field in rule['given'] + rule['outputs']}
                        for block in blocks]
    with _csv_stream(ctc, 'CTC') as stream:
        header, rows = _csv_rows(stream)
        for values in rows:
            row = pool.row(header, values)
            size = sys.getsizeof(row)
            dictionaries += size
            count += 1
            for i, block in enumerate(blocks):
                selector = block['select']
                if selector is None or row[selector['column']] in selector['values']:
                    selected_bytes[i] += sys.getsizeof({field: row[field] for field in projected_fields[i]})
                    selected_counts[i] += 1
    # Detached input/output projections coexist within a block and are released
    # before the next block. The full-width baseline/result frames remain live.
    temporary = max((2 * value for value in selected_bytes), default=0)
    estimate = 2 * dictionaries + temporary + pool.value_bytes + 512 * 1024**2
    return {'rows': count, 'columns': len(header), 'row_dictionary_bytes': dictionaries,
            'unique_or_unpooled_value_bytes': pool.value_bytes, 'pooled_values': len(pool.values),
            'pool_limit': POOL_LIMIT, 'pool_max_length': POOL_MAX_LENGTH,
            'block_selected_rows': selected_counts, 'block_selected_dictionary_bytes': selected_bytes,
            'block_projection_fields': [sorted(fields) for fields in projected_fields],
            'peak_temporary_dictionary_bytes': temporary, 'overhead_reserve_bytes': 512 * 1024**2,
            'estimated_memory_bytes': estimate}


def _preflight(ctc, expected, out, blocks):
    ancestor = out.parent
    while not ancestor.exists():
        ancestor = ancestor.parent
    free = shutil.disk_usage(ancestor).free
    minimum = 5 * ctc.stat().st_size + 128 * 1024**2
    _require(free >= minimum, 'Insufficient disk space for CTC replacement')
    inventory = _memory_inventory(ctc, blocks)
    _require(inventory['rows'] == expected['rows'], 'Actual CTC count mismatch')
    estimate = inventory['estimated_memory_bytes']
    memory = _memory()
    limit = memory['available_memory_bytes'] * 3 // 4
    _require(estimate <= limit, f'Estimated CTC memory exceeds 75% of available memory: '
             f'estimate={estimate}, available={memory["available_memory_bytes"]}, limit={limit}')
    return {**memory, 'disk_free_bytes': free, 'minimum_disk_free_bytes': minimum,
            'estimated_memory_bytes': estimate, 'memory_limit_bytes': limit,
            'memory_inventory': inventory,
            'memory_estimate_method': 'streamed pooled value census + baseline/result dictionaries + '
            'max(2x selected projected dictionaries) + 512 MiB reserve',
            'status': 'passed', 'measured_at': now()}


def _read_ctc(path, expected):
    with _csv_stream(path, 'CTC') as stream:
        header, raw = _csv_rows(stream)
        _require(set(IDENTITY + ('indexCancer',)) <= set(header), 'Missing CTC identity/index columns')
        pool = _StringPool()
        rows = [pool.row(header, values) for values in raw]
    keys, patients, anchors = set(), set(), Counter()
    for row in rows:
        key = tuple(row[k] for k in IDENTITY)
        _require(all(v.strip() for v in key), 'Blank CTC identity')
        _require(key not in keys, 'Duplicate CTC patient/tumor key')
        keys.add(key)
        patients.add(key[0])
        if row['indexCancer'] in ('1', '2'):
            anchors[key[0]] += 1
    _require(len(rows) == expected['rows'] and len(patients) == expected['patients'],
             'Actual CTC count mismatch')
    _require(all(anchors[p] == 1 for p in patients), 'CTC requires exactly one index tumor per patient')
    with zipfile.ZipFile(path) as archive:
        metadata = archive.read('ctc_metadata.json')
    return header, rows, metadata


class _Archives:
    def __init__(self, out, resolver):
        self.out, self.resolver = out, resolver
        self.entries, self.destinations, self.visited, self.omitted = {}, {}, set(), []

    def add(self, pin, destination=None):
        source = self.resolver(pin)
        destination = destination or self.out / 'inputs/workspace' / source.relative_to(self.resolver.root)
        destination = Path(destination)
        _require(destination.is_relative_to(self.out / 'inputs'), 'Archive destination escapes inputs')
        # Absolute and relative aliases must produce one replay binding.
        key = str(source), pin['sha256']
        if key in self.entries:
            return self.entries[key]['archive']
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination in self.destinations:
            _require(self.destinations[destination] == pin['sha256'], 'Archive path collision')
        else:
            with source.open('rb') as src, destination.open('xb') as dst:
                shutil.copyfileobj(src, dst)
            _require(digest(destination) == pin['sha256'], 'Input changed while archiving')
            self.destinations[destination] = pin['sha256']
        archive_pin = self.resolver.pin(destination)
        self.entries[key] = {'original': dict(pin), 'archive': archive_pin}
        return archive_pin

    def reference(self, pin, owner):
        """Workspace paths stay rooted; bundle-relative paths stay with their owner."""
        path = Path(pin['path'])
        if not path.is_absolute() and path.parts[0] not in ('reference', 'experiments', 'runs', 'releases', 'data', 'nccr-metadata'):
            path = owner.parent / path
        path = self.resolver.path(path)
        return {'path': str(path.relative_to(self.resolver.root)), 'sha256': pin['sha256']}

    def graph(self, pin):
        source = self.resolver(pin)
        archive_pin = self.add(pin)
        if source in self.visited or source.suffix.lower() != '.json':
            return
        self.visited.add(source)
        data = read_json(source)

        # Snapshot/lookup manifests declare a CLOSED inventory. Archived JSON
        # members can contain historical capture provenance pointing outside the
        # workspace; those references are evidence, never new runtime inputs.
        if isinstance(data, dict) and isinstance(data.get('files'), list):
            seen = set()
            for item in data['files']:
                _require(type(item) is dict and {'path', 'sha256'} <= set(item), 'Invalid snapshot file entry')
                relative = Path(item['path'])
                original = (source.parent / relative).resolve()
                _require(not relative.is_absolute() and original.is_relative_to(source.parent),
                         'Snapshot file escapes bundle')
                _require(original not in seen, 'Duplicate snapshot file')
                seen.add(original)
                member = {'path': str(original.relative_to(self.resolver.root)), 'sha256': item['sha256']}
                self.add(member, self.resolver.path(archive_pin['path']).parent / relative)
            self.omitted.append({'manifest': dict(pin), 'reason':
                                 'Closed files inventory; historical provenance is archived as bytes, not followed'})
            return

        def visit(value):
            if isinstance(value, list):
                for item in value:
                    visit(item)
            elif isinstance(value, dict):
                if 'path' in value and 'sha256' in value:
                    nested = self.reference({k: value[k] for k in ('path', 'sha256')}, source)
                    self.graph(nested)
                for key, item in value.items():
                    if key.endswith('_path') and key[:-5] + '_sha256' in value:
                        self.graph(self.reference({'path': item, 'sha256': value[key[:-5] + '_sha256']}, source))
                    if isinstance(item, (dict, list)):
                        visit(item)
        if isinstance(data, dict) and isinstance(data.get('rules'), list) and 'version' in data:
            for key in ('composition', 'derived_from', 'supersedes', 'lookups', 'frequency_references'):
                visit(data.get(key))
            for rule in data['rules']:
                for key in ('evidence', 'reference_snapshot'):
                    visit(rule.get(key))
        elif isinstance(data, dict) and isinstance(data.get('blocks'), list):
            visit(data['blocks'])
        # All other referenced documents are leaf evidence. Their exact bytes
        # retain provenance without granting permission to traverse it.


def _context(baseline, resolver, expected):
    manifest_path = resolver(baseline['context'])
    manifest = read_json(manifest_path)
    contract = read_json(resolver(baseline['context_contract']))
    _require(manifest.get('context_schema_sha256') == baseline['context_contract']['sha256'],
             'Context schema fingerprint mismatch')
    bindings = contract.get('raw_bindings')
    _require(type(bindings) is dict, 'Missing context raw_bindings')
    fields = [key[4:] for key in bindings if key.startswith('CTC.')]
    _require(len(fields) == 27 and all(fields), 'Expected 27 CTC context bindings')
    sidecars = []
    for kind, count in (('patient', expected['patients']), ('tumor', expected['rows'])):
        item = manifest[kind]
        _integer(item.get('rows'), 'Context row count')
        _require(item['rows'] == count, 'Context row count mismatch')
        relative = Path(item['path'])
        path = (manifest_path.parent / relative).resolve()
        _require(not relative.is_absolute() and path.is_relative_to(manifest_path.parent),
                 'Context sidecar escapes context directory')
        pin = {'path': str(path), 'sha256': item['sha256']}
        resolver(pin)
        # Sidecar semantics belong to the independent validator; count/shape still checked here.
        with path.open(encoding='utf-8', newline='') as stream:
            _, rows = _csv_rows(stream)
            _require(sum(1 for _ in rows) == count, 'Actual context row count mismatch')
        sidecars.append((relative, pin))
    return sorted(set(fields) | set(IDENTITY)), sidecars


def _preservation(before, after, header, protected, blocks, hierarchy):
    _require(type(after) is list and len(after) == len(before), 'Sampler changed row count')
    owned = {field for block in blocks for rule in block['rules'] for field in rule['outputs']}
    if hierarchy['leaf_column'] in owned:
        owned.update(hierarchy['parent_columns'])
    unchanged = set(header) - owned | set(protected)
    for old, new in zip(before, after):
        _require(type(new) is dict and set(new) == set(header) and all(isinstance(v, str) for v in new.values()),
                 'Sampler changed row schema/string representation')
        _require(all(old[k] == new[k] for k in IDENTITY), 'Sampler changed ordered patient/tumor keys')
        _require(all(old[k] == new[k] for k in unchanged), 'Sampler changed a protected or non-owned field')
    return sorted(unchanged)


def _write_ctc(path, header, rows, metadata):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for name in ('CTC.csv', 'ctc_metadata.json'):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            with archive.open(info, 'w', force_zip64=True) as binary:
                if name.endswith('.json'):
                    binary.write(metadata)
                else:
                    with io.TextIOWrapper(binary, encoding='utf-8', newline='') as stream:
                        writer = csv.DictWriter(stream, fieldnames=header, lineterminator='\n')
                        writer.writeheader()
                        writer.writerows(rows)


def _code(out, resolver):
    result = []
    for name in CODE_MODULES:
        source = Path(__file__).resolve().parent / (name + '.py')
        destination = out / 'inputs/code/jamboree_sdv' / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = source.read_bytes()
        with destination.open('xb') as stream:
            stream.write(payload)
        result.append({'original': {'path': str(source), 'sha256': hashlib.sha256(payload).hexdigest()},
                       'archive': resolver.pin(destination)})
    return result


def run(recipe_path, workspace, out):
    """Create a fresh CTC-only candidate and return (candidate dict, report dict).

    Generation success is not independent validation. Failed runs can leave
    diagnostic output; they never replace an existing directory or baseline.
    """
    started = now()
    clock = time.monotonic()
    resolver = WorkspaceResolver(workspace)
    out = _output(out, resolver)
    recipe, recipe_pin = _recipe(recipe_path, resolver)
    baseline = read_json(resolver(recipe['baseline']))
    inventories = _inventories(baseline, resolver, recipe['expected'])
    ctc_entry = inventories['files']['CTC']
    ctc = resolver.path(ctc_entry['local_path'])
    protected, sidecars = _context(baseline, resolver, recipe['expected'])
    for key in ('run_manifest', 'promotion'):
        resolver(baseline[key])
    spec, parents = load_rules(recipe['rules'], resolver)
    profile = read_json(resolver(recipe['profile']))
    selected = validate_profile(profile, spec, recipe['snapshot'], resolver)
    hierarchy = {key: deepcopy(selected[key]) for key in ('leaf_column', 'parent_columns', 'mapping')}
    preflight = _preflight(ctc, recipe['expected'], out, profile['blocks'])
    header, rows, metadata = _read_ctc(ctc, recipe['expected'])
    _require(set(protected) <= set(header), 'Missing protected CTC context columns')

    # Claim output atomically and archive effective inputs before sampling.
    out.mkdir(parents=True, exist_ok=False)
    archives = _Archives(out, resolver)
    archives.add(recipe_pin, out / 'inputs/recipe.json')
    archives.add(recipe['baseline'], out / 'inputs/baseline/release.json')
    for key in ('run_manifest', 'promotion'):
        archives.add(baseline[key], out / 'inputs/baseline' / (key + '.json'))
    context_pin = archives.add(baseline['context'], out / 'inputs/context/manifest.json')
    contract_pin = archives.add(baseline['context_contract'], out / 'inputs/context/contract.json')
    for relative, pin in sidecars:
        archives.add(pin, out / 'inputs/context' / relative)
    for key in ('rules', 'profile', 'snapshot'):
        archives.graph(recipe[key])
    code_pins = _code(out, resolver)

    newrows, blocks_report = apply_blocks(rows, profile['blocks'], resolver, recipe['seed'],
                                         protected_fields=protected, hierarchy=hierarchy)
    unchanged = _preservation(rows, newrows, header, protected, profile['blocks'], hierarchy)
    output_file = out / 'data/CTC.zip'
    _write_ctc(output_file, header, newrows, metadata)
    # Check inputs again before emitting the success marker; no baseline writes occur.
    for name in ('files', 'raw_files'):
        for item in baseline[name]:
            resolver({'path': item['local_path'], 'sha256': item['sha256']})
    for entry in archives.entries.values():
        resolver(entry['original'])
        resolver(entry['archive'])
    for entry in code_pins:
        _require(digest(entry['original']['path']) == entry['original']['sha256'], 'Code changed during generation')

    replacement = {'source': 'CTC', 'local_path': str(output_file), 'sha256': digest(output_file),
                   **recipe['expected'], 'size_bytes': output_file.stat().st_size}
    untouched = [{'inventory': name, **deepcopy(item)} for name in ('files', 'raw_files')
                 for item in baseline[name] if item['source'] != 'CTC']
    report = {
        'schema_version': 1, 'kind': 'gen_sdv_ctc_replacement_generation',
        'recipe_id': recipe['recipe_id'], 'started_at': started, 'finished_at': now(),
        'elapsed_seconds': time.monotonic() - clock,
        'baseline': recipe['baseline'], 'recipe': recipe_pin, 'rules': recipe['rules'],
        'profile': recipe['profile'], 'snapshot': recipe['snapshot'], 'seed': recipe['seed'],
        'parent_rule_versions': sorted(parents), 'hierarchy_rule_id': selected['id'],
        'protected_fields': protected, 'unchanged_fields': unchanged, 'counts': recipe['expected'],
        'blocks': blocks_report, 'ctc': replacement, 'untouched_files': untouched,
        'resource_preflight': preflight,
        'environment': {'python': sys.version, 'platform': sys.platform,
                        'numpy': importlib.metadata.version('numpy'),
                        'copulas': importlib.metadata.version('copulas'),
                        'sampling_method': 'categorical conditional count tuples using NumPy named streams'},
        'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == 'darwin' else 1024),
        'archived_inputs': list(archives.entries.values()), 'code_pins': code_pins,
        'unarchived_references': archives.omitted,
        'audit_status': 'pending', 'independent_validation_required': True,
        'promoted': False, 'aws_upload': False, 'deployment': False,
    }
    write_json(out / 'generation.json', report)
    candidate = deepcopy(baseline)
    for key in ('promotion', 'verification', 'run_manifest', 'candidate_assessment', 'assessment',
                'audit', 'audit_status', 'verified', 'status'):
        candidate.pop(key, None)
    candidate.update({'kind': 'gen_sdv_ctc_replacement_candidate', 'release_id': recipe['recipe_id'],
                      'created_at': now(), 'rules_version': spec['version'],
                      'rules': archives.add(recipe['rules']), 'context': context_pin,
                      'context_contract': contract_pin, 'promoted': False, 'aws_upload': False,
                      'deployment': False, 'audit_status': 'pending',
                      'replacement': {'baseline': recipe['baseline'], 'recipe': recipe_pin,
                                      'generation': resolver.pin(out / 'generation.json'),
                                      'independent_validation_required': True}})
    for name in ('files', 'raw_files'):
        candidate[name] = [deepcopy(replacement if item['source'] == 'CTC' else item) for item in baseline[name]]
    write_json(out / 'candidate.json', candidate)
    return candidate, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recipe', required=True)
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    candidate, report = run(args.recipe, args.workspace, args.out)
    print(json.dumps({'release_id': candidate['release_id'], 'audit_status': report['audit_status'],
                      'out': str((Path(args.workspace) / args.out).resolve())}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
