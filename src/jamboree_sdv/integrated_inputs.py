"""Strict declarative input loading shared by generation and output checks."""
from .common import WORKSPACE, checked_pin, read_json, pin, digest
from .catalog_contract import load_catalog_contract, SUPPORTED_RULES_SHA256
from pathlib import Path

PHARM_PROFILE_SHA='739a05f9c3d33a7d9eeff3f7a67d42b7f20308b453ee107b38589d75dabccf75'


def load_rules(path):
    if digest(path)!=SUPPORTED_RULES_SHA256:raise ValueError('Unsupported integrated rule bundle')
    spec=read_json(path)
    return spec,spec['rules']+spec.get('biological_constraints',[])


def load_inputs(spec, resolver=None):
    def resolve(item):
        path=Path(resolver(item)) if resolver is not None else checked_pin(item)
        if digest(path)!=item['sha256']:raise ValueError('Input hash mismatch')
        return path
    rules,_=load_rules(resolve(spec['rules']))
    catalog=load_catalog_contract(spec['rules'],spec['profiles']['catalog-linkage'],resolver=resolve)
    profile_pin=spec['profiles']['pharm-enrichment']
    profile=read_json(resolve(profile_pin))
    if profile_pin['sha256']!=PHARM_PROFILE_SHA:raise ValueError('Unsupported PHARM profile')
    lookup=rules['lookups']['canmed_ndc_2026_11_12']
    frequency=rules['frequency_references']['pharm_public_2026_11_09']
    pins={'canmed_manifest':lookup['manifest'],
          'canmed':{'path':lookup['normalized_path'],'sha256':lookup['normalized_sha256']},
          'rxnorm_manifest':profile['rxnorm_manifest'],'rxnorm':profile['rxnorm'],
          'public':frequency['normalized_json'],'public_report':frequency['published_json']}
    inputs=list(catalog['inputs'].values())+[profile_pin]+list(pins.values())
    loaded={key:read_json(resolve(item)) for key,item in pins.items()}
    manifests=[pins[k] for k in ['canmed_manifest','rxnorm_manifest']]+[catalog['inputs']['catalog_manifest']]
    for manifest_pin in manifests:
        root=resolve(manifest_pin).parent
        for item in read_json(root/Path(manifest_pin['path']).name)['files']:
            path=(root/item['path']).resolve()
            if not path.is_relative_to(root) or digest(path)!=item['sha256']:
                raise ValueError('Lookup bundle hash mismatch: '+item['path'])
            inputs.append({'path':str(Path(manifest_pin['path']).parent/item['path']),'sha256':item['sha256']})
    if loaded['rxnorm_manifest']['normalized']['sha256']!=pins['rxnorm']['sha256']:
        raise ValueError('RxNorm normalized pin mismatch')
    extra={}
    if 'tumor-multiplicity' in spec['profiles']:
        from .multiplicity_reference import load_reference
        extra['multiplicity']=load_reference(spec['profiles']['tumor-multiplicity'],resolver=resolve)
        inputs.extend(extra['multiplicity']['inputs'])
    return {'catalog':catalog,'pharm':loaded,'pharm_profile':profile,**extra,
            'inputs':list({p['path']:p for p in inputs}.values())}
