"""Strict all-source pilot inputs; separate from the retained core recipe API."""
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from .common import checked_pin,digest,pin,read_json,shared_tools,local_verifiers
from .recipe import Recipe

SOURCES=['CTC','ABM','COG','MCE','MCD','MCP','PHARM','RO','CCDI']


def scaled(value,patients,base):
    return int((Decimal(value)*patients/Decimal(base)).quantize(Decimal(1),rounding=ROUND_HALF_UP))


def load_full_recipe(path):
    path=Path(path).resolve();spec=read_json(path)
    keys={'schema_version','recipe_id','snapshot','rules','contract','profiles','sources','patients','seed','chunk_size'}
    integrated=spec.get('schema_version')==4
    scalable=spec.get('schema_version') in [3,4]
    if scalable:keys|={'size','prerequisites','template'}
    if set(spec)!=keys or type(spec['schema_version']) is not int or spec['schema_version'] not in [2,3,4]:
        raise ValueError('Unsupported all-source recipe schema')
    if spec['sources']!=SOURCES:raise ValueError('All nine sources are mandatory')
    for k,lo,hi in [('patients',100,None if integrated else (100000 if scalable else 1000)),('seed',0,2**63-1),('chunk_size',1,100000)]:
        if type(spec[k]) is not int or spec[k]<lo or (hi is not None and spec[k]>hi):raise ValueError('Invalid '+k)
    if not isinstance(spec['recipe_id'],str) or not spec['recipe_id']:raise ValueError('Missing recipe ID')
    inputs=[pin(path)]
    def get(item):
        p=checked_pin(item);inputs.append(item);return read_json(p),p
    manifest,mp=get(spec['snapshot']);snapshot=mp.parent;seen=set()
    if manifest.get('snapshot_schema_version')!=1:raise ValueError('Unsupported snapshot schema')
    for artifact in manifest['files']:
        p=(snapshot/artifact['path']).resolve()
        if not p.is_relative_to(snapshot) or p in seen or digest(p)!=artifact['sha256']:raise ValueError('Snapshot artifact mismatch')
        seen.add(p);inputs.append(pin(p))
    contract,_=get(spec['contract'])
    if (contract.get('contract_id')!=('jamboree-all-source-scaled' if scalable else 'jamboree-all-source-pilot') or contract['scope']!=SOURCES
            or contract.get('promotion') is not False or contract['maximum_patients']!=(None if integrated else (10000 if scalable else 1000))
            or contract['derived_sources']!=['CPI']):raise ValueError('Unsupported all-source contract')
    if integrated and contract.get('version')!='1.2.0':raise ValueError('Integrated recipe requires resource-gated contract')
    if scalable and not integrated and ((contract.get('version')=='1.1.0')!=('scale_range' in contract)
                     or contract.get('version') not in ['1.0.0','1.1.0']):
        raise ValueError('Unsupported scaled contract version/range')
    if set(contract['common_inputs'])!={'generation-contract.json','field-semantics.json','release-policy.json','context-schema.json'}:
        raise ValueError('Incomplete common inputs')
    common={k:get(v)[0] for k,v in contract['common_inputs'].items()}
    if common['generation-contract.json']['snapshot_manifest_sha256']!=spec['snapshot']['sha256']:raise ValueError('Snapshot contract mismatch')
    expected_profiles={'field-policies','model'}|({'catalog-linkage','pharm-enrichment'} if integrated else set())
    multiplicity=integrated and 'tumor-multiplicity' in spec['profiles']
    if multiplicity:expected_profiles.add('tumor-multiplicity')
    if set(spec['profiles'])!=expected_profiles:raise ValueError('Unknown profiles')
    profiles={k:get(v)[0] for k,v in spec['profiles'].items()}
    versions={'field-policies':'3.0.0' if integrated else '2.0.0',
              'model':'3.1.0' if multiplicity else ('3.0.0' if integrated else '2.0.0')}
    if any(profiles[k].get('schema_version')!=1 or profiles[k].get('version')!=versions[k] for k in versions):raise ValueError('Unsupported profile version')
    # Policies are executable assumptions, not free-text instructions to interpret.
    from .full_preflight import MODEL
    expected_model={**MODEL,'version':'3.0.0','pharm_identity':'pinned_public_tuple_lookup','ccdi_resources':'pinned_shared_catalog'} if integrated else MODEL
    if multiplicity:
        expected_model.update(version='3.1.0',tumor_allocation='pinned_patient_histogram_exact_totals',
                              diagnosis_year='public_support_conditioned_on_multiplicity_and_2022_cutoff',
                              later_tumor_months='uniform_distinct_feasible_months_sorted')
        del expected_model['second_tumor_gap']
    if profiles['model']!=expected_model:raise ValueError('Unsupported model policy')
    _,rp=get(spec['rules']);shared_tools()
    if integrated:from .integrated_inputs import load_rules
    else:from release_rules import load_rules
    rules_spec,rules=load_rules(rp)
    if rules_spec['version'] not in (['2.3.0'] if integrated else ['1.2.0','2.1.0']):raise ValueError('Unsupported typed-context rule version')
    if integrated:
        from .integrated_inputs import load_inputs
        inputs.extend(load_inputs(spec)['inputs'])
    for rule in rules:
        for evidence in rule.get('evidence',[]):
            if 'path' in evidence and 'sha256' in evidence:
                item={k:evidence[k] for k in ['path','sha256']}
                # Some evidence is snapshot-relative; retain exact snapshot artifact.
                p=snapshot/item['path']
                if p.is_file():
                    if digest(p)!=item['sha256']:raise ValueError('Evidence mismatch')
                    inputs.append(pin(p))
                else:checked_pin(item);inputs.append(item)
    base=common['generation-contract.json']['population']['base_patients'];targets={}
    for s in SOURCES:
        counts=common['generation-contract.json']['sources'][s]['counts']
        n=spec['patients'] if s in ['CTC','ABM','COG','MCE'] else max(1,scaled(counts['public_patients'],spec['patients'],base))
        raw=scaled(counts['public_rows'],spec['patients'],base);rows=max(1,n,raw)
        targets[s]={'rows':rows,'patients':n,'row_adjustment':rows-raw,
                    'patient_adjustment':n-scaled(counts['public_patients'],spec['patients'],base)}
        if 'confirmed_enrollment' in counts:targets[s]['confirmed']=scaled(counts['confirmed_enrollment']['public'],spec['patients'],base)
    if targets['CTC']['rows']!=targets['ABM']['rows'] or (not multiplicity and targets['CTC']['rows']>2*spec['patients']):raise ValueError('Unsupported tumor multiplicity')
    if scalable:
        from .sizing import scaled_targets
        size,patients,targets=scaled_targets(common['generation-contract.json'],spec['size'],contract)
        if size!=spec['size'] or patients!=spec['patients']:raise ValueError('Size request and resolved patient count disagree')
        if spec['template'] is not None:
            template,_=get(spec['template'])
            mutable={'recipe_id','size','patients','prerequisites','template'}
            if {k:v for k,v in template.items() if k not in mutable}!={k:v for k,v in spec.items() if k not in mutable}:raise ValueError('Effective recipe alters template scientific inputs')
        if integrated:
            local_verifiers()
            from integrated_prerequisites import validate_prerequisites
        else:from candidate_evidence import validate_prerequisites
        evidence=validate_prerequisites(spec,common)
        for record in evidence:
            for item in record['inputs']:checked_pin(item);inputs.append(item)
    unique={}
    for item in inputs:
        if item['path'] in unique and unique[item['path']]!=item:raise ValueError('Conflicting pins')
        unique[item['path']]=item
    return Recipe(path,spec,snapshot,contract,common,profiles,rules_spec,rules,list(unique.values()),targets)
