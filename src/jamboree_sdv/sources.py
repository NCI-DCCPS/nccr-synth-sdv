"""Chunked core source generation using named streams and deterministic rules."""
import numpy as np
from .copula import LatentCopula,categorical
from .population import Population
from .random_streams import seed_for


class CoreSources:
    def __init__(self,recipe,adapted):
        self.recipe,self.adapted=recipe,adapted
        self.population=Population(recipe.spec['patients'],recipe.targets['CTC'],recipe.spec['seed'])
        self.streams={}
        self.patient_attributes={}
        for key,state in adapted['fields'].items():
            if state['source']=='CTC' and state['grain']=='patient' and state['mode']=='sample':
                self.patient_attributes[key]=self.draw(key,recipe.spec['patients'])

    def draw(self,key,n):
        if key not in self.streams:
            self.streams[key]=LatentCopula([key],[[1]],seed_for(self.recipe.spec['seed'],key))
        distribution=self.adapted['fields'][key]['distribution']
        return categorical(self.streams[key].uniform(n)[:,0],distribution['categories'],distribution['probabilities'])

    def rows(self,source):
        if source not in self.recipe.spec['sources']:raise ValueError('Unsupported source')
        header=self.adapted['headers'][source]
        total=self.recipe.targets[source];chunk=self.recipe.spec['chunk_size'];pop=self.population
        for start in range(0,total,chunk):
            stop=min(total,start+chunk);size=stop-start
            indices=pop.tumor_patient[start:stop] if source in ['CTC','ABM'] else np.arange(start,stop)
            values={'nccrId':pop.ids[indices]}
            if source in ['CTC','ABM']:values['tumorRecordNumber']=pop.tumor_numbers[start:stop]
            if source=='COG':values['reportedCOGEnrollment']=self.draw('COG.reportedCOGEnrollment',size)
            for field in header:
                if field in values:continue
                key=source+'.'+field;state=self.adapted['fields'][key];mode=state['mode']
                if mode=='derived_hierarchy':continue
                if mode=='blank':values[field]=np.full(size,'',dtype=object)
                elif key in self.patient_attributes:values[field]=self.patient_attributes[key][indices]
                elif mode=='conditional_count':
                    column=np.full(size,'7777',dtype=object)
                    eligible=values['reportedCOGEnrollment']=='1'
                    column[eligible]=self.draw(key,int(eligible.sum()))
                    values[field]=column
                elif mode in ['sample','hierarchy_parent_or_sample']:values[field]=self.draw(key,size)
                else:raise ValueError('Unimplemented field ownership: '+key)
            hierarchy=self.adapted['hierarchy']
            if source=='CTC' and hierarchy:
                for parent in hierarchy['parent_columns']:
                    values[parent]=[hierarchy['mapping'][str(leaf)][parent] for leaf in values[hierarchy['leaf_column']]]
            for i in range(size):yield {field:str(values[field][i]) for field in header}

