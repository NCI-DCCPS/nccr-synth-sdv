"""Independent executable bridge for the inherited MCE prose requirements.

Uses no sampler or typed-predicate evaluator. Death code 8 has priority over 6.
The full typed-context check remains separately required.
"""
from collections import Counter
import csv
import io
import re
import zipfile


def month(text):
    year, value = map(int, text.split('-'))
    if not 1 <= value <= 12:
        raise ValueError('Invalid context month')
    return year * 12 + value - 1


def expected_special(patient, offset):
    deceased = patient['deceased'] in (True, 'true')
    coverage = month(patient['coverage_end_month']) - month(patient['synthetic_index_month'])
    death = int(patient['death_offset_months']) if deceased else None
    if offset > 0 and deceased and death < coverage and offset > death:
        return '8'
    if offset < 0 and (int(patient['index_year']) == 2000 or
                       int(patient['index_age_completed_years']) < 1):
        return '6'
    if 1 <= offset <= 12 and int(patient['index_year']) == 1999:
        return '6'
    return None


def check(path, patient_path):
    with patient_path.open(newline='') as stream:
        patients = {}
        for row in csv.DictReader(stream):
            if row['nccrId'] in patients:
                raise ValueError('Duplicate context patient')
            patients[row['nccrId']] = row
    counters = {key: Counter() for key in ['prior_year_or_infant', 'post_year_1999', 'post_death']}
    seen = set()
    with zipfile.ZipFile(path) as archive, archive.open('MCE.csv') as raw, io.TextIOWrapper(raw, newline='') as stream:
        reader = csv.DictReader(stream)
        slots = []
        for field in reader.fieldnames:
            match = re.fullmatch(r'enrollMonth(Prior|Post)IndexDx([1-9][0-9]*)', field)
            if match:
                offset = int(match[2]) * (-1 if match[1] == 'Prior' else 1)
                slots.append((field, offset))
        if not {-i for i in range(1, 13)} <= {o for _, o in slots} or not set(range(1, 13)) <= {o for _, o in slots}:
            raise ValueError('Missing inherited MCE fields')
        for row in reader:
            identity = row['nccrId']
            if identity in seen or identity not in patients:
                raise ValueError('MCE patient identity mismatch')
            seen.add(identity)
            patient = patients[identity]
            for field, offset in slots:
                actual = row[field]
                special = expected_special(patient, offset)
                if -12 <= offset < 0:
                    c = counters['prior_year_or_infant']
                    c['cells_evaluated'] += 1
                    c['eligible_cells'] += special == '6'
                    c['violating_cells'] += (actual == '6') != (special == '6')
                if offset > 0:
                    c = counters['post_death']
                    c['cells_evaluated'] += 1
                    c['eligible_cells'] += special == '8'
                    c['violating_cells'] += (actual == '8') != (special == '8')
                if 1 <= offset <= 12:
                    c = counters['post_year_1999']
                    c['cells_evaluated'] += 1
                    c['death_precedence_cells'] += special == '8'
                    c['eligible_cells'] += special == '6'
                    c['violating_cells'] += (actual == '6') != (special == '6')
    if seen != set(patients):
        raise ValueError('Incomplete MCE patient coverage')
    clauses = [{'clause': key, 'status': 'passed' if c['cells_evaluated'] and not c['violating_cells'] else 'findings',
                **dict(c)} for key, c in counters.items()]
    return {'status': 'passed' if all(c['status'] == 'passed' for c in clauses) else 'findings',
            'rows_evaluated': len(seen), 'precedence': ['8', '6', 'availability'], 'clauses': clauses}
