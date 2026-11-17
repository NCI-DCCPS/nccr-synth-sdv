"""Prerequisites for the integrated pipeline, including implementation identity."""
from pathlib import Path
from candidate_evidence import verify_prerequisite as verify_legacy
from assess_core_pilot import read, pinned, sha
from jamboree_sdv.common import full_code_pins
from jamboree_sdv.promotion_evidence import verify_raw_files


def required_sizes(spec):
    if spec['size']['kind']=='scale' or spec['patients']>10000:return [100,1000,10000]
    return [1000] if spec['patients']>1000 else []


def verify_prerequisite(item,expected,common,active=None):
    active=set() if active is None else active
    identity=(item['path'],item['sha256'])
    if identity in active:raise ValueError('Cyclic prerequisite graph')
    active.add(identity)
    proof=verify_legacy(item,expected,common)
    path=pinned(item);report=read(path);audit=read(path.parent.parent/'audit/audit.json')
    manifest=read(audit['manifest']['path']);run=read(manifest['run_manifest']['path'])
    if run['recipe']['schema_version']!=4:raise ValueError('Prerequisite is not integrated')
    actual={p['path']:p['sha256'] for p in run['archives']['code']}
    current={p['path']:p['sha256'] for p in full_code_pins(True)}
    if actual!=current:raise ValueError('Prerequisite implementation differs from current pipeline')
    if report.get('resource_checks',{}).get('status')!='passed':raise ValueError('Prerequisite resource checks missing')
    if verify_raw_files(manifest['raw_files'],manifest['files'])!=run['raw_layout_checks']:
        raise ValueError('Prerequisite raw payload mismatch')
    for child in run['recipe']['prerequisites']:verify_prerequisite(child,expected,common,active)
    active.remove(identity)
    return proof


def validate_prerequisites(spec,common):
    if not isinstance(spec['prerequisites'],list):raise ValueError('Prerequisites must be pins')
    records=[verify_prerequisite(p,spec,common) for p in spec['prerequisites']]
    counts=[r['patients'] for r in records]
    if len(counts)!=len(set(counts)):raise ValueError('Duplicate prerequisite size')
    missing=set(required_sizes(spec))-set(counts)
    if missing:raise ValueError('Missing passing prerequisite assessment(s): '+str(sorted(missing)))
    return records
