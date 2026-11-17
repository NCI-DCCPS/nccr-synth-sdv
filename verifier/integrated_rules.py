"""Full independent rule evaluation using shared base evaluators and new checks."""
from collections import Counter, defaultdict
import csv
from fractions import Fraction
import io
import math
from pathlib import Path
import zipfile

from release_rules import evaluate_rules
from jamboree_sdv.integrated_inputs import load_inputs
from jamboree_sdv.common import checked_pin, digest, read_json
from check_catalog_rules import check as check_catalog, zipped_csv
from check_pharm_enrichment import CANMED, RXNORM, NO_CANMED, check_drug_row, expected_counts, counts, month, require


def rows(path, source):
    with zipfile.ZipFile(path) as archive:
        require(set(archive.namelist())=={source+'.csv',source.lower()+'_metadata.json'} and len(archive.namelist())==2,
                'Unexpected ZIP members: '+source)
        with archive.open(source+'.csv') as binary, io.TextIOWrapper(binary,encoding='utf-8',newline='') as stream:
            reader=csv.DictReader(stream)
            for row in reader:
                require(None not in row and all(v is not None for v in row.values()),'Malformed '+source+' row')
                yield row


def check_pharm(path, inputs, plan, patient_context):
    public,canmed_lookup,rx_lookup=(inputs[k] for k in ['public','canmed','rxnorm'])
    canmed={r['fields']['medicationRequestNdc11']:r['fields'] for r in canmed_lookup['records']}
    attrs={r['fields']['medicationRequestNdc11']:r['source_attributes'] for r in canmed_lookup['records']}
    all_canmed=set(canmed)|{r['ndc11'] for r in canmed_lookup['excluded_records']}
    rxnorm={r['fields']['medicationRequestNdc11']:r['fields'] for r in rx_lookup['records']}
    require(len(canmed)==len(canmed_lookup['records']) and len(rxnorm)==len(rx_lookup['records']), 'Duplicate drug lookup key')
    observed={k:Counter() for k in CANMED+RXNORM};states=Counter();groups=Counter();packages=defaultdict(Counter)
    temporal=Counter();ids=set();n=0
    for row in rows(path,'PHARM'):
        require(all(row.values()),'Blank PHARM value')
        state=check_drug_row(row,canmed,all_canmed,rxnorm);states[state]+=1;n+=1;ids.add(row['nccrId'])
        key=(state,row['canmedDrugCategory'],row['canmedNonProprietaryName'].casefold()) if state=='canmed_matched' else (state,row['rxnormDisplayName'] if state=='rxnorm_matched' else 'UNK')
        groups[key]+=1;packages[key][row['medicationRequestNdc11']]+=1
        for k in observed:observed[k][row[k]]+=1
        require(row['nccrId'] in patient_context,'PHARM patient missing context')
        context=patient_context[row['nccrId']]
        if row['monthsFromIndexDxtoDispense']=='9999':temporal['unknown_dispensing_month']+=1;continue
        actual=month(context['synthetic_index_month'])+int(row['monthsFromIndexDxtoDispense'])
        high=month(context['coverage_end_month'])
        if context['deceased']=='true':high=min(high,month(context['synthetic_index_month'])+int(context['death_offset_months']))
        require(month(context['synthetic_birth_month'])<=actual<=high,'PHARM event outside patient timeline')
        temporal['valid_patient_timeline']+=1
        if state=='canmed_matched':
            item=attrs[row['medicationRequestNdc11']]
            start,end=item['package_effective_date'],item['package_discontinuation_date']
            if not start:temporal['canmed_unknown_effective_date']+=1
            if not end:temporal['canmed_unknown_discontinuation_date']+=1
            if start and actual<month(start):temporal['canmed_dispense_before_package_effective_month']+=1
            if end and actual>month(end):temporal['canmed_dispense_after_package_discontinuation_month']+=1
    require(n==plan['rows'] and n>0,'PHARM plan/row count mismatch')
    categories=expected_counts(counts(public,'canmedDrugCategory'),n)
    require(observed['canmedDrugCategory']==Counter(categories),'PHARM category allocation mismatch')
    missing=round(Fraction(counts(public,'rxnormDisplayName')['Missing']*n,public['total_records']))
    require(states['unresolved']==missing,'PHARM missingness allocation mismatch')
    names={k.casefold():v for k,v in counts(public,'canmedNonProprietaryName').items() if not k.startswith('Not found')}
    expected={};evidence=plan['frequency_evidence']
    for category,size in categories.items():
        if category==NO_CANMED:continue
        available={f['canmedNonProprietaryName'].casefold() for f in canmed.values() if f['canmedDrugCategory']==category}
        supported=available&names.keys()
        if not supported:require(evidence['category_only_name_allocations'].get(category)==sorted(available),'Undeclared uniform name allocation')
        weights={k:names[k] for k in supported} if supported else dict.fromkeys(available,1)
        for name,count in expected_counts(weights,size).items():expected[('canmed_matched',category,name)]=count
    rx_public=counts(public,'rxnormDisplayName');supported_rx={f['rxnormDisplayName'] for f in rxnorm.values()}
    for name,count in expected_counts({k:rx_public[k] for k in supported_rx},categories[NO_CANMED]-missing).items():
        expected[('rxnorm_matched',name)]=count
    expected[('unresolved','UNK')]=missing
    require(groups==Counter(expected),'PHARM effective group allocation mismatch')
    generic=counts(public,'canmedNonProprietaryName');supported_canmed={f['canmedNonProprietaryName'].casefold() for f in canmed.values()}
    require(evidence['canmed_unreported_count']==public['total_records']-sum(generic.values()),'Unreported CanMED mass missing')
    require(evidence['rxnorm_unreported_count']==public['total_records']-sum(rx_public.values()),'Unreported RxNorm mass missing')
    require(evidence['canmed_unsupported_names']=={k:v for k,v in generic.items() if not k.startswith('Not found') and k.casefold() not in supported_canmed},'CanMED exclusions missing')
    require(evidence['rxnorm_unsupported_names']=={k:v for k,v in rx_public.items() if k!='Missing' and not k.startswith('Not sought') and k not in supported_rx},'RxNorm exclusions missing')
    seen=set();tests=[]
    for group in plan['groups']:
        key=(group['state'],group['category'],group['name']) if group['state']=='canmed_matched' else (group['state'],group['name'])
        require(key not in seen and key in expected and group['count']==expected[key],'Invalid PHARM plan group');seen.add(key)
        valid={ndc for ndc,f in canmed.items() if f['canmedDrugCategory']==group['category'] and f['canmedNonProprietaryName'].casefold()==group['name']} if group['state']=='canmed_matched' else ({ndc for ndc,f in rxnorm.items() if f['rxnormDisplayName']==group['name']} if group['state']=='rxnorm_matched' else {'UNK'})
        options={f['medicationRequestNdc11'] for f in group['tuples']}
        require(options==valid and len(options)==len(group['tuples']),'Altered PHARM tuple support')
        count=expected[key]
        if count:
            bound=math.sqrt(math.log(2*(len(canmed)+len(rxnorm)+1)/.001)/(2*count))
            deviation=max(abs(packages[key][ndc]/count-1/len(options)) for ndc in options)
            require(deviation<=bound,'PHARM package allocation statistical failure')
            tests.append({'group':list(key),'n':count,'options':len(options),'bound':bound,'deviation':deviation})
    require(seen==set(expected),'Missing PHARM plan groups')
    deviations=[]
    for field in CANMED+RXNORM:
        if not public['fields'].get(field,{}).get('charts'):continue
        published=counts(public,field)
        def label(v):
            if v==NO_CANMED or v.startswith('Not found in CanMED'):return NO_CANMED.casefold()
            if v=='7-Not Sought in RxNorm' or v.startswith('Not sought in RxNorm'):return 'not sought'
            if v in ['9-Missing','Missing']:return 'missing'
            return v.casefold()
        actual,target=Counter(),Counter()
        for k,v in observed[field].items():actual[label(k)]+=v
        for k,v in published.items():target[label(k)]+=v
        deviations.append({'field':field,'unreported_count':public['total_records']-sum(published.values()),
            'comparisons':[{'label':k,'actual':actual[k],'public':target[k],
                'fraction_difference':actual[k]/n-target[k]/public['total_records']} for k in sorted(actual.keys()|target.keys())]})
    return {'status':'passed','rows':n,'patients':len(ids),'states':dict(states),'frequency_evidence':evidence,
            'package_checks':tests,'temporal_checks':dict(temporal),'frequency_deviations':deviations}


