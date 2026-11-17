"""Regenerate linkage resource names using a pinned mock-domain catalog.

The public distribution is explicitly conditioned on catalog coverage. Patient
and participant identifiers are retained; this is not an unconditional replay.
"""
import math

from .copula import LatentCopula, categorical
from .promotion_evidence import require
from .random_streams import seed_for
from .catalog_contract import load_catalog_contract
from .common import WORKSPACE

STREAM = 'CCDI.resourceName.catalog_v1'


def conditioned_distribution(distribution, catalog):
    require(isinstance(catalog, list) and bool(catalog), 'Empty or invalid catalog')
    names=[]
    for item in catalog:
        require(isinstance(item, dict) and all(isinstance(item.get(k), str) for k in
                ['domain_name','domain_description','domain_category','data_location'])
                and bool(item['domain_name']) and bool(item['domain_description']), 'Invalid catalog entry')
        names.append(item['domain_name'])
    require(len(names)==len(set(names)), 'Duplicate catalog domain')
    categories=distribution['categories'];weights=distribution['probabilities']
    require(bool(categories) and len(categories)==len(set(categories)) and
            len(categories)==len(weights) and all(isinstance(c,str) and c for c in categories) and
            all(type(w) in (int,float) and math.isfinite(w) and w>=0 for w in weights) and
            math.isclose(sum(weights),1,abs_tol=1e-8), 'Invalid source distribution')
    allowed=set(names)
    keep=[i for i,c in enumerate(categories) if c in allowed and weights[i]>0]
    mass=sum(weights[i] for i in keep)
    require(bool(keep) and mass>0, 'No supported positive-mass catalog domains')
    return {'constraint':'resourceName must exactly match a pinned catalog domain_name',
            'distribution':'public probabilities conditioned on catalog membership',
            'categories':[categories[i] for i in keep],
            'probabilities':[weights[i]/mass for i in keep],
            'retained_probability_mass':mass,
            'excluded_categories':[c for i,c in enumerate(categories) if i not in keep],
            'catalog_domains_without_positive_public_mass':sorted(allowed-{categories[i] for i in keep})}


def regenerate_mappings(rows, distribution, catalog, seed):
    policy=conditioned_distribution(distribution,catalog)
    require(bool(rows) and all(len(r)==3 and all(isinstance(v,str) and v for v in r) for r in rows),
            'Invalid original mapping rows')
    require(len({r[1].lower() for r in rows})==len(rows),
            'Regeneration requires globally unique synthetic participant identifiers')
    policy.update(master_seed=seed,stream=STREAM,stream_seed=seed_for(seed,STREAM),
                  engine='Copulas supplied Gaussian parameters; no fit',
                  patient_and_participant_ids='preserved in original row order')
    uniforms=LatentCopula([STREAM],[[1]],policy['stream_seed']).uniform(len(rows))[:,0]
    names=categorical(uniforms,policy['categories'],policy['probabilities'])
    return [(row[0],row[1],str(name)) for row,name in zip(rows,names)],policy


def regenerate_from_rules(rows, rules_pin, profile_pin, seed, *, workspace=WORKSPACE):
    """Apply the shared catalog rules to mappings; raw/CPI output still needs checking.

    This scoped adapter does not qualify the other rules in the selected bundle.
    The caller must serialize raw CCDI and the exact same triples as CPI, then run
    the independent catalog checker against CTC and both delivered payloads.
    """
    contract = load_catalog_contract(rules_pin, profile_pin, workspace=workspace)
    counts = contract['counts']
    reported = sum(counts.values())
    distribution = {'categories': list(counts), 'probabilities': [n/reported for n in counts.values()]}
    rows, policy = regenerate_mappings(rows, distribution, contract['catalog'], seed)
    supported = set(policy['categories'])
    policy.update(rules=rules_pin, profile=profile_pin, inputs=contract['inputs'],
                  published_counts=counts, published_total_records=contract['total_records'],
                  reported_count=reported, unreported_count=contract['total_records']-reported,
                  excluded_counts={name: n for name, n in counts.items() if name not in supported},
                  retained_count=sum(counts[name] for name in supported),
                  retained_known_mass=policy['retained_probability_mass'],
                  evaluated_rules=contract['evaluated_rules'],
                  not_evaluated_rules=contract['not_evaluated_rules'],
                  enforcement='catalog sampling; projection, parent membership and output frequency require verifier',
                  full_rule_set_qualified=False)
    return rows, policy
