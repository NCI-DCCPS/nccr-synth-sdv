"""Conservative local resource measurements and benchmark-based full-run gates."""
from pathlib import Path
import re
import shutil
import subprocess
import sys
from .common import WORK,shared_tools,local_verifiers


def snapshot():
    if sys.platform=='darwin':
        raw=subprocess.check_output(['/usr/bin/vm_stat'],text=True)
        page=int(re.search(r'page size of (\d+) bytes',raw)[1])
        counts={k:int(re.search(r'^'+re.escape(k)+r':\s+(\d+)',raw,re.M)[1]) for k in ['Pages free','Pages inactive']}
        available=sum(counts.values())*page
        method='vm_stat (free + inactive) pages; excludes speculative/purgeable pages'
    else:
        raw=Path('/proc/meminfo').read_text();available=int(re.search(r'^MemAvailable:\s+(\d+)',raw,re.M)[1])*1024
        method='/proc/meminfo MemAvailable'
    return {'available_memory_bytes':available,'available_memory_method':method,'disk_free_bytes':shutil.disk_usage(WORK).free}


def check_budget(recipe):
    measured=snapshot();result={**measured,'memory_limit_bytes':measured['available_memory_bytes']//2,
                               'disk_margin_multiplier':2,'status':'passed','forecast':None}
    integrated=recipe.spec.get('schema_version')==4
    if recipe.spec['size']['kind']=='scale' or (integrated and recipe.spec['patients']>10000):
        if integrated:
            local_verifiers()
            from integrated_prerequisites import validate_prerequisites
        else:
            shared_tools()
            from candidate_evidence import validate_prerequisites
        evidence=validate_prerequisites(recipe.spec,recipe.common);by={r['patients']:r for r in evidence};n=recipe.spec['patients']
        predictions={}
        for stage in ['generation','audit','assessment_metrics']:
            small=by[1000][stage]['peak_rss_bytes'];large=by[10000][stage]['peak_rss_bytes']
            slope=max(0,(large-small)/9000)
            predictions[stage]=int(1.5*(large+slope*max(0,n-10000)))
        ratio=max(1,n/10000)
        # Includes payloads, context, repeated small input/code archives and reports.
        storage=int(2*by[10000]['generation']['disk_payload_bytes']*ratio+512*1024**2)
        result['forecast']={'peak_rss_bytes':predictions,'additional_disk_bytes':storage,'method':'1k/10k nonnegative memory slope with 1.5x headroom and 10k floor; disk 2x scaled payload with 10k floor + 512 MiB'}
        if max(predictions.values())>=result['memory_limit_bytes']:raise ValueError('Scale-run memory forecast exceeds half of currently available memory')
        if measured['disk_free_bytes']<=2*storage:raise ValueError('Insufficient free disk for scale-run margin')
    return result
