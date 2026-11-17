"""All-source rows from public marginals and explicit synthetic context."""
from collections import defaultdict
import re
import numpy as np
from .copula import LatentCopula,categorical
from .random_streams import seed_for
from .full_population import FullPopulation
from .context import make_context,render_context,month,mce_code
from .numeric_bins import numeric_support,sample_numeric
from .common import read_json


class FullSources:
    def __init__(self,recipe,adapted):
        self.recipe,self.adapted=recipe,adapted;self.streams={};self.population=FullPopulation(recipe)
        self.numeric_cache={}
        self.patients,self.tumors,self.groups=make_context(self)
        self.attributes={key:self.draw(key,len(self.patients)) for key,s in adapted['fields'].items()
                         if s['source']=='CTC' and s['grain']=='patient' and s['mode']=='sample'}
        self.confirmed={s:set(map(int,values)) for s,values in self.population.confirmed.items()}
        mce=read_json(recipe.snapshot/'normalized/mce.json')
        enrolled=next(r['count'] for r in mce['fields']['enrollIndexDxMonth']['charts'][0]['values'] if r['value']=='1')
        self.mce_probability=enrolled/recipe.common['generation-contract.json']['sources']['MCE']['counts']['confirmed_enrollment']['public']
        if not 0<=self.mce_probability<=1:raise ValueError('Invalid MCE availability prior')

    def uniform(self,key,n):
        if key not in self.streams:self.streams[key]=LatentCopula([key],[[1]],seed_for(self.recipe.spec['seed'],key))
        return self.streams[key].uniform(n)[:,0]

    def draw(self,key,n,condition=None):
        d=self.adapted['fields'][key]['distribution'];categories=d['categories'];weights=d['probabilities']
        if condition:
            chosen=[i for i,x in enumerate(categories) if condition(x)]
            categories=[categories[i] for i in chosen];weights=[weights[i] for i in chosen]
        stream=self.adapted['catalog_policy']['profile']['stream'] if self.adapted['fields'][key]['mode']=='catalog_sample' else key
        return categorical(self.uniform(stream,n),categories,weights)

    def numeric(self,key,patient,minimum=None,uniforms=None):
        state=self.adapted['fields'][key];lo=hi=None
        if state['policy'].get('temporal'):
            start=month(patient['synthetic_index_month']);lo=max(-999,month(patient['synthetic_birth_month'])-start)
            hi=min(999,month(patient['coverage_end_month'])-start)
            if patient['deceased']:hi=min(hi,patient['death_offset_months'])
            if minimum is not None:lo=max(lo,minimum)
        cache_key=(key,lo,hi)
        if cache_key not in self.numeric_cache:self.numeric_cache[cache_key]=numeric_support(state['distribution'],lo,hi,('9999',) if state['policy'].get('temporal') else ())
        u,v=uniforms if uniforms is not None else (self.uniform(key,1)[0],self.uniform(key+'.within',1)[0])
        return sample_numeric(self.numeric_cache[cache_key],u,v)

    def rows(self,source):
        if source not in self.recipe.spec['sources']:raise ValueError('Unknown source')
        pop=self.population;header=self.adapted['headers'][source];total=self.recipe.targets[source]['rows']
        self.numeric_cache={}
        # Only radiation needs per-patient retrospective sequencing (<=3 rows in these pilots).
        radiation=[]
        for start in range(0,total,self.recipe.spec['chunk_size']):
            stop=min(total,start+self.recipe.spec['chunk_size']);size=stop-start;drawn={};uniforms={}
            for field in header:
                key=source+'.'+field;state=self.adapted['fields'][key]
                if state['mode'] in ['sample','hierarchy_parent_or_sample','catalog_sample'] and key not in self.attributes:drawn[field]=self.draw(key,size)
                if state['mode']=='numeric_bin':uniforms[field]=(self.uniform(key,size),self.uniform(key+'.within',size))
                if state['mode']=='mce_context':uniforms[field]=self.uniform(key,size)
            for j,ordinal in enumerate(range(start,stop)):
                idx=int(pop.row_patient[source][ordinal]);patient=self.patients[idx]
                row={'nccrId':str(pop.ids[idx])}
                if source in ['CTC','ABM']:row['tumorRecordNumber']=str(pop.tumor_numbers[ordinal])
                if source=='CTC':row.update(render_context(patient,self.tumors[ordinal],self.groups[idx],self.recipe.common['context-schema.json']['raw_bindings']))
                if source in ['COG','MCE']:row['reportedCOGEnrollment' if source=='COG' else 'reportedInsuranceEnrollment']='1' if idx in self.confirmed[source] else '0'
                for field in header:
                    if field in row:continue
                    key=source+'.'+field;state=self.adapted['fields'][key];mode=state['mode']
                    if key in self.attributes:row[field]=str(self.attributes[key][idx])
                    elif field in drawn:row[field]=str(drawn[field][j])
                    elif mode in ['blank','drug_tuple']:row[field]=''
                    elif mode=='conditional_count':row[field]=str(self.draw(key,1)[0]) if idx in self.confirmed['COG'] else '7777'
                    elif mode=='mce_context':
                        offset=0 if field=='enrollIndexDxMonth' else int(re.search(r'(\d+)$',field)[1])*(-1 if 'Prior' in field else 1)
                        row[field]=mce_code(patient,offset,idx in self.confirmed['MCE'],uniforms[field][j],self.mce_probability)
                    elif mode=='numeric_bin':
                        minimum=int(row['months_from_dx_to_tx_start']) if field=='months_from_dx_to_treatment_end' and row['months_from_dx_to_tx_start']!='9999' else None
                        row[field]=self.numeric(key,patient,minimum,(uniforms[field][0][j],uniforms[field][1][j]))
                    elif mode=='resource_id':row[field]=f'SDV_RESOURCE_{ordinal+1:012d}'
                    elif mode=='radiation_id':row[field]=str(10000000+ordinal) if field=='recordId' else ''
                    elif mode=='derived_hierarchy':continue
                    else:raise ValueError('Unimplemented field ownership: '+key)
                h=self.adapted['hierarchy']
                if source=='CTC' and h:
                    row.update(h['mapping'][row[h['leaf_column']]])
                if source=='RO':radiation.append(row)
                else:yield {f:row[f] for f in header}
        if source=='RO':
            groups=defaultdict(list)
            for row in radiation:groups[row['nccrId']].append(row)
            for rows in groups.values():
                unknown=any(r['months_from_dx_to_tx_start']=='9999' for r in rows)
                ordered=sorted((r for r in rows if r['months_from_dx_to_tx_start']!='9999'),key=lambda r:(int(r['months_from_dx_to_tx_start']),int(r['recordId'])))
                for rank,row in enumerate(ordered,1):row['sequenceId']=str((900 if unknown else 800)+rank)
                for row in rows:
                    if row['months_from_dx_to_tx_start']=='9999':row['sequenceId']='999'
            yield from radiation
