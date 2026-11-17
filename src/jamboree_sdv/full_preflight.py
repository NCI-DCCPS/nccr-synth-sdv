"""Compile all-source coverage and supplied distributions before any output."""
from .common import read_json,shared_tools,code_pins,environment,output_path,write_json,now,full_code_pins
from .adapters.snapshot import compile_distribution
from .adapters.rules import KNOWN
from .numeric_bins import compile_numeric


def adapt_full(recipe):
    shared_tools()
    integrated=recipe.spec['schema_version']==4
    extensions=None
    if integrated:
        from .integrated_inputs import load_inputs
        extensions=load_inputs(recipe.spec)
    from audit_release import compile_values,allowed
    semantics={s['id']:s for s in recipe.common['field-semantics.json']['fields']}
    context=recipe.common['context-schema.json'];fields={};headers={};expected=[];documents={}
    for source in recipe.spec['sources']:
        dictionary=read_json(recipe.snapshot/'metadata'/f'{source.lower()}_metadata.json')
        headers[source]=[e['Column_Name_at_Source'] for e in dictionary['Dictionary_Elements']]
        expected.extend((source,f) for f in headers[source])
        documents[source]=read_json(recipe.snapshot/'normalized'/f'{source.lower()}.json')
    policies=recipe.profiles['field-policies']['fields']
    if [(p['source'],p['field']) for p in policies]!=expected:raise ValueError('Incomplete or reordered field policies')
    modes={'identity','tumor_number','sample','conditional_count','hierarchy_parent_or_sample','context',
           'enrollment','mce_context','resource_id','radiation_id','blank','numeric_bin'}
    if integrated:modes|={'drug_tuple','catalog_sample'}
    for p in policies:
        allowed_keys={'source','field','semantic_id','grain','mode','chart_index','count_policy','unreported_support',
                      'reason','condition','excluded_codes','otherwise','range_policy','label_codes','numeric_policy','temporal'}
        if set(p)-allowed_keys:raise ValueError('Unknown field policy key')
        source,field=p['source'],p['field'];key=source+'.'+field;s=semantics[p['semantic_id']]
        if (s['source'],s['field'],s['grain'])!=(source,field,p['grain']):raise ValueError('Semantic mismatch: '+key)
        mode=p['mode'];item=documents[source]['fields'][field]
        if mode not in modes:raise ValueError('Unimplemented mode: '+mode)
        if integrated:
            from .pharm_enrichment import DRUG_FIELDS
            if (source=='PHARM' and field in DRUG_FIELDS)!=(mode=='drug_tuple'):raise ValueError('Drug tuple ownership mismatch')
            if (key=='CCDI.resourceName')!=(mode=='catalog_sample'):raise ValueError('Catalog ownership mismatch')
        if mode=='identity' and field!='nccrId':raise ValueError('Invalid identity ownership')
        if mode=='tumor_number' and (source not in ['CTC','ABM'] or field!='tumorRecordNumber'):raise ValueError('Invalid tumor ownership')
        if mode=='resource_id' and key!='CCDI.resourceParticipantId':raise ValueError('Invalid resource identity ownership')
        if mode=='radiation_id' and key not in ['RO.recordId','RO.sequenceId']:raise ValueError('Invalid radiation identity ownership')
        if mode=='enrollment' and key not in ['COG.reportedCOGEnrollment','MCE.reportedInsuranceEnrollment']:raise ValueError('Invalid enrollment ownership')
        if source=='MCE' and field not in ['nccrId','reportedInsuranceEnrollment'] and mode!='mce_context':raise ValueError('Mandatory timing ownership')
        if mode=='blank' and not s['missingness']['blank_allowed']:raise ValueError('Required field cannot be blank: '+key)
        if key in context['raw_bindings'] and mode!='context':raise ValueError('Context binding cannot be overridden: '+key)
        if mode=='context' and key not in context['raw_bindings']:raise ValueError('Unknown context field')
        if mode in {'sample','conditional_count','hierarchy_parent_or_sample','context','numeric_bin'} and item['charts']:
            if p.get('count_policy')!='normalize_known_reported_support' or p.get('unreported_support')!='unknown_not_zero':
                raise ValueError('Unsupported distribution policy: '+key)
        if mode=='conditional_count' and (source!='COG' or p.get('condition')!={'field':'reportedCOGEnrollment','equals':'1'}
                or p.get('excluded_codes')!=['7777'] or p.get('otherwise')!='7777'):
            raise ValueError('Unsupported conditional distribution policy: '+key)
        dictionary_labels={str(v.get('Description','')):str(v['Value']) for v in item['definition'].get('Permissible_Values',[])}
        for label,code in p.get('label_codes',{}).items():
            if key=='RO.radiation_dose_delivered' and label=='No Doses' and code=='0':continue
            if dictionary_labels.get(label)!=code:raise ValueError('Label mapping contradicts metadata: '+key)
        distribution=None
        if mode=='catalog_sample':
            from .catalog_linkage import conditioned_distribution
            counts=extensions['catalog']['counts'];total=sum(counts.values())
            distribution=conditioned_distribution({'categories':list(counts),'probabilities':[v/total for v in counts.values()]},extensions['catalog']['catalog'])
        elif mode=='numeric_bin':distribution=compile_numeric(item,p)
        elif mode in {'sample','conditional_count','hierarchy_parent_or_sample','context'} and item['charts']:
            distribution=compile_distribution(item,p)
            pv=item['definition'].get('Permissible_Values',[])
            if pv and any(not allowed(v,compile_values(pv)) for v in distribution['categories']):raise ValueError('Invalid chart outcome: '+key)
        elif mode in {'sample','conditional_count','hierarchy_parent_or_sample'}:raise ValueError('Missing public mass: '+key)
        fields[key]={'source':source,'field':field,'mode':mode,'grain':p['grain'],'policy':p,
                     'required':s['required'],'blank_allowed':s['missingness']['blank_allowed'],'distribution':distribution}
    known=KNOWN|{'synthetic_patient_tumor_context','mce_timing_from_synthetic_context'}
    if integrated:known|={r['id'] for r in recipe.rules_spec['rules'][10:]}
    hierarchy=None;capabilities=[]
    for rule in recipe.rules:
        if not rule['enabled']:capabilities.append({'id':rule['id'],'status':'disabled'});continue
        if rule['id'] not in known:raise ValueError('Unsupported enabled rule: '+rule['id'])
        capabilities.append({'id':rule['id'],'status':'external_enforcement','native_copulas_support':False})
        if rule['type']=='hierarchical_lookup':hierarchy=rule
        if rule['type']=='mce_timing_context' and rule['timing']!=context['mce']:raise ValueError('Timing contract/rule mismatch')
    if not {'synthetic_patient_tumor_context','mce_timing_from_synthetic_context'} <= {r['id'] for r in recipe.rules if r['enabled']}:
        raise ValueError('Both context rules required')
    if hierarchy:
        leaf=fields['CTC.'+hierarchy['leaf_column']]['distribution']
        if set(leaf['categories'])-set(hierarchy['mapping']):raise ValueError('Unmapped ICCC leaf')
        for parent in hierarchy['parent_columns']:fields['CTC.'+parent]['mode']='derived_hierarchy'
    result={'fields':fields,'headers':headers,'hierarchy':hierarchy,'capabilities':capabilities,
            'targets':recipe.targets,'scope':recipe.contract,
            'coverage':[{'source':s['source'],'field':s['field'],'mode':s['mode'],'grain':s['grain']} for s in fields.values()],
            'limitations':recipe.contract['limitations']+[
                'Diagnosis years conditioned to <=2022 for the frozen context cutoff; 2023 mass excluded explicitly.',
                'Top-coded age is uniform through 95; unknown age uses a finite synthetic age.',
                'PHARM uses pinned identity-only lookup tuples; historical availability is not established.' if integrated else 'Optional drug identity/classification fields are unpopulated.',
                'Monthly insurance availability uses an explicit index-month proxy; monthly public marginals are absent.',
                'Numeric bin sentinel ambiguity uses uniform permissible grid mass.',
                'Independent clinical code margins do not establish biological or clinical coherence.']}
    if integrated:
        from .pharm_enrichment import build_plan
        pharm=extensions['pharm']
        result['pharm_plan']=build_plan(pharm['public'],pharm['canmed'],pharm['rxnorm'],recipe.targets['PHARM']['rows'])
        result['catalog_policy']={k:extensions['catalog'][k] for k in ['counts','total_records','profile','inputs']}
        if 'multiplicity' in extensions:
            from .tumor_multiplicity import allocation_plan
            reference=extensions['multiplicity']
            population=recipe.common['generation-contract.json']['population']
            if (reference['patients']!=population['base_patients']
                    or reference['rows']!=recipe.common['generation-contract.json']['sources']['CTC']['counts']['public_rows']):
                raise ValueError('Multiplicity reference population disagrees with count contract')
            if recipe.targets['CTC']['rows']!=recipe.targets['ABM']['rows']:
                raise ValueError('CTC and ABM tumor targets disagree')
            result['tumor_multiplicity']=allocation_plan(reference,recipe.spec['patients'],recipe.targets['CTC']['rows'])
            result['limitations'] += [
                'Patient multiplicity uses frozen STAGE_TEST_ONLY counts; clinical representativeness is unverified.',
                'Multiplicity is assigned independently of demographics; diagnosis years are conditioned on enough distinct months before 2022-12.',
                'Rare multiplicity buckets can round to zero at smaller sizes; every bucket is within one patient of its reference quota.']
    return result


