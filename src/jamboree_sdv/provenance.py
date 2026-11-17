"""Retain exact common inputs, translated settings, code and environment."""
from pathlib import Path
import shutil
from .common import WORKSPACE,digest,write_json


def archive_pins(pins,base):
    records=[]
    for item in pins:
        source=WORKSPACE/item['path']
        if digest(source)!=item['sha256']:raise ValueError('Input changed while archiving: '+str(source))
        destination=Path(base)/item['path']
        if not destination.resolve().is_relative_to(Path(base).resolve()):raise ValueError('Archive path escape')
        destination.parent.mkdir(parents=True,exist_ok=True)
        with source.open('rb') as src,destination.open('xb') as dst:shutil.copyfileobj(src,dst)
        if digest(destination)!=item['sha256']:raise ValueError('Archive hash mismatch')
        records.append({**item,'archived_path':str(destination.resolve())})
    return records


def retain_preflight(preflight,run):
    common=[x for x in preflight['inputs'] if x['path'].startswith('reference/')]
    generator=[x for x in preflight['inputs'] if not x['path'].startswith('reference/')]
    inputs=archive_pins(common,run/'inputs/common')+archive_pins(generator,run/'inputs/generator')
    code=archive_pins(preflight['code'],run/'code')
    write_json(run/'inputs/adapted/preflight.json',preflight)
    return {'inputs':inputs,'code':code,'adapted_sha256':digest(run/'inputs/adapted/preflight.json')}

