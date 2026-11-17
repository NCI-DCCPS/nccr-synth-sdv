"""Offline PHARM tuple enrichment of an existing, linked SDV candidate.

This is a scoped postprocessor, not a replacement for the full-recipe loader.
Published counts select drug groups; lookup multiplicity never sets group usage.
"""
from collections import defaultdict
import csv
from fractions import Fraction
import io
import random
import zipfile


CANMED = ['canmedNonProprietaryName', 'canmedProprietaryName', 'canmedDrugCategory',
          'canmedMajorDrugClass', 'canmedMinorDrugClass']
RXNORM = ['rxnormRxcui', 'rxnormConceptName', 'rxnormDisplayName', 'rxnormStrength',
          'rxnormDoseForm', 'rxnormAtcprodClass']
DRUG_FIELDS = ['medicationRequestNdc9', 'medicationRequestNdc11', *CANMED, *RXNORM]
NO_CANMED = '7-No CanMED Data Available'
NOT_SOUGHT = {k: '7777777' if k == 'rxnormRxcui' else '7-Not Sought in RxNorm' for k in RXNORM}
MISSING = {k: '9999999' if k == 'rxnormRxcui' else '9-Missing' for k in RXNORM}


def allocate(weights, count):
    """Exact largest-remainder allocation; lexicographic tie breaking."""
    if count < 0 or not weights or any(v <= 0 for v in weights.values()):
        raise ValueError('Invalid positive weights or count')
    total = sum(weights.values())
    expected = {k: Fraction(v * count, total) for k, v in weights.items()}
    result = {k: int(v) for k, v in expected.items()}
    order = sorted(weights, key=lambda k: (-(expected[k] - result[k]), k))
    for key in order[:count - sum(result.values())]:
        result[key] += 1
    return result


def values(public, field):
    return {r['value']: r['count'] for r in public['fields'][field]['charts'][0]['values']}


def build_plan(public, canmed, rxnorm, count):
    groups = []
    seen = set()
    for record in canmed['records']:
        key = record['fields']['medicationRequestNdc11']
        if key in seen:
            raise ValueError('duplicate CanMED NDC')
        seen.add(key)
    seen.update(r['ndc11'] for r in canmed['excluded_records'])
    rx_seen = set()
    for record in rxnorm['records']:
        key = record['fields']['medicationRequestNdc11']
        if key in seen:
            raise ValueError('RxNorm NDC occurs in complete CanMED source')
        if key in rx_seen:
            raise ValueError('duplicate RxNorm NDC')
        rx_seen.add(key)
    categories = values(public, 'canmedDrugCategory')
    if sum(categories.values()) != public['total_records']:
        raise ValueError('Category support must account for full public denominator')
    category_counts = allocate(categories, count)
    generic = values(public, 'canmedNonProprietaryName')
    names = {name.casefold(): n for name, n in generic.items() if not name.startswith('Not found')}
    available = defaultdict(lambda: defaultdict(list))
    for record in canmed['records']:
        fields = record['fields']
        name = fields['canmedNonProprietaryName'].casefold()
        if name in names:
            available[fields['canmedDrugCategory']][name].append(fields)
    category_only = {}
    for category, n in category_counts.items():
        if category == NO_CANMED:
            continue
        if not available[category]:
            for record in canmed['records']:
                fields = record['fields']
                if fields['canmedDrugCategory'] == category:
                    available[category][fields['canmedNonProprietaryName'].casefold()].append(fields)
            if not available[category]:
                raise ValueError(f'No lookup support for category {category}')
            category_only[category] = sorted(available[category])
        weights = {name: names[name] if category not in category_only else 1 for name in available[category]}
        for name, k in allocate(weights, n).items():
            tuples = [{**f, **NOT_SOUGHT} for f in sorted(available[category][name], key=lambda f: f['medicationRequestNdc11'])]
            groups.append({'state': 'canmed_matched', 'category': category, 'name': name,
                           'public_weight': weights[name] if category not in category_only else None,
                           'allocation_basis': 'published_name_conditioned_on_category' if category not in category_only else 'published_category_count_with_uniform_unreported_names',
                           'count': k, 'tuples': tuples})
    rx_public = values(public, 'rxnormDisplayName')
    missing_count = round(Fraction(rx_public['Missing'] * count, public['total_records']))
    rx_count = category_counts[NO_CANMED] - missing_count
    if rx_count < 0:
        raise ValueError('Inconsistent missingness counts')
    rx_by_name = defaultdict(list)
    for record in rxnorm['records']:
        f = record['fields']
        if f['rxnormDisplayName'] not in rx_public:
            raise ValueError('RxNorm name has no published frequency support')
        rx_by_name[f['rxnormDisplayName']].append({**f, **dict.fromkeys(CANMED, NO_CANMED)})
    rx_weights = {name: rx_public[name] for name in rx_by_name}
    for name, k in allocate(rx_weights, rx_count).items():
        groups.append({'state': 'rxnorm_matched', 'name': name, 'public_weight': rx_weights[name],
                       'count': k, 'tuples': sorted(rx_by_name[name], key=lambda f: f['medicationRequestNdc11'])})
    groups.append({'state': 'unresolved', 'name': 'UNK', 'count': missing_count,
                   'tuples': [{'medicationRequestNdc9': 'UNK', 'medicationRequestNdc11': 'UNK',
                               **dict.fromkeys(CANMED, NO_CANMED), **MISSING}]})
    all_names = {r['fields']['canmedNonProprietaryName'].casefold() for r in canmed['records']}
    unsupported_canmed = {k: v for k, v in generic.items() if not k.startswith('Not found') and k.casefold() not in all_names}
    unsupported_rx = {k: v for k, v in rx_public.items() if k != 'Missing' and not k.startswith('Not sought') and k not in rx_by_name}
    return {'schema_version': 1, 'rows': count, 'groups': groups,
            'frequency_evidence': {
                'public_denominator': public['total_records'], 'category_counts': category_counts,
                'unresolved_rows': missing_count, 'rxnorm_matched_rows': rx_count,
                'canmed_unreported_count': public['total_records'] - sum(generic.values()),
                'canmed_unsupported_names': unsupported_canmed,
                'category_only_name_allocations': category_only,
                'category_only_name_policy': 'When no reported name supports a published category, retain category mass and allocate uniformly among distinct eligible lookup names. This is an explicit synthetic name allocation, not an inferred public name frequency.',
                'rxnorm_unreported_count': public['total_records'] - sum(rx_public.values()),
                'rxnorm_unsupported_names': unsupported_rx,
                'rxnorm_supported_name_count': sum(rx_weights.values()),
                'precedence': ['CanMED category including not-found', 'RxNorm display missingness',
                               'CanMED generic weights conditioned on supported names within category',
                               'RxNorm display weights conditioned on supported reported names'],
                'remaining_marginals': 'Derived from valid tuples; compare and disclose deviations, not independently sampled.',
                'allocation': 'Largest remainder per priority group; exact effective group counts; uniform package choice within selected group, seeded row shuffle.',
                'ndc_usage': 'No published marginal; package allocation is a synthetic assumption, not empirical usage.',
                'unresolved_evidence': 'Published RxNorm display Missing rate retained; NDC=UNK is an explicit synthetic recipe assumption because the report supplies no NDCs. This does not establish that the underlying source NDC was unknown.',
                'temporal_policy': 'Identity-only lookup. Package effective/discontinuation dates do not filter synthetic rows; quantify date comparisons separately. Current lookup/status proves no historical availability. Preserve the existing dispensing timeline; no clinical appropriateness claim.'}}


