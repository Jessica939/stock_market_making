"""Conditional exit replay of actual B inventory-increasing fill lots.

No exchange connection. Frozen historical entries, independent counterfactual
exits; sums are not portfolio backtest PnL. FIFO pairs observed exits. Raw books
may contain own orders: conservative scenario removes first 200 lots on the
liquidation side (logged max outstanding volume). No assumed passive fills.
"""
import bisect
import gzip
import json
from collections import deque, Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
def ts(value):
    return datetime.fromisoformat(value).timestamp()

def liquidate(book, sign, quantity, remove):
    levels = book['bids'] if sign==1 else book['asks']
    best = levels[0][0]
    remaining, value = quantity, 0.
    for price, size in levels:
        # Do not sweep remote boundary liquidity to fabricate a normal exit.
        if abs(price-best)>1.0000001:
            break
        removed = min(remove,size)
        remove -= removed
        size -= removed
        take = min(remaining,size)
        value += take*price
        remaining -= take
        if remaining==0:
            return value/quantity
    return None

def summary(entries, horizon=None):
    if not entries:
        return {'entries':0,'volume':0}
    volume = sum(e['quantity'] for e in entries)
    result = dict(entries=len(entries),volume=volume)
    if horizon is None:
        cash = sum(e['actual_cash'] for e in entries)
    else:
        cash = sum(e['marks'][str(horizon)]['cash'] for e in entries)
        result['raw_cash'] = sum(e['marks'][str(horizon)]['raw_cash'] for e in entries)
        result['worst_sampled_loss_per_share'] = min(e['marks'][str(horizon)]['worst_per_share'] for e in entries)
        result['median_sampled_adverse_per_share'] = sorted(e['marks'][str(horizon)]['adverse_per_share'] for e in entries)[len(entries)//2]
    result.update(cash=cash,per_share=cash/volume)
    return result

results = []
truncated_files = set()
def read_books(path):
    with gzip.open(path,'rt',encoding='utf-8') as f:
        while True:
            try:
                line = f.readline()
            except EOFError:
                truncated_files.add(path.relative_to(ROOT).as_posix())
                return
            if not line:
                return
            yield json.loads(line)

for path in sorted((ROOT/'data/runs/baseline').glob('*20260910*/*.jsonl')):
    rows = [json.loads(s) for s in path.read_text(encoding='utf-8').splitlines() if s.strip()]
    start, end = ts(rows[0]['observed_at_utc']),ts(rows[-1]['observed_at_utc'])
    settings = next(r for r in rows if r.get('action')=='settings')
    own_bound = settings['max_outstanding_volume']
    # Join market observations by instrument and exact time window, not run
    # ownership. Legacy manifests do not record an explicit recording link.
    books, sources = [], []
    for market in sorted((ROOT/'data/market').glob('philips_20260910*')):
        selected = []
        for zipped in sorted(market.glob('orderbooks*.jsonl.gz')):
            for b in read_books(zipped):
                observed = ts(b['observed_at_utc'])
                if b['instrument_id']=='PHILIPS_B' and start<=observed<=end:
                    b['_t'] = observed
                    selected.append(b)
        if selected:
            sources.append(market.relative_to(ROOT).as_posix())
            books.extend(selected)
    books.sort(key=lambda b:b['_t'])
    times = [b['_t'] for b in books]
    def usable(b):
        return (b['status']=='ok' and b.get('bids') and b.get('asks')
            and b['bids'][0][0]<b['asks'][0][0]
            and 0<=b['_t']-ts(b['book_timestamp'])<=1)
    quotes, orders = {}, {}
    for r in rows:
        if r.get('instrument')!='PHILIPS_B':
            continue
        if r['type']=='quote':
            quotes['last'] = r
        elif r['type']=='order_response' and r.get('success'):
            q = quotes.get('last')
            if q and abs(q[r['side']+'_price']-r['price'])<1e-7:
                orders[r['order_id']] = q
    snap = [r for r in rows if r['type']=='snapshot' and r['instrument']=='PHILIPS_B']
    position = snap[0]['position']
    fifo = deque([{'remaining':abs(position),'entry':None}] if position else [])
    entries = []
    fills = sorted((r for r in rows if r['type']=='fill' and r['instrument']=='PHILIPS_B'),key=lambda r:(ts(r['trade_timestamp']),r['trade_id']))
    for f in fills:
        sign = 1 if f['side']=='bid' else -1
        volume = f['volume']
        if position*sign<0:
            close = min(abs(position),volume)
            volume -= close
            while close:
                item = fifo[0]
                take = min(item['remaining'],close)
                if item['entry'] is not None:
                    e = item['entry']
                    e['actual_cash'] += take*e['sign']*(f['price']-e['price'])
                    e['actual_closed'] += take
                item['remaining'] -= take
                close -= take
                if not item['remaining']:
                    fifo.popleft()
        if volume:
            q = orders.get(f['order_id'],{})
            risk = sign*q.get('cycle_risk_shift',q.get('cycle_shift',0))
            label = 'inactive' if not q.get('cycle',{}).get('active') else 'favorable' if risk>=.05 else 'adverse' if risk<=-.05 else 'weak'
            e = dict(trade_id=f['trade_id'],time=ts(f['trade_timestamp']),utc=f['trade_timestamp'],
                     price=f['price'],quantity=volume,sign=sign,cycle=label,
                     actual_cash=0.,actual_closed=0,marks={},missing={})
            entries.append(e)
            fifo.append({'remaining':volume,'entry':e})
        position += sign*f['volume']
    assert position==snap[-1]['position']
    if snap[0]['position']==snap[-1]['position']==0:
        assert abs(sum(e['actual_cash'] for e in entries)-(snap[-1]['cash']-snap[0]['cash']))<1e-6
    for e in entries:
        for h in (0,1,5,15):
            target = e['time']+h
            index = bisect.bisect_left(times,target)
            candidates = []
            while index<len(books) and times[index]<=target+1:
                b = books[index]
                if usable(b) and ts(b['book_timestamp'])>=target:
                    candidates.append(b)
                    break
                index += 1
            if not candidates:
                e['missing'][str(h)]='no_fresh_book_within_1s'
                continue
            b = candidates[0]
            price = liquidate(b,e['sign'],e['quantity'],own_bound)
            raw = liquidate(b,e['sign'],e['quantity'],0)
            if price is None or raw is None:
                e['missing'][str(h)]='insufficient_depth_within_10_ticks'
                continue
            # Max adverse excursion over sampled executable values only; not a
            # continuous-path bound. Missing path depth makes risk incomplete.
            path_books = books[bisect.bisect_left(times,e['time']):index+1]
            values, unavailable = [],0
            for pb in path_books:
                if not usable(pb) or ts(pb['book_timestamp'])<e['time']:
                    unavailable+=1
                    continue
                px = liquidate(pb,e['sign'],e['quantity'],own_bound)
                if px is None:
                    unavailable+=1
                else:
                    values.append(e['sign']*(px-e['price']))
            per_share = e['sign']*(price-e['price'])
            assert values and price is not None
            assert e['sign']*(raw-price)>=-1e-7
            e['marks'][str(h)] = dict(cash=per_share*e['quantity'],raw_cash=e['quantity']*e['sign']*(raw-e['price']),
                exit_vwap=price,exit_observed=b['observed_at_utc'],delay=b['_t']-target,
                worst_per_share=min(values),adverse_per_share=max(0,-min(values)),
                path_samples=len(values),path_unavailable=unavailable)
    # All horizons and actual exit use exactly the same entry cohort.
    common = [e for e in entries if all(str(h) in e['marks'] for h in (0,1,5,15)) and e['actual_closed']==e['quantity']]
    cohorts = {}
    for label, subset in [('all',common),('favorable',[e for e in common if e['cycle']=='favorable']),('other',[e for e in common if e['cycle']!='favorable'])]:
        cohorts[label] = {'actual_fifo':summary(subset),**{str(h):summary(subset,h) for h in (0,1,5,15)}}
    for label in cohorts:
        subset = common if label=='all' else [e for e in common if (e['cycle']=='favorable')==(label=='favorable')]
        for h in (0,1,5,15):
            events = []
            for e in subset:
                events.extend([(e['time'],e['quantity']), (ts(e['marks'][str(h)]['exit_observed']),-e['quantity'])])
            gross, peak = 0,0
            for _,delta in sorted(events):
                gross += delta
                peak = max(peak,gross)
            cohorts[label][str(h)]['overlapping_gross_lots'] = peak
            cohorts[label][str(h)]['entries_with_path_gaps'] = sum(e['marks'][str(h)]['path_unavailable']>0 for e in subset)
    result = dict(run=path.parent.name,market_sources=sources,market_samples=len(books),
        truncated_market_files=[p for p in sorted(truncated_files) if any(p.startswith(s+'/') for s in sources)],
        own_depth_removal=own_bound,total_entries=len(entries),total_volume=sum(e['quantity'] for e in entries),
        missing=dict(Counter(reason for e in entries for reason in e['missing'].values())),
        eligible_by_horizon={str(h):sum(str(h) in e['marks'] for e in entries) for h in (0,1,5,15)},
        cohorts=cohorts,entries=entries)
    results.append(result)
(OUT/'exit_replay.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
for r in results:
    print(r['run'],'entries',r['total_entries'],'volume',r['total_volume'],'sources',r['market_sources'],'missing',r['missing'])
    for label,cohort in r['cohorts'].items():
        print(label,{h:{k:round(v,4) if isinstance(v,float) else v for k,v in s.items()} for h,s in cohort.items()})
