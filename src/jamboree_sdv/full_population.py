"""Exact synthetic membership and bounded, balanced event allocation."""
import numpy as np
from .population import Population
from .random_streams import rng_for


class FullPopulation(Population):
    def __init__(self,recipe):
        n=recipe.spec['patients'];seed=recipe.spec['seed']
        multiplicity=None
        if 'tumor-multiplicity' in recipe.spec['profiles']:
            from .multiplicity_reference import load_reference
            from .tumor_multiplicity import allocate_histogram
            reference=load_reference(recipe.spec['profiles']['tumor-multiplicity'])
            multiplicity=allocate_histogram(reference['histogram'],n,recipe.targets['CTC']['rows'])
        super().__init__(n,recipe.targets['CTC']['rows'],seed,multiplicity=multiplicity)
        self.members={s:np.arange(n) for s in ['CTC','ABM','COG','MCE']}
        self.confirmed={s:np.sort(rng_for(seed,'enrollment.'+s).permutation(n)[:recipe.targets[s]['confirmed']]) for s in ['COG','MCE']}
        for s in ['MCD','MCP','PHARM','RO','CCDI']:
            pool=self.confirmed['MCE'] if s in ['MCD','MCP','PHARM'] else np.arange(n)
            count=recipe.targets[s]['patients']
            if count>len(pool):raise ValueError('Insufficient eligible membership: '+s)
            self.members[s]=np.sort(rng_for(seed,'membership.'+s).permutation(pool)[:count])
        self.row_patient={s:self.members[s][np.arange(recipe.targets[s]['rows'])%len(self.members[s])] for s in recipe.spec['sources'] if s not in ['CTC','ABM']}
        self.row_patient.update(CTC=self.tumor_patient,ABM=self.tumor_patient)