def evaluate_all(recipe,manifest,sources,rules,*,resolver=None,adapted=None):
    resolve=resolver or checked_pin
    inputs=load_inputs(recipe,resolver=resolve)
    extras={r['id'] for r in rules if r['id'].startswith(('pharm_','ccdi_cpi_'))}
    base=evaluate_rules([r for r in rules if r['id'] not in extras],sources,
                        context=manifest['context'],contract=resolve(manifest['context_contract']))
    if adapted is None:
        run=read_json(resolve(manifest['run_manifest']))
        adapted_path=Path(manifest['run_manifest']['path']).parent/'inputs/adapted/preflight.json'
        require(digest(adapted_path)==run['archives']['adapted_sha256'],'Altered adapted input')
        adapted=read_json(adapted_path)
    plan=adapted['adapted']['pharm_plan']
    raw={f['source']:f for f in manifest['raw_files']};processed={f['source']:f for f in manifest['files']}
    for item in list(raw.values())+list(processed.values()):
        require(digest(item['local_path'])==item['sha256'],'Payload hash mismatch')
    ctc={r['nccrId'] for r in rows(processed['CTC']['local_path'],'CTC')}
    catalog=check_catalog(zipped_csv(raw['CCDI']['local_path']),Path(raw['CPI']['local_path']).read_bytes(),ctc,inputs['catalog'])
    context_manifest=Path(manifest['context']['path']);require(digest(context_manifest)==manifest['context']['sha256'],'Context hash mismatch');context=read_json(context_manifest)
    patient_path=context_manifest.parent/context['patient']['path']
    require(digest(patient_path)==context['patient']['sha256'],'Context hash mismatch')
    with patient_path.open(newline='') as stream:patients={r['nccrId']:r for r in csv.DictReader(stream)}
    pharm=check_pharm(processed['PHARM']['local_path'],inputs['pharm'],plan,patients)
    results={r['id']:r for r in base}
    for rule in rules:
        if rule['id'] in extras:results[rule['id']]={'id':rule['id'],'type':rule['type'],'status':'passed',
                                                  'evidence':'extension_checks.'+('pharm' if rule['id'].startswith('pharm_') else 'catalog')}
    checks={'catalog':catalog,'pharm':pharm}
    if 'multiplicity' in inputs:
        from check_tumor_multiplicity import check as check_multiplicity
        checks['tumor_multiplicity']=check_multiplicity(
            rows(processed['CTC']['local_path'],'CTC'),rows(processed['ABM']['local_path'],'ABM'),
            inputs['multiplicity'],adapted['adapted']['tumor_multiplicity'],recipe['patients'],
            adapted['adapted']['targets']['CTC']['rows'])
    return [results[r['id']] for r in rules],checks
