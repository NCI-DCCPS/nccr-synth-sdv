"""One synthetic patient universe and a declared bounded tumor allocation."""
import numpy as np
from .random_streams import rng_for


class Population:
    def __init__(self, patients, tumor_rows, seed, *, multiplicity=None):
        if multiplicity is None:
            if not 1<=patients<=tumor_rows<=2*patients:
                raise ValueError('Legacy pilot allocation supports one or two tumors per patient')
            counts=np.ones(patients,dtype=int)
            chosen=rng_for(seed,'population.tumor_allocation').permutation(patients)[:tumor_rows-patients]
            counts[chosen]+=1
        else:
            if (any(type(k) is not int or not 1<=k<=99 or type(v) is not int or v<0 for k,v in multiplicity.items())
                    or sum(multiplicity.values())!=patients or sum(k*v for k,v in multiplicity.items())!=tumor_rows):
                raise ValueError('Invalid patient multiplicity allocation')
            counts=np.repeat(sorted(multiplicity),[multiplicity[k] for k in sorted(multiplicity)])
            counts=rng_for(seed,'population.tumor_multiplicity').permutation(counts)
        self.ids=np.array([f'SDV{i+1:010d}' for i in range(patients)],dtype=object)
        self.tumor_patient=np.repeat(np.arange(patients),counts)
        self.tumor_numbers=np.array([f'{j+1:02d}' for count in counts for j in range(count)],dtype=object)

