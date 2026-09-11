"""Compare recorded baseline quotes with an isolated bot-depth size guard.

No hypothetical fills, PnL, counterfactual position paths, or exchange connections.
Book joins require exact exchange timestamps and prior local observation.
"""
import bisect
from collections import Counter, defaultdict
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[2]
OUT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
from quote_helpers import external_price_book
from guard import SETTINGS, apply, signal


def ts(s):
    d=datetime.fromisoformat(s.replace('Z','+00:00'))
    if d.tzinfo is None: d=d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def nsbook(r):
    return SimpleNamespace(timestamp=datetime.fromisoformat(r['book_timestamp']),
        bids=[SimpleNamespace(price=p,volume=v) for p,v in r['bids']],
        asks=[SimpleNamespace(price=p,volume=v) for p,v in r['asks']])


sources=[]
books=defaultdict(list)
prices=defaultdict(list)
issues=[]
for folder in sorted((ROOT/'data/market').iterdir()):
    if not (folder/'prices.csv').exists(): continue
    d=pd.read_csv(folder/'prices.csv')
    for r in d.itertuples():
        prices[(folder.name,r.instrument_id)].append((ts(r.observed_at_utc),r.mid,r.spread))
    for path in folder.glob('orderbooks*.gz'):
        sources.append(str(path.relative_to(ROOT)))
        try:
            with gzip.open(path,'rt',encoding='utf-8') as f:
                for line in f:
                    r=json.loads(line)
                    r['observed']=ts(r['observed_at_utc'])
                    r['stamp']=ts(r['book_timestamp'])
                    r['session']=folder.name
                    books[(r['instrument_id'],r['stamp'])].append(r)
        except EOFError:
            issues.append(dict(file=str(path.relative_to(ROOT)),reason='incomplete_gzip_tail'))
for values in books.values(): values.sort(key=lambda r:r['observed'])
price_arrays={k:np.array(v) for k,v in prices.items()}
results=dict(settings=SETTINGS,current_version='baseline_cycle_risk_only_v2',
    issues=issues,runs=[],quote_outcomes=[],fill_outcomes=[],source_logs=[],
    assumptions=['Frozen recorded quote prices, positions and cycle state.',
        'Own-order ledger uses only order acknowledgements, cancel acknowledgements and fill reports available before source-book observation.',
        'Unknown inherited orders and read races cannot be fully reconstructed; exact external top agreement is required.',
        'Order-origin fill classification; later replacement/cancellation paths are not simulated.',
        'No hypothetical fills or strategy profit claimed.'])