def enrich_rows(rows, plan, seed):
    if len(rows) != plan['rows']:
        raise ValueError('Baseline row count differs from plan')
    if any(r.get(k, '') for r in rows for k in DRUG_FIELDS):
        raise ValueError('Refuse replacement of already populated drug fields')
    rng = random.Random(seed)
    tuples = [rng.choice(group['tuples']) for group in plan['groups'] for _ in range(group['count'])]
    rng.shuffle(tuples)
    if len(tuples) != len(rows):
        raise ValueError('Plan row count inconsistent')
    return [{**row, **fields} for row, fields in zip(rows, tuples)]


def iter_enriched(rows, plan, seed):
    """Retain only tuple references, never a materialized table of claim rows."""
    rng=random.Random(seed)
    tuples=[rng.choice(group['tuples']) for group in plan['groups'] for _ in range(group['count'])]
    rng.shuffle(tuples)
    if len(tuples)!=plan['rows']:raise ValueError('Plan row count inconsistent')
    count=0
    for row in rows:
        if count>=len(tuples):raise ValueError('Too many PHARM rows')
        if any(row.get(k,'') for k in DRUG_FIELDS):raise ValueError('Refuse populated drug fields')
        yield {**row,**tuples[count]}
        count+=1
    if count!=len(tuples):raise ValueError('Too few PHARM rows')


def write_candidate(baseline, destination, plan, seed):
    with zipfile.ZipFile(baseline) as z:
        if set(z.namelist()) != {'PHARM.csv', 'pharm_metadata.json'}:
            raise ValueError('Unexpected PHARM ZIP members')
        reader = csv.DictReader(io.StringIO(z.read('PHARM.csv').decode()))
        header, rows = reader.fieldnames, list(reader)
        metadata = z.read('pharm_metadata.json')
    enriched = enrich_rows(rows, plan, seed)
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=header, lineterminator='\n')
    writer.writeheader()
    writer.writerows(enriched)
    with zipfile.ZipFile(destination, 'x') as z:
        for name, payload in [('PHARM.csv', stream.getvalue().encode()), ('pharm_metadata.json', metadata)]:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            z.writestr(info, payload)
