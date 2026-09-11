"""Attribute actual B fills to inventory roles and submission-time quote state.

No hypothetical fills. All bins are descriptive, not selected trading rules.
Inventory is reconstructed from exchange-time ordered fills and checked against
the final snapshot. Crossing-flat fills are split into reducing/increasing lots.
Quote state is attached through successful order acknowledgement, with price
validation. Markout groups are volume-weighted; fills are not independent.
"""
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent

def ts(value):
    return datetime.fromisoformat(value).timestamp()

def aggregate(items):
    volume = sum(x['volume'] for x in items)
    result = {'fill_parts':len(items), 'volume':volume}
    for h in (1,5,15,30,60):
        marked = [x for x in items if h in x['marks']]
        marked_volume = sum(x['volume'] for x in marked)
        if marked_volume:
            result[str(h)] = {
                'volume':marked_volume,
                'per_share':sum(x['volume']*x['marks'][h] for x in marked)/marked_volume,
                'submission_edge':sum(x['volume']*x['edge'] for x in marked)/marked_volume,
                'subsequent_move':sum(x['volume']*(x['marks'][h]-x['edge']) for x in marked)/marked_volume}
    return result

results = []
for path in sorted((ROOT/'data/runs/baseline').glob('*20260910*/*.jsonl')):
    rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    rows_b = [r for r in rows if r.get('instrument')=='PHILIPS_B']
    snapshots = [r for r in rows_b if r['type']=='snapshot']
    initial, final = snapshots[0], snapshots[-1]
    fills = sorted((r for r in rows_b if r['type']=='fill'), key=lambda r:(ts(r['trade_timestamp']), r['trade_id']))
    assert len({r['trade_id'] for r in fills})==len(fills)
    marks = defaultdict(dict)
    for r in rows_b:
        if r['type']=='markout':
            assert r['horizon_seconds'] not in marks[r['trade_id']]
            marks[r['trade_id']][r['horizon_seconds']] = r['per_share']
    orders = {}
    quote = None
    for r in rows_b:
        if r['type']=='quote':
            quote = r
        if r['type']=='order_response' and r.get('success'):
            valid = (quote is not None and quote.get('book_scope')=='excluding_own_orders'
                     and abs(quote[r['side']+'_price']-r['price'])<1e-7)
            orders[r['order_id']] = quote if valid else None
    position = initial['position']
    parts = []
    excluded = 0
    excursions = []
    current = None
    for f in fills:
        sign = 1 if f['side']=='bid' else -1
        reducing = min(abs(position),f['volume']) if position*sign<0 else 0
        quote = orders.get(f['order_id'])
        cycle_bin = 'unknown'
        if quote is None:
            excluded += f['volume']
        else:
            mid = (quote['best_bid']+quote['best_ask'])/2
            spread = quote['best_ask']-quote['best_bid']
            # These instruments use a 0.1 tick in the recorded strategy.
            spread_bin = '<=2 ticks' if spread<=.2000001 else '3-5 ticks' if spread<=.5000001 else '>5 ticks'
            raw_fair = quote['fair_value']-quote.get('cycle_shift',0)
            tilt = sign*(raw_fair-mid)
            tilt_bin = '<-0.5 tick' if tilt<-.05 else '>+0.5 tick' if tilt>.05 else 'within 0.5 tick'
            cycle = quote.get('cycle',{})
            risk = sign*quote.get('cycle_risk_shift',quote.get('cycle_shift',0))
            cycle_bin = 'inactive' if not cycle.get('active') else 'adverse' if risk<=-.05 else 'favorable' if risk>=.05 else 'weak'
            for role, volume in (('reducing',reducing),('increasing',f['volume']-reducing)):
                if volume:
                    parts.append(dict(volume=volume, marks=marks[f['trade_id']], role=role,
                        side=f['side'],spread=spread_bin,tilt=tilt_bin,cycle=cycle_bin,
                        edge=sign*(mid-f['price'])))
        # Exact cash PnL of complete flat-to-flat excursions, ignoring inherited
        # inventory until it is fully closed. Handle a crossing fill by splitting.
        if position==0:
            current = {'start':ts(f['trade_timestamp']),'cash':0.,'volume':0,
                       'entry_cycle':cycle_bin,'entry_side':f['side'],
                       'first_price':f['price'],'start_utc':f['trade_timestamp']}
        if current is not None:
            closing_volume = reducing if reducing==abs(position) and position!=0 else f['volume']
            current['cash'] -= sign*f['price']*closing_volume
            current['volume'] += closing_volume
            if position!=0 and reducing==abs(position):
                current['seconds'] = ts(f['trade_timestamp'])-current.pop('start')
                current['end_utc'] = f['trade_timestamp']
                current['last_price'] = f['price']
                excursions.append(current)
                current = None
                remainder = f['volume']-reducing
                if remainder:
                    current = {'start':ts(f['trade_timestamp']),'cash':-sign*f['price']*remainder,'volume':remainder,
                               'entry_cycle':cycle_bin,'entry_side':f['side'],
                               'first_price':f['price'],'start_utc':f['trade_timestamp']}
        elif position!=0 and reducing==abs(position) and f['volume']>reducing:
            remainder = f['volume']-reducing
            current = {'start':ts(f['trade_timestamp']),'cash':-sign*f['price']*remainder,'volume':remainder,
                       'entry_cycle':cycle_bin,'entry_side':f['side'],
                       'first_price':f['price'],'start_utc':f['trade_timestamp']}
        position += sign*f['volume']
    assert position==final['position'], path
    cash = sum((1 if f['side']=='ask' else -1)*f['price']*f['volume'] for f in fills)
    assert abs(cash-(final['cash']-initial['cash']))<1e-6
    if initial['position']==final['position']==0:
        assert abs(sum(e['cash'] for e in excursions)-cash)<1e-6
    grouped = {}
    for dimension in ('role','side','spread','tilt','cycle'):
        buckets = defaultdict(list)
        for item in parts:
            buckets[item[dimension]].append(item)
        grouped[dimension] = {key:aggregate(value) for key,value in buckets.items()}
    role_cycle = defaultdict(list)
    for item in parts:
        role_cycle[item['role']+' / '+item['cycle']].append(item)
    grouped['role_cycle'] = {key:aggregate(value) for key,value in role_cycle.items()}
    durations = sorted(e['seconds'] for e in excursions)
    n = len(durations)
    median = (durations[(n-1)//2]+durations[n//2])/2 if n else None
    result = dict(run=path.parent.name, excluded_quote_volume=excluded,groups=grouped,
        round_trips=dict(count=n,wins=sum(e['cash']>1e-7 for e in excursions),
            losses=sum(e['cash']< -1e-7 for e in excursions),
            cash=sum(e['cash'] for e in excursions),median_seconds=median),
        excursions=excursions)
    result['round_trips_by_entry_cycle'] = {
        label:dict(count=len(ee),cash=sum(e['cash'] for e in ee),wins=sum(e['cash']>1e-7 for e in ee))
        for label in sorted({e['entry_cycle'] for e in excursions})
        if (ee := [e for e in excursions if e['entry_cycle']==label])}
    results.append(result)
(OUT/'fill_diagnostic.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
for r in results:
    print(r['run'], 'unmatched volume',r['excluded_quote_volume'],'round trips',r['round_trips'])
    print('round trips by entry cycle',r['round_trips_by_entry_cycle'])
    for dimension, groups in r['groups'].items():
        print(dimension, {k:dict(volume=v['volume'],m1=round(v.get('1',{}).get('per_share',0),3),
                 m15=round(v.get('15',{}).get('per_share',0),3),
                 edge1=round(v.get('1',{}).get('submission_edge',0),3)) for k,v in groups.items()})
