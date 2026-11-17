"""Capture public, exact-name RxTerms/NDC support; never called by generation.

Usage: python capture_pharm_rxnorm.py CAPTURE_DIRECTORY WORKSPACE
The directory must already contain the RxTerms ZIP and version responses.
Responses and receipts are retained, including rejected candidates.
"""
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import sys
import time
from urllib.request import urlopen
import zipfile


def capture(out, root):
    def get(query):
        url = 'https://rxnav.nlm.nih.gov/REST/' + query
        key = hashlib.sha256(url.encode()).hexdigest()
        path = out / (key + '.json')
        if not path.exists():
            time.sleep(0.22)
            with urlopen(url, timeout=45) as response:
                raw = response.read()
                headers = dict(response.headers)
            json.loads(raw)
            path.write_bytes(raw)
            path.with_suffix('.receipt.json').write_text(json.dumps({
                'url': url, 'retrieved_at': datetime.now(timezone.utc).isoformat(),
                'sha256': hashlib.sha256(raw).hexdigest(), 'headers': headers}, indent=2) + '\n')
        return json.loads(path.read_bytes()), path.name

    canmed = json.loads((root / 'reference/generation-rules/lookups/canmed-ndc/2026-11-12/canmed-ndc.json').read_text())
    keys = {r['fields']['medicationRequestNdc11'] for r in canmed['records']}
    keys.update(r['ndc11'] for r in canmed['excluded_records'])
    public = json.loads((root / 'reference/nccr-public/2026-11-09/normalized/pharm.json').read_text())
    targets = public['fields']['rxnormDisplayName']['charts'][0]['values']
    with zipfile.ZipFile(out / 'RxTerms202611.zip') as z:
        rows = list(csv.DictReader(io.StringIO(z.read('RxTerms202611.txt').decode()), delimiter='|'))
    records, excluded = [], []
    for target in targets:
        name = target['value']
        if name == 'Missing' or name.startswith('Not sought'):
            continue
        candidates = sorted((r for r in rows if r['DISPLAY_NAME'] == name and
                             not r['SUPPRESS_FOR'] and not r['IS_RETIRED']), key=lambda r: int(r['RXCUI']))
        attempts = []
        found = False
        for r in candidates:
            rid = r['RXCUI']
            response, ndc_file = get(f'rxcui/{rid}/ndcs.json')
            ndcs = sorted(set(response.get('ndcGroup', {}).get('ndcList', {}).get('ndc', [])) - keys)
            attempts.append({'rxcui': rid, 'ndcs_response': ndc_file, 'eligible_ndcs': len(ndcs)})
            for ndc in ndcs:
                if len(ndc) != 11 or not ndc.isascii() or not ndc.isdigit():
                    continue
                status, status_file = get(f'ndcstatus.json?ndc={ndc}&altpkg=0&history=0')
                s = status.get('ndcStatus', {})
                if (s.get('ndc11') != ndc or s.get('rxcui') != rid or s.get('status') != 'ACTIVE'
                    or s.get('altNdc') != 'N' or s.get('conceptName') != r['FULL_NAME']):
                    continue
                classes, class_file = get(f'rxclass/class/byRxcui.json?rxcui={rid}&relaSource=ATCPROD')
                direct = [c for c in classes.get('rxclassDrugInfoList', {}).get('rxclassDrugInfo', [])
                          if c['minConcept']['rxcui'] == rid and c['relaSource'] == 'ATCPROD']
                class_names = sorted({c['rxclassMinConceptItem']['className'] for c in direct})
                fields = {'medicationRequestNdc11': ndc, 'medicationRequestNdc9': ndc[:9],
                          'rxnormRxcui': rid, 'rxnormConceptName': r['FULL_NAME'],
                          'rxnormDisplayName': name, 'rxnormStrength': r['STRENGTH'] or '9-Missing',
                          'rxnormDoseForm': r['RXN_DOSE_FORM'] or '9-Missing',
                          'rxnormAtcprodClass': '; '.join(class_names) or '9-Missing'}
                records.append({'fields': fields, 'rxterms_record': r,
                                'responses': {'ndcs': ndc_file, 'status': status_file, 'classes': class_file},
                                'direct_class_relationships': direct,
                                'absent_properties': [f for f, value in fields.items() if value == '9-Missing'],
                                'selection_attempts': attempts})
                found = True
                break
            if found:
                break
        if not found:
            excluded.append({'public_name': name, 'count': target['count'], 'attempts': attempts,
                             'reason': 'No active exact-display-name RxTerms concept with a verified NDC outside CanMED'})
        print(len(records), name, 'accepted' if found else 'excluded', flush=True)
    result = {'schema_version': 1, 'records': records, 'excluded_public_names': excluded,
              'selection_policy': 'First numeric RxCUI with exact active unsuppressed display name, then first lexical NDC outside complete CanMED key set with exact active status; no alternate package substitution.',
              'class_policy': 'Only ATCPROD relationships returned for the exact selected RxCUI; retain all distinct class names sorted and joined by semicolon-space. Absence uses 9-Missing.',
              'association_policy': 'Current association at captured release; history retained in status response, not interpreted as historical availability.'}
    (out / 'rxnorm-ndc.json').write_text(json.dumps(result, indent=2) + '\n')
    print('Finished:', len(records), 'supported names;', len(excluded), 'excluded', flush=True)


if __name__ == '__main__':
    capture(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
