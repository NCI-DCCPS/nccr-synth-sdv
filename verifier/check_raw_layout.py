"""Independent raw/processed equivalence check; standard library, no generator imports."""
from collections import Counter
import csv
import hashlib
import io
from pathlib import Path
import zipfile

SOURCES = ['CTC', 'ABM', 'COG', 'MCE', 'MCD', 'MCP', 'PHARM', 'RO', 'CCDI', 'CPI']
MAPPING = ['nccrId', 'resourceParticipantId', 'resourceName']


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def csv_in_zip(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)) and all(Path(n).name == n for n in names),
                'Invalid raw ZIP members')
        require(archive.testzip() is None, 'Raw ZIP CRC failure')
        members = [n for n in names if n.endswith('.csv')]
        require(len(members) == 1, 'Expected one CSV per ZIP')
        return archive.read(members[0]), {n: archive.read(n) for n in names if n != members[0]}


def verify(raw_files, processed_files, resolve=None):
    resolve = resolve or (lambda path, expected: Path(path))
    require([f['source'] for f in raw_files] == SOURCES, 'Incomplete raw inventory')
    require([f['source'] for f in processed_files] == SOURCES, 'Incomplete processed inventory')
    raw, processed = {}, {}
    for inventory, paths in [(raw_files, raw), (processed_files, processed)]:
        for item in inventory:
            path = resolve(item['local_path'], item['sha256'])
            require(sha(path) == item['sha256'] and path.stat().st_size == item['size_bytes'],
                    'Raw/processed payload hash or size mismatch: ' + item['source'])
            paths[item['source']] = path
    for source in SOURCES[:-2]:
        require(sha(raw[source]) == sha(processed[source]), 'Unexpected raw conversion: ' + source)
    counts = {f['source']: f for f in raw_files}
    wide, raw_metadata = csv_in_zip(raw['CCDI'])
    flat, processed_metadata = csv_in_zip(processed['CCDI'])
    require(raw_metadata == processed_metadata, 'CCDI processed metadata was changed')
    rows = list(csv.reader(io.StringIO(wide.decode('utf-8'), newline='')))
    require(bool(rows) and len(rows[0]) >= 3 and len(rows[0]) % 2 == 1, 'Invalid raw CCDI width')
    width = (len(rows[0]) - 1) // 2
    require(rows[0] == ['nccrId'] + [f'{field}{i}' for i in range(1, width + 1) for field in MAPPING[1:]],
            'Raw CCDI requires numbered mapping pairs')
    seen, triples = set(), []
    for row in rows[1:]:
        require(len(row) == len(rows[0]) and row[0], 'Malformed raw CCDI row')
        require(row[0].lower() not in seen, 'Duplicate raw CCDI patient')
        seen.add(row[0].lower())
        ended = False
        for i in range(1, len(row), 2):
            pair = row[i:i + 2]
            if pair == ['', '']:
                require(i != 1, 'Raw CCDI patient has no mapping')
                ended = True
            else:
                require(all(pair) and not ended, 'Incomplete raw CCDI pair or gap')
                triples.append((row[0], *pair))
    logical = list(csv.reader(io.StringIO(flat.decode('utf-8'), newline='')))
    require(logical and logical[0] == MAPPING, 'Unexpected processed CCDI header')
    require(Counter(triples) == Counter(map(tuple, logical[1:])), 'Raw CCDI mapping mismatch')
    cpi, _ = csv_in_zip(processed['CPI'])
    require(raw['CPI'].read_bytes() == cpi, 'Raw CPI CSV changed bytes')
    cpi_rows = list(csv.reader(io.StringIO(cpi.decode('utf-8'), newline='')))
    require(cpi_rows and cpi_rows[0] == MAPPING and
            Counter(map(tuple, cpi_rows[1:])) == Counter(set(triples)), 'CPI projection mismatch')
    require(bool(seen), 'Empty raw CCDI is unsupported')
    require(counts['CCDI']['rows'] == counts['CCDI']['patients'] == len(seen), 'Incorrect raw CCDI count')
    require(counts['CPI']['rows'] == len(cpi_rows)-1 and counts['CPI']['patients'] == len(seen),
            'Incorrect raw CPI count')
    return {'status': 'passed_raw_layout', 'ccdi_patient_rows': len(seen),
            'ccdi_mapping_rows': len(triples), 'ccdi_mapping_pairs_per_row': width,
            'cpi_rows': len(cpi_rows) - 1, 'unchanged_raw_zips': 8}
