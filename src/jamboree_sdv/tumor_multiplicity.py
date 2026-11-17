"""Exact-total apportionment of a frozen patient-level multiplicity histogram."""


def allocate_histogram(histogram, patients, tumor_rows):
    """Round every patient quota down/up, preserving both totals exactly.

    Dynamic programming chooses the least absolute rounding error using integer
    arithmetic. Ties use ascending support order. State size depends on the
    small support, not the requested population. Infeasible quotas fail closed.
    """
    if (type(patients) is not int or type(tumor_rows) is not int or patients < 1
            or tumor_rows < patients or not histogram
            or any(type(k) is not int or k < 1 or type(v) is not int or v <= 0
                   for k, v in histogram.items())):
        raise ValueError('Invalid multiplicity counts')
    support = sorted(histogram)
    base = sum(histogram.values())
    floors, remainders = {}, {}
    for k in support:
        floors[k], remainders[k] = divmod(histogram[k]*patients, base)
    wanted = (patients - sum(floors.values()), tumor_rows - sum(k*v for k, v in floors.items()))
    if wanted[0] < 0 or wanted[1] < 0:
        raise ValueError('Infeasible multiplicity quotas for requested totals')
    states = {(0, 0): (0, ())}
    for k in support:
        if not remainders[k]:
            continue
        next_states = dict(states)
        for (n, rows), (cost, chosen) in states.items():
            target = (n + 1, rows + k)
            if target[0] > wanted[0] or target[1] > wanted[1]:
                continue
            candidate = (cost + base - 2*remainders[k], chosen + (k,))
            if target not in next_states or candidate < next_states[target]:
                next_states[target] = candidate
        states = next_states
    if wanted not in states:
        raise ValueError('Infeasible multiplicity quotas for requested totals')
    chosen = set(states[wanted][1])
    return {k: floors[k] + int(k in chosen) for k in support}


def allocation_plan(reference, patients, rows):
    counts = allocate_histogram(reference['histogram'], patients, rows)
    return {'status': 'allocated', 'patients': patients, 'rows': rows,
            'reference_patients': reference['patients'], 'reference_rows': reference['rows'],
            'patient_counts': {str(k): v for k, v in counts.items()},
            'reference_counts': {str(k): v for k, v in sorted(reference['histogram'].items())},
            'rounding': 'each_patient_bucket_floor_or_ceiling_with_exact_totals'}
