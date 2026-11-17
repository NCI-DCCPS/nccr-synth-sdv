"""Independent output checks for shared CCDI/CPI catalog rules.

Only declarative input loading is shared with the adapter. Projection, identity,
membership and frequency computations do not call generation code.
"""
from collections import Counter
import argparse
import csv
import io
import math
from pathlib import Path
import shutil
import sys
import zipfile

from jamboree_sdv import catalog_contract, common


def require(condition, message):
    if not condition:
        raise ValueError(message)


def table(payload):
    return list(csv.reader(io.StringIO(payload.decode('utf-8'), newline=''), strict=True))


def check(raw_payload, cpi_payload, ctc_ids, contract):
    projection = contract['projection']
    raw = table(raw_payload)
    spec = projection['raw_ccdi']
    width = (len(raw[0])-1)//2 if raw else 0
    header = [spec['patient_key']] + [f'{field}{i}' for i in range(spec['first_suffix'], width+1)
                                     for field in spec['numbered_pair_columns']]
    require(width > 0 and raw[0] == header, 'Invalid raw CCDI header')
    seen = set()
    triples = []
    for row in raw[1:]:
        require(len(row) == len(header) and row[0].strip() and row[0].casefold() not in seen,
                'Invalid or duplicate raw patient')
        seen.add(row[0].casefold())
        ended = False
        mappings = 0
        for index in range(1, len(row), 2):
            pair = row[index:index+2]
            if pair == ['', '']:
                ended = True
            else:
                require(all(v.strip() for v in pair) and not ended, 'Incomplete or gapped mapping pair')
                triples.append((row[0], *pair))
                mappings += 1
        require(mappings >= spec['minimum_mappings_per_patient'], 'Patient has no mapping pair')
    cpi = table(cpi_payload)
    require(cpi and cpi[0] == projection['cpi']['columns'], 'Invalid CPI header')
    require(all(len(row) == 3 and all(v.strip() for v in row) for row in cpi[1:]), 'Invalid CPI row')
    processed = list(map(tuple, cpi[1:]))
    require(len(set(triples)) == len(triples) and len(set(processed)) == len(processed),
            'Invalid duplicate mapping')
    require(set(triples) == set(processed), 'Raw CCDI/CPI projection mismatch')
    require(bool(triples), 'No mappings; frequency cannot be evaluated')
    require({r[0] for r in triples} <= set(ctc_ids), 'Patient not in CTC')
    identities = {}
    allowed = {item[contract['membership']['lookup_key']] for item in contract['catalog']}
    for patient, participant, name in triples:
        require(name in allowed, 'Resource outside catalog: ' + name)
        key = (name, participant)
        require(key not in identities or identities[key] == patient, 'Ambiguous resource participant identity')
        identities[key] = patient

    # Independently derive targets from original counts, not the sampler report.
    counts = contract['counts']
    retained = {name: n for name, n in counts.items() if name in allowed and n > 0}
    retained_total = sum(retained.values())
    observed = Counter(r[2] for r in triples)
    require(set(observed) <= set(retained), 'Observed frequency outside explicit profile support')
    alpha = contract['profile']['frequency_check']['family_alpha']
    tolerance = math.sqrt(math.log(2*len(retained)/alpha)/(2*len(triples)))
    probabilities = {name: n/retained_total for name, n in retained.items()}
    deviations = {name: abs(observed[name]/len(triples)-p) for name, p in probabilities.items()}
    require(all(value <= tolerance for value in deviations.values()), 'Observed frequency exceeds declared tolerance')
    return {'schema_version': 1, 'scope': 'CCDI/CPI catalog rules only', 'status': 'passed',
            'rules_version': contract['rules_version'], 'inputs': contract['inputs'],
            'evaluated_rules': contract['evaluated_rules'],
            'not_evaluated_rules': contract['not_evaluated_rules'],
            'disabled_rules': contract['disabled_rules'], 'full_rule_set_qualified': False,
            'patient_count': len(seen), 'mapping_count': len(triples),
            'catalog_count': len(allowed), 'observed_counts': dict(observed),
            'published_counts': counts, 'total_records': contract['total_records'],
            'reported_count': sum(counts.values()),
            'unreported_count': contract['total_records']-sum(counts.values()),
            'excluded_counts': {k: v for k, v in counts.items() if k not in retained},
            'retained_count': retained_total, 'retained_known_mass': retained_total/sum(counts.values()),
            'catalog_domains_without_positive_public_mass': sorted(allowed-set(retained)),
            'effective_targets': probabilities, 'frequency_family_alpha': alpha,
            'frequency_tolerance': tolerance, 'max_frequency_deviation': max(deviations.values())}