quote_rows=[]
fill_rows=[]
for path in sorted((ROOT/'data/runs/baseline').glob('*/*.jsonl')):
    rows=[json.loads(line) for line in path.open(encoding='utf-8') if line.strip()]
    quotes=[r for r in rows if r.get('type')=='quote' and r['observed_at_utc'].startswith(('2026-09-09','2026-09-10'))]
    if not quotes: continue
    run=path.parent.name
    settings=next((r for r in rows if r.get('action')=='settings'),{})
    version=settings.get('strategy_version','unversioned')
    results['source_logs'].append(dict(path=str(path.relative_to(ROOT)),sha256=hashlib.sha256(path.read_bytes()).hexdigest(),version=version))
    mutations=[]
    for r in rows:
        kind=r.get('type')
        if kind in ('order_response','cancel_response','fill'):
            mutations.append((ts(r['observed_at_utc']),r))
    mutations.sort(key=lambda x:x[0])
    ledger={}
    pointer=0
    ledger_at={}
    # Cache known own ledger at each available source snapshot, causally.
    source_times=sorted({b['observed'] for q in quotes
        for b in books.get((q['instrument'],ts(q['book_timestamp'])),[])
        if b['observed']<=ts(q['observed_at_utc']) and ts(q['observed_at_utc'])-b['observed']<=2})
    for time in source_times:
        while pointer<len(mutations) and mutations[pointer][0]<=time:
            _,r=mutations[pointer]; pointer+=1
            key=(r.get('instrument'),r.get('order_id'))
            if r['type']=='order_response' and r.get('success'):
                ledger[key]=dict(side=r['side'],price=r['price'],volume=r['volume'])
            elif r['type']=='cancel_response' and r.get('success'):
                ledger.pop(key,None)
            elif r['type']=='fill' and key in ledger:
                ledger[key]['volume']-=r['volume']
                if ledger[key]['volume']<=0: ledger.pop(key,None)
        ledger_at[time]={key:value.copy() for key,value in ledger.items()}
    q_by_observed={}
    counters=Counter()
    for q in quotes:
        now=ts(q['observed_at_utc'])
        candidates=[b for b in books.get((q['instrument'],ts(q['book_timestamp'])),[])
                    if b['observed']<=now and now-b['observed']<=2]
        row=dict(run=run,version=version,instrument=q['instrument'],t=now,
                 position=q['position'],bid_price=q['bid_price'],ask_price=q['ask_price'],
                 base_bid=q['buy_volume'],base_ask=q['sell_volume'],
                 candidate_bid=q['buy_volume'],candidate_ask=q['sell_volume'],
                 signal=None,reason='no_exact_prior_book',session=None)
        imbalance=None
        if candidates:
            raw=candidates[-1]
            own_orders={oid:SimpleNamespace(**v) for (inst,oid),v in ledger_at[raw['observed']].items() if inst==q['instrument']}
            external=external_price_book(nsbook(raw),own_orders,.1)
            row['session']=raw['session']
            row['source_observed']=raw['observed']
            row['source_stamp']=raw['stamp']
            assert raw['observed']<=now and now-raw['stamp']<=2.5
            if not external or not external.bids or not external.asks:
                row['reason']='own_ledger_inconsistent_or_empty'
            elif abs(external.bids[0].price-q['best_bid'])>1e-7 or abs(external.asks[0].price-q['best_ask'])>1e-7:
                row['reason']='external_top_mismatch'
            else:
                imbalance,reason=signal([(x.price,x.volume) for x in external.bids],
                    [(x.price,x.volume) for x in external.asks],now-raw['stamp'])
                row.update(signal=imbalance,reason=reason)
        candidate=apply(q,imbalance)
        row.update(candidate_bid=candidate['buy_volume'],candidate_ask=candidate['sell_volume'])
        assert candidate['bid_price']==q['bid_price'] and candidate['ask_price']==q['ask_price']
        for side in ('bid','ask'):
            assert 0<=row['candidate_'+side]<=row['base_'+side]
        counters[row['reason']]+=1
        quote_rows.append(row)
        q_by_observed[(q['instrument'],q['observed_at_utc'])]=row
    # Attribute fills to the actual order's original submission and preceding quote.
    latest_quote={}
    orders={}
    markouts={}
    for r in rows:
        if r.get('type')=='markout':
            key=(r['instrument'],r['trade_id'],r['horizon_seconds'])
            if r.get('status')=='ok' and r.get('lateness_seconds',99)<=1:
                markouts[key]=r
    fill_counts=Counter()
    for r in rows:
        inst=r.get('instrument')
        kind=r.get('type')
        if kind=='quote':
            latest_quote[inst]=q_by_observed.get((inst,r['observed_at_utc']))
        elif kind=='order_response' and r.get('success'):
            q=latest_quote.get(inst)
            if q and abs(r['price']-q[r['side']+'_price'])<1e-7 and 0<=ts(r['observed_at_utc'])-q['t']<=2:
                orders[(inst,r['order_id'])]=(r,q)
        elif kind=='fill':
            fill_counts['total']+=1
            found=orders.get((inst,r['order_id']))
            if not found:
                fill_counts['unmapped_order']+=1; continue
            response,q=found
            trade_time=ts(r['trade_timestamp'])
            # The quote signal must predate execution, not just the fill report.
            if q['t']>trade_time:
                fill_counts['quote_after_execution']+=1; continue
            if q['reason']!='ok':
                fill_counts['signal_unavailable']+=1; continue
            reduced=min(response['volume'],q['candidate_'+r['side']])<response['volume']
            f=dict(run=run,version=version,instrument=inst,trade_id=r['trade_id'],order_id=r['order_id'],
                t=trade_time,side=r['side'],volume=r['volume'],price=r['price'],
                original_order_volume=response['volume'],candidate_order_volume=min(response['volume'],q['candidate_'+r['side']]),
                affected=bool(reduced),signal=q['signal'],signal_age_at_fill=trade_time-q['t'])
            for h in (5,15):
                m=markouts.get((inst,r['trade_id'],h))
                f['mark'+str(h)]=m['per_share'] if m else np.nan
                if m:
                    expected=(m['mid']-r['price'])*(1 if r['side']=='bid' else -1)
                    assert abs(expected-m['per_share'])<1e-7
            fill_rows.append(f)
            fill_counts['supported']+=1
    results['runs'].append(dict(run=run,version=version,quotes=len(quotes),quote_statuses=dict(counters),fills=dict(fill_counts)))

