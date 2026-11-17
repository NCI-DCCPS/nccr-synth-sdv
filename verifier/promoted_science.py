"""Scientific checks derived from the retained scaled assessor, without sampler imports.

The promotion verifier supplies verified shared copies and provenance. Exact
row/field/rule/distribution checks below preserve the candidate assessor logic.
"""
from collections import Counter,defaultdict
from decimal import Decimal
import csv
import io
import math
from pathlib import Path
import zipfile
from assess_core_pilot import read,sha,independent_target
from assess_scaled_candidate import resolve_requested_size,rounded_public_count,expected_source_counts,numeric_label
from release_rules import load_rules,evaluate_rules
from context_rules import month_number

SOURCES=['CTC','ABM','COG','MCE','MCD','MCP','PHARM','RO','CCDI']


def assess_outputs(manifest,run,audit,provenance,reader):
    recipe=run['recipe'];findings=[]
    def require(ok,message):
        if not ok and message not in findings:findings.append(message)
    def pinned(item):
        path=Path(item['path']);require(sha(path)==item['sha256'],'Operational input hash mismatch');return path
    def historical(item):return reader.path(item['path'],item['sha256'])
    contract=read(historical(recipe['contract']))
    common={k:read(historical(v)) for k,v in contract['common_inputs'].items()}
    rules_spec,rules=load_rules(historical(recipe['rules']))
    snapshot=historical(recipe['snapshot']).parent
    policies=read(historical(recipe['profiles']['field-policies']))['fields']
    model=read(historical(recipe['profiles']['model']))
    request=recipe['size'];population=common['generation-contract.json']['population'];n=recipe['patients']
    full=request['kind']=='scale'
    factor,resolved=resolve_requested_size(request,population,contract)
    rounded=lambda x:rounded_public_count(x,factor)
    require(type(n) is int and n==resolved,'Size/resolved patient count mismatch')
    context_manifest=Path(manifest['context']['path']);require(sha(context_manifest)==manifest['context']['sha256'],'Context manifest hash mismatch')
    sidecars=read(context_manifest);patient_path=context_manifest.parent/sidecars['patient']['path']
    require(sha(patient_path)==sidecars['patient']['sha256'],'Patient context hash mismatch')
    bounds={}
    with patient_path.open(newline='') as stream:
        for p in csv.DictReader(stream):
            start=month_number(p['synthetic_index_month']);lo=month_number(p['synthetic_birth_month'])-start;hi=month_number(p['coverage_end_month'])-start
            if p['deceased']=='true':hi=min(hi,int(p['death_offset_months']))
            require(p['nccrId'] not in bounds,'Duplicate patient context');bounds[p['nccrId']]=(lo,hi)
    require(len(bounds)==n,'Patient context cardinality mismatch')
    policies_by_source={s:[p for p in policies if p['source']==s] for s in SOURCES}
    semantics={s['id']:s for s in common['field-semantics.json']['fields']}
    raw_counts={};observed={};counts={};ids={};headers={};pairs={};projections={};patient_values={};found=set();radiation=[]
    event_fields={'MCD':['monthsFromIndexDxToDiagnosisCode'],'MCP':['monthsFromIndexDxToProcedureCode'],
                  'PHARM':['monthsFromIndexDxtoDispense'],'RO':['months_from_dx_to_tx_start','months_from_dx_to_treatment_end']}
    for item in manifest['files']:
        source=item['source'];path=Path(item['local_path']);require(sha(path)==item['sha256'],'Payload hash mismatch: '+source)
        original='CCDI' if source=='CPI' else source
        dictionary=snapshot/'metadata'/f'{original.lower()}_metadata.json';expected=[e['Column_Name_at_Source'] for e in read(dictionary)['Dictionary_Elements']]
        selected=policies_by_source.get(source,[]);patient_fields=[p['field'] for p in selected if p['grain']=='patient'] if source=='CTC' else []
        require(source=='CPI' or [p['field'] for p in selected]==expected,'Policy field inventory mismatch: '+source)
        counters={f:Counter() for f in expected};obs={p['field']:Counter() for p in selected if p['mode'] in ['sample','conditional_count','hierarchy_parent_or_sample','numeric_bin']}
        ids[source]=set();row_count=0;pair_counts=Counter();projection=Counter()
        with zipfile.ZipFile(path) as archive:
            require(set(archive.namelist())=={source+'.csv',source.lower()+'_metadata.json'},'Unexpected ZIP members: '+source)
            require(archive.read(source.lower()+'_metadata.json')==dictionary.read_bytes(),'Altered bundled metadata: '+source)
            with archive.open(source+'.csv') as binary,io.TextIOWrapper(binary,encoding='utf-8',newline='') as stream:
                reader=csv.DictReader(stream);headers[source]=reader.fieldnames;require(reader.fieldnames==expected,'Schema/order mismatch: '+source)
                for row in reader:
                    row_count+=1
                    if None in row or any(v is None for v in row.values()):raise ValueError('Malformed row: '+source)
                    identity=row['nccrId'];first=identity not in ids[source];ids[source].add(identity)
                    for field,value in row.items():counters[field][value]+=1
                    if patient_fields:
                        values=tuple(row[f] for f in patient_fields)
                        if first:patient_values[identity]=values
                        else:require(patient_values[identity]==values,'Patient-grain values vary: CTC')
                    for p in selected:
                        field=p['field']
                        if field not in obs or (p['grain']=='patient' and not first):continue
                        if p['mode']=='conditional_count' and row['reportedCOGEnrollment']!='1':continue
                        obs[field][row[field]]+=1
                    if source in ['CTC','ABM']:pair_counts[(identity,row['tumorRecordNumber'])]+=1
                    if source in ['CCDI','CPI']:projection[(identity,row['resourceParticipantId'],row['resourceName'])]+=1
                    if source=='MCE' and row['reportedInsuranceEnrollment']=='1':found.add(identity)
                    if source in event_fields:
                        require(identity in bounds,'Event patient missing context: '+source)
                        if identity in bounds:
                            lo,hi=bounds[identity]
                            for field in event_fields[source]:
                                if row[field]!='9999':require(lo<=int(row[field])<=hi,'Event outside birth/coverage/death window: '+source)
                        if source=='RO':
                            radiation.append(row);a,b=(row[f] for f in event_fields[source])
                            if a!='9999' and b!='9999':require(int(a)<=int(b),'Radiation start/end inversion')
        require('' not in ids[source],'Blank patient ID: '+source)
        raw_counts[source]=counters;observed[source]=obs;counts[source]={'rows':row_count,'distinct_patients':len(ids[source])}
        if source in ['CTC','ABM']:pairs[source]=pair_counts
        if source in ['CCDI','CPI']:projections[source]=projection
        c=common['generation-contract.json']['sources'][original]['counts']
        target,patients=expected_source_counts(common['generation-contract.json'],source,n,factor,full)
        require(counts[source]=={'rows':target,'distinct_patients':patients},'Count mismatch: '+source)
        require(item['rows']==row_count and item['patients']==len(ids[source]),'Claimed count mismatch: '+source)
        if 'confirmed_enrollment' in c:
            field='reportedCOGEnrollment' if source=='COG' else 'reportedInsuranceEnrollment'
            require(counters[field]['1']==rounded(c['confirmed_enrollment']['public']),'Enrollment count mismatch: '+source)
        print(f'{source}: independently assessed {row_count:,} rows',flush=True)
    for source in SOURCES+['CPI']:require(ids[source]<=ids['CTC'],'Orphan patient IDs: '+source)
    require(set(bounds)==ids['CTC'],'Context/raw patient universe mismatch')
    for source in ['MCD','MCP','PHARM']:require(ids[source]<=found,'Event membership outside confirmed MCE: '+source)
    require(pairs['CTC']==pairs['ABM'] and all(v==1 for v in pairs['CTC'].values()),'Tumor pair coverage/uniqueness mismatch')
    require(projections['CCDI']==projections['CPI'] and all(v==1 for v in projections['CPI'].values()),'CPI projection/uniqueness mismatch')
    mapping={}
    for patient,resource_id,resource_name in projections['CPI']:
        key=(resource_name,resource_id);require(key not in mapping or mapping[key]==patient,'Ambiguous CPI identity');mapping[key]=patient
    coverage=[]
    for p in policies:
        source,field=p['source'],p['field'];blank=raw_counts[source][field][''];semantic=semantics[p['semantic_id']]
        require(not blank or semantic['missingness']['blank_allowed'],'Forbidden blanks: '+source+'.'+field)
        if p['mode']=='blank':require(blank==counts[source]['rows'],'Blank policy not followed: '+source+'.'+field)
        coverage.append({'source':source,'field':field,'mode':p['mode'],'blank_cells':blank,'all_blank':blank==counts[source]['rows']})
    require(sha(Path(run['coverage']['path']))==run['coverage_sha256'],'Altered coverage report')
    claimed={(r['source'],r['field']):r for r in read(Path(run['coverage']['path']))}
    require(set(claimed)=={(s,f) for s in SOURCES+['CPI'] for f in headers[s]},'Incomplete coverage report')
    for source in SOURCES+['CPI']:
        for field in headers[source]:
            require(claimed[(source,field)]['blank_cells']==raw_counts[source][field][''],'Inaccurate coverage count')
            if source=='CPI':require(raw_counts[source][field]['']==0,'Blank CPI field')
    approved=common['release-policy.json']['annotation_exceptions'][0];exceptions=[]
    require({s['source'] for s in audit['sources']}==set(SOURCES+['CPI']),'Incomplete audit source inventory')
    files={f['source']:f for f in manifest['files']}
    for source in audit['sources']:
        name=source['source'];require(source['sha256_matches_manifest'] is True,'Audit payload hash failed: '+name)
        require(source['sha256']==files[name]['sha256'] and Path(source['resolved_path']).resolve()==Path(files[name]['local_path']).resolve(),'Audit payload binding mismatch: '+name)
        require(not source['metadata_errors'] and not source.get('row_width_errors') and not source.get('duplicate_headers'),'Raw format error: '+name)
        require(source.get('status') in ['passed_checks','findings'],'Incomplete raw audit: '+name)
        if name!='CPI':require(source.get('metadata_equal') is True,'Metadata mismatch: '+name)
        for label,check in source['checks'].items():
            require(not any(check.get(k) for k in ['missing_columns','extra_columns','vocabulary_issues','numeric_type_issues','whitespace_cells']),'Raw validation failed: '+name)
            for advisory in check['length_advisories']:
                field=advisory['field'];actual={v:c for v,c in raw_counts[name][field].items() if len(v)>advisory['stated_max']}
                permitted=name==approved['source'] and field in approved['fields'] and advisory['stated_max']==approved['declared_length'] and set(actual)=={approved['code']} and sum(actual.values())==advisory['cells']
                require(permitted,'Unapproved width advisory: '+name+'.'+field)
                if permitted:exceptions.append({'source':name,'field':field,'dictionary':label,'cells':advisory['cells']})
    fresh=evaluate_rules(rules,audit['sources'],context=manifest['context'],contract=pinned(manifest['context_contract']))
    require(fresh==audit['rule_results'],'Fresh rule evaluation differs from retained audit')
    require(len(fresh)==len(rules),'Incomplete rule inventory')
    for rule,result in zip(rules,fresh):require(result['status']==('passed' if rule['enabled'] else 'disabled'),'Rule failed/unevaluated: '+rule['id'])
    ro=defaultdict(list)
    for row in radiation:ro[row['nccrId']].append(row)
    require(len({r['recordId'] for r in radiation})==len(radiation),'Duplicate radiation record ID')
    for group in ro.values():
        unknown=any(r['months_from_dx_to_tx_start']=='9999' for r in group)
        ordered=sorted((r for r in group if r['months_from_dx_to_tx_start']!='9999'),key=lambda r:(int(r['months_from_dx_to_tx_start']),int(r['recordId'])))
        for rank,row in enumerate(ordered,1):require(row['sequenceId']==str((900 if unknown else 800)+rank),'Radiation sequence mismatch')
        for row in group:
            if row['months_from_dx_to_tx_start']=='9999':require(row['sequenceId']=='999','Unknown radiation start needs sequence 999')
    reports={s:read(snapshot/'normalized'/f'{s.lower()}.json') for s in SOURCES};comparisons=[];descriptive=[];K=0
    hierarchy=next((r for r in rules if r['enabled'] and r['type']=='hierarchical_lookup'),None)
    for p in policies:
        source,field,mode=p['source'],p['field'],p['mode']
        if mode in ['context','mce_context','enrollment'] or (mode=='numeric_bin' and p.get('temporal')) or (hierarchy and source=='CTC' and field in hierarchy['parent_columns']):
            descriptive.append({'source':source,'field':field,'status':'derived_or_conditioned_descriptive_only','observed':dict(raw_counts[source][field])});continue
        if field not in observed[source]:continue
        item=reports[source]['fields'][field];obs=observed[source][field]
        if mode=='numeric_bin':
            chart=item['charts'][p['chart_index']];total=sum(r['count'] for r in chart['values']);target={r['value']:r['count']/total for r in chart['values']};bins=Counter()
            for value,count in obs.items():bins[numeric_label(value,p,chart)]+=count
            obs=bins
        else:target=independent_target(item,p)
        K+=len(target);comparisons.append({'source':source,'field':field,'target':target,'observed':dict(obs),'n':sum(obs.values())})
    for c in comparisons:
        size=c['n'];bound=math.sqrt(math.log(2*K/.001)/(2*size)) if size else None
        maximum=max((abs(c['observed'].get(k,0)/size-v) for k,v in c['target'].items()),default=0) if size else None
        passed=not size or (not set(c['observed'])-set(c['target']) and maximum<=bound)
        require(passed,'Statistical review required: '+c['source']+'.'+c['field'])
        c.update(status='not_applicable' if not size else ('passed' if passed else 'statistical_review_required'),absolute_bound=bound,maximum_absolute_deviation=maximum,familywise_K=K)
    metrics=run['metrics'];resources=run.get('resources') or {}
    require(metrics['peak_rss_bytes']<resources.get('memory_limit_bytes',0),'Generation exceeds available-memory budget')
    audit_resources=provenance.get('resources') or {}
    require(audit['metrics']['peak_rss_bytes']<audit_resources.get('available_memory_bytes',0)/2,'Audit exceeds available-memory budget')
    require(sum(Path(f['local_path']).stat().st_size for f in manifest['files'])==metrics['disk_payload_bytes'],'Payload size metric mismatch')
    if full:
        if factor==Decimal(population['full_scale']):
            require(sum(counts[s]['rows'] for s in SOURCES)==population['full_total_source_rows'],'Full total count mismatch')
            require(counts['CPI']['rows']==common['generation-contract.json']['CPI']['full_rows'],'Full CPI target mismatch')
        require(resources.get('forecast') is not None,'Missing benchmark forecast')
    return {'findings':findings,'size':request,'observed_counts':counts,
            'coverage':coverage,'annotation_exceptions':exceptions,'rule_results':fresh,
            'statistics':comparisons,'derived_or_conditioned':descriptive}
