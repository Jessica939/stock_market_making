"""Experimental size overlay. Pure functions; no live strategy imports."""
import math

SETTINGS = dict(threshold=.6, radius_ticks=4, official_min_exclusive=200,
                boundary_min=15000, size_fraction=.5, tick=.1,
                max_age_seconds=2., max_spread_ticks=20)


def signal(bids, asks, age, settings=SETTINGS):
    if not bids or not asks or not -.5 <= age <= settings['max_age_seconds']:
        return None, 'missing_or_stale'
    tick=settings['tick']
    spread=asks[0][0]-bids[0][0]
    if not 0 < spread <= settings['max_spread_ticks']*tick+1e-8:
        return None, 'bad_spread'
    if max(bids[0][1],asks[0][1])>=settings['boundary_min']:
        return None, 'boundary_at_touch'
    volumes=[]
    for levels in (bids,asks):
        if any(not math.isfinite(p) or not math.isfinite(v) or p<=0 or v<=0 for p,v in levels):
            return None,'invalid_level'
        volumes.append(sum(v for p,v in levels
            if abs(p-levels[0][0])<=settings['radius_ticks']*tick+1e-8
            and settings['official_min_exclusive']<v<settings['boundary_min']))
    if not sum(volumes): return None,'no_official_near_depth'
    return (volumes[0]-volumes[1])/sum(volumes),'ok'


def apply(quote, imbalance, settings=SETTINGS):
    """Preserve prices and reducing capacity; halve only adverse new exposure.

    Keep a minimum of one lot when the original increasing portion is nonzero.
    Thus 1 stays 1, 2 becomes 1, 5 becomes 2. Existing baseline blocks remain.
    """
    out=dict(quote)
    if imbalance is None or not math.isfinite(imbalance): return out
    for side,key in [('bid','buy_volume'),('ask','sell_volume')]:
        adverse=imbalance<=-settings['threshold'] if side=='bid' else imbalance>=settings['threshold']
        if not adverse: continue
        size=quote[key]
        position=quote['position']
        reducing=min(size,max(0,-position if side=='bid' else position))
        increasing=size-reducing
        out[key]=reducing+(max(1,math.floor(increasing*settings['size_fraction'])) if increasing else 0)
    return out
