"""Local output boundary around the independent common pilot assessor."""
from pathlib import Path
from .common import WORKSPACE,now,output_path,pin,shared_tools,write_json
from .provenance import archive_pins
from .recipe import load_recipe


def assess_candidate(recipe_path,run_path,manifest_path,audit_path,out):
    destination=output_path(out)
    load_recipe(recipe_path)
    shared_tools()
    from assess_core_pilot import assess
    result=assess(manifest_path,run_path,audit_path,recipe_path)
    destination.mkdir(parents=True,exist_ok=False)
    verifier=archive_pins([pin(WORKSPACE/'tools/assess_core_pilot.py'),pin(WORKSPACE/'tools/hierarchy_rules.py')],destination/'verifier')
    result.update(created_at=now(),verifier=verifier)
    write_json(destination/'assessment.json',result)
    summary=['# gen-sdv core pilot assessment','',f"Status: **{result['status']}**",'',
             'Scope: CTC, ABM, COG. Full release readiness: **false**.','',
             f"Raw shared audit: `{result['raw_audit_status']}`. Missing MCE remains visible.",'',
             '| Source | Rows | Patients |','|---|---:|---:|']
    for source,c in result['observed_counts'].items():summary.append(f"| {source} | {c['rows']} | {c['distinct_patients']} |")
    summary+=['','Findings:']+(['- '+v for v in result['findings']] or ['- None in the declared core scope.'])
    summary+=['','Limitations:']+['- '+v for v in result['limitations']]
    (destination/'summary.md').write_text('\n'.join(summary)+'\n')
    return result

