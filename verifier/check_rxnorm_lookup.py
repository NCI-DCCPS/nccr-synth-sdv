"""Verify normalized RxNorm tuples against retained authoritative bytes, offline."""
import csv
import hashlib
import io
import json
from pathlib import Path
import zipfile


def require(value, message):
    if not value:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify(directory, canmed):
    directory = Path(directory)
    receipts = {}
    for path in directory.glob('*.receipt.json'):
        record = json.loads(path.read_bytes())
        raw_name = path.name.removesuffix('.receipt.json')
        if not (directory / raw_name).exists():
            raw_name += '.json'
        require(sha(directory / raw_name) == record['sha256'], 'Raw lookup response hash mismatch')
        require(record['retrieved_at'] and record['url'].startswith('https://'), 'Missing retrieval evidence')
        receipts[raw_name] = record
    require(len(receipts) >= 7, 'Incomplete source receipts')
    for source in ['rxnorm', 'rxterms', 'atcprod']:
        require(json.loads((directory / f'{source}-version.json').read_bytes()) ==
                json.loads((directory / f'{source}-version-after.json').read_bytes()), 'Source version drift')
    with zipfile.ZipFile(directory / 'RxTerms202611.zip') as z:
        terms = {r['RXCUI']: r for r in csv.DictReader(io.StringIO(z.read('RxTerms202611.txt').decode()), delimiter='|')}
    require(json.loads((directory / 'rxterms-version.json').read_bytes())['rxtermsVersion'] == 'RxTerms202611', 'RxTerms version mismatch')
    lookup = json.loads((directory / 'rxnorm-ndc.json').read_bytes())
    canmed_keys = {r['fields']['medicationRequestNdc11'] for r in canmed['records']} | {r['ndc11'] for r in canmed['excluded_records']}
    seen = set()
    for record in lookup['records']:
        f = record['fields']; rid = f['rxnormRxcui']; ndc = f['medicationRequestNdc11']
        require(ndc not in seen and ndc not in canmed_keys, 'Duplicate or CanMED-overlapping NDC')
        seen.add(ndc)
        r = terms[rid]
        require(not r['SUPPRESS_FOR'] and not r['IS_RETIRED'], 'Suppressed or retired concept')
        require(record['rxterms_record'] == r, 'Altered RxTerms source record')
        replies = {}
        expected_queries = {'ndcs': f'rxcui/{rid}/ndcs.json',
            'status': f'ndcstatus.json?ndc={ndc}&altpkg=0&history=0',
            'classes': f'rxclass/class/byRxcui.json?rxcui={rid}&relaSource=ATCPROD'}
        for key, name in record['responses'].items():
            require(receipts[name]['url'] == 'https://rxnav.nlm.nih.gov/REST/' + expected_queries[key], 'Wrong query binding')
            replies[key] = json.loads((directory / name).read_bytes())
        require(ndc in replies['ndcs']['ndcGroup']['ndcList']['ndc'], 'NDC missing from concept response')
        status = replies['status']['ndcStatus']
        require(status['ndc11'] == ndc and status['rxcui'] == rid and status['altNdc'] == 'N' and
                status['status'] == 'ACTIVE' and status['conceptName'] == r['FULL_NAME'], 'NDC concept mismatch')
        relationships = [c for c in replies['classes'].get('rxclassDrugInfoList', {}).get('rxclassDrugInfo', [])
                         if c['relaSource'] == 'ATCPROD' and c['minConcept']['rxcui'] == rid]
        expected = {'medicationRequestNdc11': ndc, 'medicationRequestNdc9': ndc[:9],
            'rxnormRxcui': rid, 'rxnormConceptName': r['FULL_NAME'], 'rxnormDisplayName': r['DISPLAY_NAME'],
            'rxnormStrength': r['STRENGTH'] or '9-Missing', 'rxnormDoseForm': r['RXN_DOSE_FORM'] or '9-Missing',
            'rxnormAtcprodClass': '; '.join(sorted({c['rxclassMinConceptItem']['className'] for c in relationships})) or '9-Missing'}
        require(f == expected, 'Normalized RxNorm tuple differs from source')
        require(record['direct_class_relationships'] == relationships, 'Altered class evidence')
        require(set(record['absent_properties']) == {k for k, v in expected.items() if v == '9-Missing'}, 'Missingness evidence mismatch')
    return {'status': 'passed', 'verified_tuples': len(seen), 'verified_response_receipts': len(receipts)}