qd=pd.DataFrame(quote_rows)
# Mark quote price relative to later actual mids. This is not a fill model.
for h in (5,15):
    qd['future_mid'+str(h)]=np.nan
    for key,ids in qd[qd.session.notna()].groupby(['session','instrument']).groups.items():
        arr=price_arrays[key]; time=arr[:,0]
        now=qd.loc[ids,'t'].to_numpy()
        j=np.searchsorted(time,now+h)
        safe=np.minimum(j,len(arr)-1)
        valid=(j<len(arr))&(time[safe]-now-h<=1)
        # Reject observation gaps only; never filter future adverse book quality.
        seg=np.r_[0,np.cumsum(np.diff(time)>2)]
        start=np.searchsorted(time,now,side='right')-1
        valid&=(start>=0)&(seg[np.maximum(start,0)]==seg[safe])
        qd.loc[ids,'future_mid'+str(h)]=np.where(valid,arr[safe,1],np.nan)
for (run,inst),d in qd.groupby(['run','instrument']):
    supported=d[d.reason=='ok']
    base=float((d.base_bid+d.base_ask).sum())
    candidate=float((d.candidate_bid+d.candidate_ask).sum())
    row=dict(run=run,version=d.version.iloc[0],instrument=inst,quotes=len(d),
        supported_quotes=len(supported),supported_fraction=len(supported)/len(d),
        affected_quotes=int(((d.base_bid!=d.candidate_bid)|(d.base_ask!=d.candidate_ask)).sum()),
        base_target_lot_sum=base,candidate_target_lot_sum=candidate,
        target_lot_reduction_fraction=(base-candidate)/base if base else 0,
        changed_bid_quotes=int((d.base_bid!=d.candidate_bid).sum()),
        changed_ask_quotes=int((d.base_ask!=d.candidate_ask).sum()),horizons={})
    for h in (5,15):
        z=d[d['future_mid'+str(h)].notna() & (d.reason=='ok')]
        bidmark=z['future_mid'+str(h)]-z.bid_price
        askmark=z.ask_price-z['future_mid'+str(h)]
        removed_bid=z.base_bid-z.candidate_bid
        removed_ask=z.base_ask-z.candidate_ask
        removed=float((removed_bid+removed_ask).sum())
        def weighted(b,a):
            denom=float((b+a).sum())
            return float((b*bidmark+a*askmark).sum()/denom) if denom else None
        row['horizons'][str(h)]=dict(valid_quotes=len(z),removed_target_lots=removed,
            original_quote_mark=weighted(z.base_bid,z.base_ask),
            candidate_quote_mark=weighted(z.candidate_bid,z.candidate_ask),
            removed_quote_mark=weighted(removed_bid,removed_ask))
    results['quote_outcomes'].append(row)
fd=pd.DataFrame(fill_rows)
for (run,inst),d in fd.groupby(['run','instrument']):
    for h in (5,15):
        z=d[d['mark'+str(h)].notna()]
        for label,part in [('all_supported',z),('affected',z[z.affected]),('unchanged',z[~z.affected])]:
            volume=int(part.volume.sum())
            results['fill_outcomes'].append(dict(run=run,version=d.version.iloc[0],instrument=inst,
                horizon=h,group=label,fill_count=len(part),volume=volume,
                mark_per_share=float((part['mark'+str(h)]*part.volume).sum()/volume) if volume else None,
                mark_total=float((part['mark'+str(h)]*part.volume).sum()),
                negative_volume_fraction=float(part.loc[part['mark'+str(h)]<0,'volume'].sum()/volume) if volume else None,
                median_signal_age=float(part.signal_age_at_fill.median()) if len(part) else None))
qd.to_json(OUT/'quote_comparison.jsonl',orient='records',lines=True)
fd.to_json(OUT/'matched_fills.jsonl',orient='records',lines=True)
(OUT/'metrics.json').write_text(json.dumps(results,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
print(json.dumps({k:v for k,v in results.items() if k not in ('source_logs','assumptions','quote_outcomes','fill_outcomes')},indent=2))
print('CURRENT QUOTES',json.dumps([r for r in results['quote_outcomes'] if r['version']==results['current_version']],indent=2))
print('CURRENT FILLS',json.dumps([r for r in results['fill_outcomes'] if r['version']==results['current_version']],indent=2))
