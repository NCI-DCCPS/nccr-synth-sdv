"""Finite discrete grids inside reported numeric bins, without expanding grids."""
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import math
import re

NUMBER = r'-?\d+(?:\.\d+)?'


def ticks(value, step, upper=False):
    return int((Decimal(str(value))/step).to_integral_value(
        rounding=ROUND_FLOOR if upper else ROUND_CEILING))


def compile_numeric(item, policy):
    if type(policy['chart_index']) is not int or not 0<=policy['chart_index']<len(item['charts']):raise ValueError('Invalid numeric chart selection')
    settings=policy['numeric_policy']; step=Decimal(settings['step'])
    if (set(settings)!={'step','lower','upper','within_bin','ambiguous_sentinel_mass'}
            or step not in [Decimal(1),Decimal('0.001')]
            or settings['within_bin']!='uniform_over_permitted_grid'
            or settings['ambiguous_sentinel_mass']!='uniform_over_permitted_grid'):
        raise ValueError('Unsupported numeric grid policy')
    lower,upper=Decimal(settings['lower']),Decimal(settings['upper'])
    if not lower.is_finite() or not upper.is_finite() or not -999<=lower<=upper<=10000:
        raise ValueError('Invalid finite numeric bounds')
    domains=[]
    for entry in item['definition'].get('Permissible_Values',[]):
        code=str(entry['Value']); match=re.fullmatch(f'({NUMBER})-({NUMBER})',code)
        if match:domains.append((Decimal(match[1]),Decimal(match[2])))
        elif re.fullmatch(NUMBER,code):domains.append((Decimal(code),Decimal(code)))
        else:raise ValueError('Non-numeric dictionary domain')
    if not domains:domains=[(lower,upper)]
    bins=[];seen=[];rows=item['charts'][policy['chart_index']]['values']
    literals={ticks(policy.get('label_codes',{}).get(r['value'],r['value']),step)
              for r in rows if re.fullmatch(NUMBER,policy.get('label_codes',{}).get(r['value'],r['value']))}
    for row in rows:
        label=row['value']; value=policy.get('label_codes',{}).get(label,label)
        count=row['count']
        if type(count) not in [int,float] or not math.isfinite(count) or count<0:
            raise ValueError('Unknown numeric-bin frequency')
        lo,hi=lower,upper; left_open=right_open=False
        match=re.fullmatch(f'({NUMBER}) to ({NUMBER})',value)
        half=re.fullmatch(f'({NUMBER})-<({NUMBER})',value)
        tail=re.fullmatch(rf'(>=|>|<=|<)\s*({NUMBER})',value)
        if match:lo,hi=Decimal(match[1]),Decimal(match[2])
        elif half:lo,hi=Decimal(half[1]),Decimal(half[2]);right_open=True
        elif tail:
            if tail[1] in ['>','>=']:lo=Decimal(tail[2]);left_open=tail[1]=='>'
            else:hi=Decimal(tail[2]);right_open=tail[1]=='<'
        elif re.fullmatch(NUMBER,value):lo=hi=Decimal(value)
        else:raise ValueError('Unmapped numeric label: '+value)
        a=ticks(lo,step);b=ticks(hi,step,True)
        if left_open and Decimal(a)*step==lo:a+=1
        if right_open and Decimal(b)*step==hi:b-=1
        segments=[]
        for dl,dh in domains:
            x=max(a,ticks(dl,step),ticks(lower,step));y=min(b,ticks(dh,step,True),ticks(upper,step,True))
            pieces=[(x,y)] if x<=y else []
            if match or half or tail:
                for literal in sorted(literals):
                    pieces=[piece for l,h in pieces for piece in
                            ([(l,literal-1),(literal+1,h)] if l<=literal<=h else [(l,h)]) if piece[0]<=piece[1]]
            for x,y in pieces:
                if any(max(x,l)<=min(y,h) for l,h in seen):raise ValueError('Overlapping numeric outcomes: '+policy['field']+' '+label)
                segments.append([x,y]);seen.append((x,y))
        if not segments:raise ValueError('Numeric bin has no permissible support: '+label)
        bins.append({'label':label,'count':count,'segments':segments,
                     'grid_points':sum(b-a+1 for a,b in segments)})
    if sum(b['count'] for b in bins)<=0:raise ValueError('Empty numeric mass')
    return {'kind':'numeric_grid','step':str(step),'bins':bins,
            'target_scope':'reported_bin_mass_uniform_permitted_grid_then_temporal_conditioning'}


def numeric_support(distribution, low=None, high=None, sentinels=()):
    """Prepare reusable conditional support; retain each bin's prior grid mass."""
    step=Decimal(distribution['step']);segments=[];weights=[]
    sentinel_ticks={int(Decimal(str(s))/step) for s in sentinels}
    for bin in distribution['bins']:
        for a,b in bin['segments']:
            cuts=[(a,b)]
            if low is not None or high is not None:
                x=max(a,ticks(low,step)) if low is not None else a
                y=min(b,ticks(high,step,True)) if high is not None else b
                cuts=[(x,y)] if x<=y else []
                cuts.extend((s,s) for s in sorted(sentinel_ticks) if a<=s<=b and not x<=s<=y)
            for x,y in cuts:
                segments.append((x,y));weights.append(bin['count']*(y-x+1)/bin['grid_points'])
    total=sum(weights)
    if total<=0:raise ValueError('No numeric probability mass within feasible window')
    return step,segments,weights,total


def sample_numeric(support,u,v):
    step,segments,weights,total=support
    threshold=float(u)*total; chosen=len(weights)-1
    for i,weight in enumerate(weights):
        threshold-=weight
        if threshold<0:chosen=i;break
    a,b=segments[chosen];tick=a+min(int(float(v)*(b-a+1)),b-a)
    value=Decimal(tick)*step
    return format(value,'f').rstrip('0').rstrip('.') if step<1 and '.' in format(value,'f') else str(value)


def draw_numeric(distribution,u,v,low=None,high=None,sentinels=()):
    return sample_numeric(numeric_support(distribution,low,high,sentinels),u,v)