def zipped_csv(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)) and all(Path(n).name == n for n in names),
                'Unsafe or duplicate ZIP members')
        require(archive.testzip() is None, 'ZIP CRC failure')
        members = [n for n in names if n.endswith('.csv')]
        require(len(members) == 1, 'Expected one CSV in archive')
        return archive.read(members[0])


def verify_release(manifest_pin, rules_pin, profile_pin, *, workspace=common.WORKSPACE):
    contract = catalog_contract.load_catalog_contract(rules_pin, profile_pin, workspace=workspace)
    manifest = common.read_json(catalog_contract.checked(manifest_pin, workspace))
    files = {f['source']: f for f in manifest['files']}
    require(len(files) == len(manifest['files']), 'Duplicate release source')
    require({'CTC', 'CCDI', 'CPI'} <= set(files), 'Required source missing; catalog rules not evaluated')
    paths = {}
    for source in ['CTC', 'CCDI', 'CPI']:
        item = files[source]
        paths[source] = catalog_contract.checked({'path': item['local_path'], 'sha256': item['sha256']}, workspace)
        require(paths[source].stat().st_size == item['size_bytes'], 'Payload size mismatch')
    ctc = table(zipped_csv(paths['CTC']))
    require(ctc and ctc[0].count('nccrId') == 1 and len(set(ctc[0])) == len(ctc[0]), 'Invalid CTC header')
    index = ctc[0].index('nccrId')
    require(all(len(r) == len(ctc[0]) and r[index].strip() for r in ctc[1:]), 'Invalid CTC patient row')
    report = check(zipped_csv(paths['CCDI']), paths['CPI'].read_bytes(), {r[index] for r in ctc[1:]}, contract)
    report['inputs']['release_manifest'] = manifest_pin
    report['verified_payloads'] = [files[s] for s in paths]
    report['payload_sources_not_checked'] = sorted(set(files)-set(paths))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, default=common.WORKSPACE)
    for key in ['manifest', 'rules', 'profile']:
        parser.add_argument('--'+key, required=True)
        parser.add_argument('--'+key+'-sha256', required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    out = common.output_path(args.out)
    pins = [{ 'path': getattr(args, k), 'sha256': getattr(args, k+'_sha256') }
            for k in ['manifest', 'rules', 'profile']]
    report = verify_release(*pins, workspace=args.workspace)
    out.mkdir(parents=True)
    report.update(created_at=common.now(), command=argv if argv is not None else sys.argv[1:],
                  python=sys.version, archived_inputs={}, code=[])
    for key, item in report['inputs'].items():
        src = catalog_contract.checked(item, args.workspace)
        dst = out/'inputs'/(key+'.json')
        dst.parent.mkdir(exist_ok=True)
        shutil.copyfile(src, dst)
        report['archived_inputs'][key] = {'path': str(dst), 'sha256': common.digest(dst)}
    # Archive the exact independent output algorithm and declarative input reader.
    paths = [(Path(__file__), out/'verifier'/Path(__file__).name)]
    for name in ['__init__.py', 'common.py', 'catalog_contract.py']:
        paths.append((Path(common.__file__).parent/name, out/'verifier'/'jamboree_sdv'/name))
    for src, dst in paths:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        report['code'].append({'path': str(src), 'archived_path': str(dst), 'sha256': common.digest(dst)})
    common.write_json(out/'audit.json', report)
    (out/'summary.md').write_text(
        f"# Scoped CCDI/CPI rule assessment\n\nPassed {len(report['evaluated_rules'])} catalog rules for "
        f"{report['patient_count']} patients and {report['mapping_count']} mappings.\n\n"
        "This is not full-rule-set qualification. Other rules and payloads are listed as unevaluated in audit.json.\n")
    print(str(out/'audit.json'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
