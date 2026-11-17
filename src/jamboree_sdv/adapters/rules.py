"""Explicit rule capability table and deterministic dependency ordering."""
from collections import defaultdict

KNOWN={'all_source_patient_ids_belong_to_ctc','ctc_patient_id_may_repeat_for_multiple_tumors',
       'abm_cog_mce_cover_all_ctc_patients','cog_mce_have_one_row_per_ctc_patient',
       'cog_status_and_study_counts_are_consistent','mce_status_and_monthly_enrollment_are_consistent',
       'ctc_iccc_2017_hierarchy_is_consistent'}


def topological_order(dependencies):
    pending={k:set(v) for k,v in dependencies.items()};ordered=[]
    while pending:
        ready=sorted(k for k,v in pending.items() if not v.intersection(pending))
        if not ready:raise ValueError('Cyclic derivation dependencies')
        for key in ready:ordered.append(key);pending.pop(key)
    return ordered


def adapt_rules(recipe, adapted):
    capabilities=[];hierarchy=None;dependencies={}
    for rule in recipe.rules:
        if not rule['enabled']:
            capabilities.append({'id':rule['id'],'status':'disabled'});continue
        if rule['id'] not in KNOWN:raise ValueError(f'No generator implementation for enabled rule {rule["id"]}')
        if rule.get('source')=='MCE':
            capabilities.append({'id':rule['id'],'status':'out_of_scope','reason':'MCE/context unsupported by this core pilot'});continue
        capabilities.append({'id':rule['id'],'status':'external_enforcement','native_copulas_support':False,
                             'excluded_sources':[s for s in rule.get('child_sources',rule.get('sources',[])) if s not in recipe.spec['sources']] if isinstance(rule.get('child_sources',rule.get('sources',[])),list) else []})
        if rule['type']=='enrollment_field_consistency':
            for f in rule['dependent_fields']:dependencies[f'COG.{f}']=['COG.'+rule['status_field']]
        if rule['type']=='hierarchical_lookup':
            if hierarchy is not None:raise ValueError('Conflicting ICCC field ownership')
            hierarchy=rule
            leaf=adapted['fields']['CTC.'+rule['leaf_column']]['distribution']
            missing=set(leaf['categories'])-set(rule['mapping'])
            if missing:raise ValueError(f'Unmapped reported ICCC codes: {missing}')
            discrepancies={}
            for parent in rule['parent_columns']:
                key='CTC.'+parent
                if adapted['fields'][key]['mode']!='hierarchy_parent_or_sample':raise ValueError('Conflicting hierarchy field policy')
                dependencies[key]=['CTC.'+rule['leaf_column']]
                sums=defaultdict(float)
                for code,p in zip(leaf['categories'],leaf['probabilities']):sums[rule['mapping'][code][parent]]+=p
                state=adapted['fields'][key];original=state['distribution'];state['original_distribution']=original
                state['distribution']={**original,'categories':sorted(sums),'probabilities':[sums[c] for c in sorted(sums)],'target_scope':'derived_from_reported_extended_codes'}
                state['mode']='derived_hierarchy'
                discrepancies[parent]={'public_count_sum':original['source_count_sum'],'effective_leaf_count_sum':leaf['retained_count_sum']}
            adapted['iccc_discrepancies']={'unreported_valid_leaf_codes':sorted(set(rule['mapping'])-set(leaf['categories'])),
                                         'missing_frequency_policy':'unknown; excluded from first pilot support', 'parents':discrepancies,
                                         'metadata_discrepancies':rule['known_metadata_discrepancies']}
    adapted['hierarchy']=hierarchy
    adapted['capabilities']=capabilities
    adapted['dependencies']=dependencies
    adapted['derivation_order']=topological_order(dependencies)
    return adapted

