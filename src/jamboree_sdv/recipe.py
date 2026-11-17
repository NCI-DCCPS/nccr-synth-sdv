"""Recipe validation binds common definitions and generator choices by hash."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from .common import checked_pin, digest, pin, read_json, shared_tools


@dataclass
class Recipe:
    path: Path
    spec: dict
    snapshot: Path
    contract: dict
    common: dict
    profiles: dict
    rules_spec: dict
    rules: list
    inputs: list
    targets: dict


def load_recipe(path):
    path = Path(path).resolve()
    spec = read_json(path)
    required = {'schema_version','recipe_id','snapshot','rules','contract','profiles','sources','patients','seed','chunk_size'}
    if set(spec) != required or type(spec['schema_version']) is not int or spec['schema_version'] != 1:
        raise ValueError('Unsupported recipe keys/schema')
    if spec['sources'] != ['CTC','ABM','COG']:
        raise ValueError('Only the CTC/ABM/COG core scope is supported; MCE requires context bindings')
    for key, low, high in [('patients',1,1000),('seed',0,2**63-1),('chunk_size',1,100000)]:
        if type(spec[key]) is not int or not low <= spec[key] <= high:
            raise ValueError(f'Invalid {key}')
    if not isinstance(spec['recipe_id'],str) or not spec['recipe_id']:
        raise ValueError('Nonempty recipe_id required')
    inputs = [pin(path)]
    def get(item):
        p=checked_pin(item); inputs.append(item); return read_json(p),p
    manifest, manifest_path = get(spec['snapshot'])
    if manifest.get('snapshot_schema_version') != 1:
        raise ValueError('Unsupported snapshot schema')
    snapshot=manifest_path.parent
    seen=set()
    for artifact in manifest['files']:
        p=(snapshot/artifact['path']).resolve()
        if not p.is_relative_to(snapshot) or p in seen or digest(p)!=artifact['sha256']:
            raise ValueError(f'Snapshot artifact mismatch or duplicate: {artifact["path"]}')
        seen.add(p)
        if artifact['path'].startswith(('metadata/','normalized/','reports/')):
            # These are public aggregates/dictionaries, not patient records.
            if p.name.startswith(('ctc','abm','cog','nccr_instances')):
                inputs.append(pin(p))
    contract,_=get(spec['contract'])
    if (contract.get('contract_id')!='jamboree-core-pilot' or contract['scope']!=spec['sources']
            or contract.get('promotion') is not False or spec['patients']>contract['maximum_patients']):
        raise ValueError('Unsupported pilot contract')
    common={name:get(item)[0] for name,item in contract['common_inputs'].items()}
    if common['generation-contract.json']['snapshot_manifest_sha256']!=spec['snapshot']['sha256']:
        raise ValueError('Common contract uses a different snapshot')
    if set(spec['profiles'])!={'field-policies','dependence','core-counts'}:
        raise ValueError('Exactly three known profiles required')
    profiles={name:get(item)[0] for name,item in spec['profiles'].items()}
    for profile in profiles.values():
        if profile.get('schema_version')!=1 or profile.get('version')!='1.0.0':
            raise ValueError('Unsupported profile version')
    dep=profiles['dependence']
    if set(dep)!={'schema_version','version','mode','blocks','reason'} or dep['mode']!='independent' or dep['blocks']!=[]:
        raise ValueError('Domain dependence blocks require a future reviewed adapter; current profile uses independence')
    counts=profiles['core-counts']
    expected={'rounding':'ROUND_HALF_UP','tumor_allocation':'one_per_patient_plus_uniform_without_replacement',
              'maximum_tumors_per_patient':2,'id_prefix':'SDV','id_digits':10,
              'iccc_policy':'reported_extended_support_derive_parents',
              'missing_iccc_leaf_frequency':'exclude_from_sampling_report_unknown',
              'enrollment_count_policy':'independent_bernoulli_from_public_rate','field_order':'frozen_dictionary'}
    if set(counts)!=set(expected)|{'schema_version','version'} or any(counts[k]!=v or type(counts[k]) is not type(v) for k,v in expected.items()):
        raise ValueError('Unsupported counts profile policy')
    _,rules_path=get(spec['rules'])
    shared_tools()
    from release_rules import load_rules
    rules_spec,rules=load_rules(rules_path)
    if rules_spec['version'] not in ['1.1.0','2.0.0']:
        raise ValueError('Only rules 1.1.0 or 2.0.0 supported')
    for rule in rules:
        if rule.get('enabled') and rule['type']=='hierarchical_lookup':
            for evidence in rule.get('evidence',[]):
                if 'path' in evidence:
                    item={k:evidence[k] for k in ['path','sha256']}; checked_pin(item);inputs.append(item)
    population=common['generation-contract.json']['population']
    targets={}
    for source in spec['sources']:
        base=common['generation-contract.json']['sources'][source]['counts']
        rows=int((Decimal(base['public_rows'])*spec['patients']/Decimal(population['base_patients'])).quantize(Decimal(1),rounding=ROUND_HALF_UP))
        targets[source]=max(rows,spec['patients'])
    if targets['CTC']!=targets['ABM'] or targets['CTC']>2*spec['patients'] or targets['COG']!=spec['patients']:
        raise ValueError('Unsupported source targets')
    unique={}
    for item in inputs:
        if item['path'] in unique and unique[item['path']]!=item:
            raise ValueError('Conflicting input pins')
        unique[item['path']]=item
    return Recipe(path,spec,snapshot,contract,common,profiles,rules_spec,rules,list(unique.values()),targets)

