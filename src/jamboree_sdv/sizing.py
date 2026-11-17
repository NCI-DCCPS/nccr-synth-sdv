"""Explicit patient-count and exact-scale requests, with independent rounding."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext


def rounded_count(value, factor):
    """Keep supplied decimal digits through multiplication and integer rounding."""
    with localcontext() as context:
        context.prec = max(28, len(factor.as_tuple().digits) + len(str(value)) + 2)
        return int((Decimal(value) * factor).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def resolve_size(request, population, contract=None):
    if not isinstance(request,dict) or set(request)!={'kind','value'}:raise ValueError('Size requires kind and value')
    unrestricted=(contract or {}).get('version')=='1.2.0'
    if unrestricted and (contract.get('scale_policy')!='positive_finite_resource_gated'
            or contract.get('minimum_patients')!=100 or contract.get('maximum_patients') is not None
            or 'scale_range' in contract):raise ValueError('Invalid resource-gated size contract')
    base=population['base_patients']
    if request['kind']=='patients':
        n=request['value']
        if type(n) is not int or n<100 or (not unrestricted and n>10000):raise ValueError('Invalid --patients for selected size contract')
        return {'kind':'patients','value':n},n,Decimal(n)/Decimal(base)
    if request['kind']!='scale' or not isinstance(request['value'],str):raise ValueError('Scale must be an exact decimal string')
    try:scale=Decimal(request['value'])
    except InvalidOperation as exc:raise ValueError('Invalid scale') from exc
    reference_scale=Decimal(population['full_scale'])
    bounds=(contract or {}).get('scale_range')
    if unrestricted:
        if not scale.is_finite() or scale<=0:raise ValueError('--scale must be positive and finite')
    elif bounds is None:
        if not scale.is_finite() or scale!=reference_scale:
            raise ValueError('Only --scale 0.05 is supported by the pinned legacy contract')
    else:
        if not isinstance(bounds,dict) or set(bounds)!={'minimum','maximum'} or any(not isinstance(v,str) for v in bounds.values()):
            raise ValueError('Invalid contract scale range')
        try:lower,upper=(Decimal(bounds[k]) for k in ['minimum','maximum'])
        except InvalidOperation as exc:raise ValueError('Invalid contract scale range') from exc
        if not lower.is_finite() or not upper.is_finite() or not 0<lower<=upper<=reference_scale:
            raise ValueError('Invalid contract scale range')
        if not scale.is_finite() or not lower<=scale<=upper:
            raise ValueError(f'--scale must be between {lower} and {upper}, inclusive')
    patients=rounded_count(base,scale)
    if patients<100:raise ValueError('Scale must resolve to at least 100 patients')
    with localcontext() as context:
        context.prec=max(28,len(scale.as_tuple().digits))
        canonical=str(scale.normalize())
    return {'kind':'scale','value':canonical},patients,scale


def scaled_targets(common, request, contract=None):
    size,n,factor=resolve_size(request,common['population'],contract);targets={}
    full=size['kind']=='scale'
    reference_full=full and factor==Decimal(common['population']['full_scale'])
    def count(value):return rounded_count(value,factor)
    for source,definition in common['sources'].items():
        c=definition['counts'];raw_rows=count(c['public_rows']);raw_patients=count(c['public_patients'])
        patients=n if source in ['CTC','ABM','COG','MCE'] else (raw_patients if full else max(1,raw_patients))
        rows=raw_rows if full else max(1,patients,raw_rows)
        if rows<patients or patients<1:raise ValueError('Infeasible source target: '+source)
        if reference_full and (rows!=c['full_rows'] or patients!=c['full_patients']):raise ValueError('Full count disagrees with shared contract: '+source)
        targets[source]={'rows':rows,'patients':patients,'row_adjustment':rows-raw_rows,'patient_adjustment':patients-raw_patients}
        if 'confirmed_enrollment' in c:targets[source]['confirmed']=count(c['confirmed_enrollment']['public'])
    return size,n,targets
