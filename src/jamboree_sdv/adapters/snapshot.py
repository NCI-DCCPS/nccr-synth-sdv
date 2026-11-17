"""Preserve public code labels and explicitly expand declared numeric bins."""
from collections import defaultdict
import math
import re
from ..common import read_json, shared_tools


def compile_distribution(item, policy):
    index=policy['chart_index']
    if type(index) is not int or index<0 or index>=len(item['charts']):
        raise ValueError('Invalid chart selection')
    chart=item['charts'][index]
    weights={};labels=set();excluded=[];source_total=0
    for row in chart['values']:
        label,count=row['value'],row['count']
        if not isinstance(label,str) or label in labels:
            raise ValueError('Duplicate or non-string reported code')
        labels.add(label)
        if type(count) not in (int,float) or not math.isfinite(count) or count<0:
            raise ValueError(f'Unknown or invalid count for {label}')
        source_total+=count
        if label in policy.get('excluded_codes',[]):
            excluded.append({'value':label,'count':count});continue
        value=policy.get('label_codes',{}).get(label,label)
        match=re.fullmatch(r'(\d+) to (\d+)',value)
        tail=re.fullmatch(r'> (\d+)',value)
        if match or tail:
            rp=policy.get('range_policy',{})
            if rp.get('within_bin')!='uniform_integer':raise ValueError('Range needs explicit finite expansion policy')
            lo=int(match[1]) if match else int(tail[1])+1
            hi=int(match[2]) if match else rp.get('open_upper_bound')
            if type(hi) is not int or not 0<=lo<=hi<=999:
                raise ValueError('Invalid bin bounds')
            values=[str(n) for n in range(lo,hi+1)]
        else:
            if value in ['Missing','Not Available Due to Imputed Month of Diagnosis']:
                raise ValueError('Report label needs explicit dictionary code mapping')
            values=[value]
        for value in values:
            if value in weights:raise ValueError('Overlapping chart outcomes')
            weights[value]=count/len(values)
    total=sum(weights.values())
    if not math.isfinite(total) or total<=0:raise ValueError('No positive known retained count')
    return {'categories':list(weights),'probabilities':[v/total for v in weights.values()],
            'source_count_sum':source_total,'retained_count_sum':total,'excluded':excluded,
            'source_chart_index':index,'target_scope':'conditional_on_retained_reported_support'}


def adapt_fields(recipe):
    shared_tools()
    from audit_release import compile_values,allowed,finite_number
    semantics={s['id']:s for s in recipe.common['field-semantics.json']['fields']}
    fields=recipe.profiles['field-policies']['fields']
    expected=[];documents={};headers={}
    for source in recipe.spec['sources']:
        document=read_json(recipe.snapshot/'normalized'/f'{source.lower()}.json')
        dictionary=read_json(recipe.snapshot/'metadata'/f'{source.lower()}_metadata.json')
        headers[source]=[x['Column_Name_at_Source'] for x in dictionary['Dictionary_Elements']]
        expected.extend((source,f) for f in headers[source]);documents[source]=document
    if [(x['source'],x['field']) for x in fields]!=expected:
        raise ValueError('Field policies must exactly match every dictionary field in source order')
    compiled={};coverage=[]
    optional_keys={'chart_index','count_policy','unreported_support','reason','condition','excluded_codes','otherwise','range_policy','label_codes'}
    for policy in fields:
        if set(policy)-({'source','field','semantic_id','grain','mode'}|optional_keys):
            raise ValueError('Unknown field policy key')
        source,field=policy['source'],policy['field'];key=f'{source}.{field}'
        semantic=semantics[policy['semantic_id']]
        if semantic['source']!=source or semantic['field']!=field or semantic['grain']!=policy['grain']:
            raise ValueError('Policy conflicts with shared field semantics')
        item=documents[source]['fields'][field];mode=policy['mode'];entry=item['definition']
        if 'label_codes' in policy:
            dictionary_labels={str(v.get('Description','')):str(v['Value']) for v in entry.get('Permissible_Values',[])}
            if any(dictionary_labels.get(label)!=code for label,code in policy['label_codes'].items()):
                raise ValueError('Report label mapping conflicts with dictionary meaning')
        if 'range_policy' in policy:
            rp=policy['range_policy']
            if (set(rp)!={'within_bin','open_upper_bound','reason'}
                    or rp['within_bin']!='uniform_integer' or type(rp['open_upper_bound']) is not int
                    or rp['open_upper_bound']!=999
                    or '0-999' not in [str(v['Value']) for v in entry.get('Permissible_Values',[])]):
                raise ValueError('Unsupported range expansion policy')
        state={'source':source,'field':field,'mode':mode,'grain':policy['grain'],
               'required':semantic['required'],'blank_allowed':semantic['missingness']['blank_allowed'],
               'distribution':None,'policy':policy}
        if mode=='blank':
            if not state['blank_allowed']:raise ValueError('Blank forbidden by shared semantics')
        elif mode in ['identity','tumor_number']:
            if field!=('nccrId' if mode=='identity' else 'tumorRecordNumber'):raise ValueError('Unknown identity derivation')
        elif mode in ['sample','conditional_count','hierarchy_parent_or_sample']:
            if policy.get('count_policy')!='normalize_known_reported_support' or policy.get('unreported_support')!='unknown_not_zero':
                raise ValueError('Unsupported distribution policy')
            if mode=='conditional_count' and (source!='COG' or policy.get('condition')!={'field':'reportedCOGEnrollment','equals':'1'} or policy.get('excluded_codes')!=['7777'] or policy.get('otherwise')!='7777'):
                raise ValueError('Unsupported conditional count policy')
            distribution=compile_distribution(item,policy)
            pv=entry.get('Permissible_Values',[]);vocab=compile_values(pv)
            for value in distribution['categories']:
                if pv and not allowed(value,vocab):raise ValueError(f'Invalid output vocabulary: {key}={value}')
                if entry['Column_DataType_at_Source']['Semantic_Type'] in ['numeric','float'] and finite_number(value) is None:
                    raise ValueError(f'Invalid numeric outcome: {key}={value}')
            state['distribution']=distribution
        else:raise ValueError(f'Unsupported field mode {mode}')
        compiled[key]=state
        coverage.append({'source':source,'field':field,'mode':mode,'grain':state['grain']})
    return {'fields':compiled,'headers':headers,'coverage':coverage}

