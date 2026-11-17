"""Recount patient multiplicity from actual CTC/ABM rows; no allocator imports."""
from collections import Counter


def check(ctc_rows, abm_rows, reference, plan, patients, tumor_rows):
    def require(ok, message):
        if not ok:
            raise ValueError('Tumor multiplicity: '+message)

    populations = []
    for name, rows in [('CTC', ctc_rows), ('ABM', abm_rows)]:
        counts, masks, declared = Counter(), {}, {}
        for row in rows:
            identity = row['nccrId']
            number = int(row['tumorRecordNumber'])
            require(bool(identity) and 1 <= number <= 99, 'invalid patient/tumor key')
            bit = 1 << (number-1)
            require(not masks.get(identity, 0) & bit, 'duplicate tumor key')
            masks[identity] = masks.get(identity, 0) | bit
            counts[identity] += 1
            if name == 'CTC':
                value = int(row['numberOfPrimaries'])
                require(identity not in declared or declared[identity] == value, 'inconsistent numberOfPrimaries')
                declared[identity] = value
        require(len(counts) == patients and sum(counts.values()) == tumor_rows, 'patient/row totals disagree')
        require(all(masks[p] == (1 << count)-1 for p, count in counts.items()), 'nonconsecutive tumor numbers')
        if name == 'CTC':
            require(declared == dict(counts), 'numberOfPrimaries disagrees with actual rows')
        populations.append(counts)
    require(populations[0] == populations[1], 'CTC/ABM patient multiplicity differs')
    observed = Counter(populations[0].values())
    source = reference['histogram']
    require(set(observed) <= set(source), 'unsupported multiplicity')
    require(sum(source.values()) == reference['patients'], 'reference patient denominator mismatch')
    for k, count in source.items():
        floor, remainder = divmod(count*patients, reference['patients'])
        require(floor <= observed[k] <= floor+bool(remainder), 'patient histogram outside rounding quota')
    actual = {str(k): observed[k] for k in sorted(source)}
    require(plan['patient_counts'] == actual, 'output differs from archived allocation')
    require(plan['patients'] == patients and plan['rows'] == tumor_rows, 'plan totals disagree')
    require(plan['reference_counts'] == {str(k): v for k, v in sorted(source.items())}
            and plan['reference_patients'] == reference['patients'] and plan['reference_rows'] == reference['rows'],
            'plan reference differs from frozen evidence')
    return {'status': 'passed', 'patients': patients, 'tumor_rows': tumor_rows,
            'patient_histogram': actual, 'maximum_observed': max(observed),
            'reference_maximum': max(source), 'rounding_quota_check': 'passed'}