def compile_recipe(path):
    from .full_recipe import load_full_recipe
    recipe=load_full_recipe(path)
    # Include every used verifier and its transitive local imports. Other
    # generators' unrelated tools must not invalidate this local generation.
    entrypoints = (['audit_all_source_pilot','assess_all_source_pilot',
                    'assess_scaled_candidate','candidate_evidence']
                   if recipe.spec['schema_version']==3 else None)
    return recipe,{'schema_version':2,'kind':'gen_sdv_all_source_preflight','inputs':recipe.inputs,
                   'code':full_code_pins(True) if recipe.spec['schema_version']==4 else code_pins(entrypoints),'environment':environment(),'adapted':adapt_full(recipe)}


def preflight(path,out):
    destination=output_path(out);_,result=compile_recipe(path)
    destination.mkdir(parents=True,exist_ok=False)
    write_json(destination/'preflight.json',{'created_at':now(),**result});return result

MODEL = {'schema_version': 1, 'version': '2.0.0', 'dependence': 'independent_supplied_gaussian', 'correlation': 'identity', 'tumor_allocation': 'one_plus_uniform_without_replacement', 'membership': 'uniform_without_replacement', 'claims_membership': 'subset_of_confirmed_mce_assumption', 'events': 'balanced_round_robin', 'calendar_end': '2022-12', 'coverage_end': '2023-12', 'age_topcode_upper': 95, 'unknown_age': 'uniform_0_95', 'diagnosis_year': 'public_support_conditioned_at_2022', 'diagnosis_month': 'uniform_feasible_months', 'second_tumor_gap': 'uniform_positive_feasible_months', 'death': 'public_vital_rate_then_uniform_after_last_tumor_before_cutoff', 'followup': 'uniform_after_last_tumor_before_death_or_cutoff', 'treatment': 'public_offsets_conditioned_at_followup_missing_retained', 'mce_available_probability': 'public_index_enrolled_divided_by_confirmed_linkage', 'numeric_bins': 'uniform_permitted_grid', 'pharm_identity': 'blank_without_verified_lookup', 'procedure_text': 'blank_without_verified_lookup', 'unmodeled_joint': 'no_clinical_joint_fidelity_claim', 'resource_id': 'SDV_RESOURCE_12_digits'}
