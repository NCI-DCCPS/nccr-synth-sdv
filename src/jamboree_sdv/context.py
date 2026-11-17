"""Synthetic calendar generation and raw bindings, independent of the verifier."""
import numpy as np
from .random_streams import rng_for


def month(text):
    y,m=map(int,text.split('-'));return y*12+m-1


def month_text(value):return f'{value//12:04d}-{value%12+1:02d}'


def render_context(patient,tumor,siblings,bindings):
    out={};age=tumor['age_completed_years'];death=month(patient['synthetic_index_month'])+patient['death_offset_months'] if patient['deceased'] else None
    dead=death is not None and death<=month(patient['vital_status_cutoff_month'])
    for key,b in bindings.items():
        field=key.split('.',1)[1];op=b['op']
        if op=='tumor_value':v=tumor[b['field']]
        elif op in ['single_age','grouped_age']:
            v=b['unknown'] if not tumor['age_is_known'] else str(min(age if op=='single_age' else (0 if age==0 else age//5+1),b['cap'])).zfill(b['width'])
        elif op=='boolean_flag':v=int(tumor[b['field']])
        elif op=='index_behavior':v=(b['index_malignant'] if tumor['behavior_code'] in b['malignant'] else b['index_nonmalignant']) if tumor['is_index'] else b['nonindex']
        elif op=='behavior_sequence':
            malignant=tumor['behavior_code'] in b['malignant'];group=[t for t in siblings if (t['behavior_code'] in b['malignant'])==malignant]
            v=(b['single_malignant'] if malignant else b['single_nonmalignant']) if len(group)==1 else (0 if malignant else 60)+group.index(tumor)+1
            v=str(v).zfill(b['width'])
        elif op=='record_order':v=str(tumor['tumor_order']).zfill(b['width'])
        elif op=='vital_at_cutoff':v=b['dead'] if dead else b['alive']
        elif op=='unknown_death_cause':v=b['nonindex'] if not tumor['is_index'] else (b['dead_unknown'] if dead else b['alive'])
        elif op in ['followup_months','followup_flag']:
            end=month(patient[b['endpoint']]);end=min(end,death) if death is not None else end
            months=max(0,end-month(tumor['diagnosis_month']));v=months if op=='followup_months' else (b['positive'] if months else b['zero'])
        elif op=='optional_treatment_offset':v=b['missing'] if tumor[b['field']] is None else tumor[b['field']]
        else:raise ValueError('Unimplemented context operation '+op)
        out[field]=str(v)
    return out


def make_context(generator):
    recipe=generator.recipe;pop=generator.population;n=len(pop.ids);rng=rng_for(recipe.spec['seed'],'calendar.context')
    cutoff=month('2022-12');patients=[];tumors=[];by_patient=[]
    years=generator.draw('CTC.yearOfDiagnosis',n,lambda x:int(x)<=2022)
    ages=generator.draw('CTC.ageRecodeSingle90p',n);vitals=generator.draw('CTC.vitalStatusRecode',n)
    behaviors=generator.draw('CTC.behaviorICDO3Public',len(pop.tumor_patient))
    counts=np.bincount(pop.tumor_patient,minlength=n);cursor=0
    empirical=getattr(recipe,'profiles',{}).get('model',{}).get('tumor_allocation')=='pinned_patient_histogram_exact_totals'
    for i,identity in enumerate(pop.ids):
        count=int(counts[i]);year=int(years[i])
        if empirical:
            latest_start=cutoff-count+1
            if year*12>latest_start:
                year=int(generator.draw('CTC.yearOfDiagnosis',1,lambda x:int(x)*12<=latest_start)[0])
            start=year*12+int(rng.integers(0,min(12,latest_start-year*12+1)))
        else:
            start=year*12+int(rng.integers(0,12 if year<2022 else 13-count))
        raw_age=int(ages[i]);known=raw_age!=99
        age=int(rng.integers(0,96)) if not known else (int(rng.integers(90,96)) if raw_age==90 else raw_age)
        birth=start-age*12-int(rng.integers(0,12))
        if empirical:
            available=min(999,cutoff-start,birth+121*12-1-start)
            if available<count-1:raise ValueError('Insufficient distinct feasible tumor months')
            offsets=[0,*sorted(map(int,rng.choice(available,count-1,replace=False)+1))]
        else:
            gap=int(rng.integers(1,min(999,cutoff-start)+1)) if count==2 else 0
            offsets=[0]+[gap]*(count-1)
        last=start+offsets[-1];dead=str(vitals[i])=='0';death=int(rng.integers(last,cutoff+1)) if dead else None
        contact=int(rng.integers(last,(death if dead else cutoff)+1))
        patient={'nccrId':str(identity),'index_tumor_number':'01','index_year':year,'index_age_completed_years':age,
                 'synthetic_index_month':month_text(start),'coverage_end_month':'2023-12','deceased':dead,
                 'death_offset_months':death-start if dead else None,'synthetic_birth_month':month_text(birth),
                 'active_followup_end_month':month_text(contact),'vital_status_cutoff_month':'2022-12',
                 'observation_start_month':month_text(start),'observation_end_month':'2022-12'}
        group=[]
        for j in range(count):
            date=start+offsets[j]
            tumor={'nccrId':str(identity),'tumorRecordNumber':f'{j+1:02d}','is_index':j==0,
                   'diagnosis_month':month_text(date),'diagnosis_year':date//12,'age_completed_years':(date-birth)//12,
                   'age_is_known':known,'months_from_index':date-start,'tumor_order':j+1,'number_of_primaries':count,
                   'imputed_month_flag':False,'behavior_code':str(behaviors[cursor+j])}
            for suffix in ['Brm','Chemo','Hormone','MostDefinSurg','Radiation','Surgery','OtherTx']:
                value=str(generator.draw('CTC.monthsFromDxTo'+suffix,1,lambda x:x=='9999' or (x!='6666' and 0<=int(x)<=contact-date))[0])
                tumor['treatment_offset_'+suffix]=None if value=='9999' else int(value)
            group.append(tumor);tumors.append(tumor)
        patients.append(patient);by_patient.append(group);cursor+=count
    return patients,tumors,by_patient


def mce_code(patient,offset,found,uniform,probability):
    start=month(patient['synthetic_index_month']);death=patient['death_offset_months']
    if offset>0 and patient['deceased'] and death<month(patient['coverage_end_month'])-start and offset>death:return '8'
    if (offset<0 and (patient['index_year']==2000 or patient['index_age_completed_years']<1)) or (1<=offset<=12 and patient['index_year']==1999):return '6'
    if not found or start+offset>month(patient['coverage_end_month']) or start+offset<month(patient['synthetic_birth_month']):return '7'
    return '1' if uniform<probability else '7'
