"""Frozen-history exit quote study plus mechanical inventory delay sensitivity.

Later price availability is not a fill simulation or a return forecast.
"""
from collections import Counter
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import numpy as np
import pandas as pd
from guard import apply,SETTINGS
from attribution import fill_impact

ROOT=Path(__file__).resolve().parents[2]
OUT=Path(__file__).resolve().parent
CURRENT='baseline_cycle_risk_only_v2'


def ts(s):
    d=datetime.fromisoformat(s.replace('Z','+00:00'))
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def describe(values):
    v=np.asarray(values,dtype=float)
    v=v[np.isfinite(v)]
    return dict(n=len(v),mean=float(v.mean()),median=float(np.median(v)),
        p95=float(np.quantile(v,.95)),max=float(v.max())) if len(v) else {'n':0}


def inventory_delay(fills,first_position,affected,seconds):
    events=[]
    for row in fills:
        sign=1 if row['side']=='bid' else -1
        now=ts(row['trade_timestamp'])
        events.append((now+(seconds if row['trade_id'] in affected else 0),row['trade_id'],sign*row['volume']))
    events.sort()
    q=first_position; peak=abs(q); breaches=0
    for _,_,delta in events:
        q+=delta; peak=max(peak,abs(q)); breaches+=abs(q)>100
    baseline_last=first_position+sum((1 if r['side']=='bid' else -1)*r['volume'] for r in fills)
    assert q==baseline_last
    return dict(delay_seconds=seconds,peak_abs_inventory=peak,events_outside_100=breaches,final_position=q)


price_sessions={}
keys={}
for path in sorted((ROOT/'data/market').glob('*/prices.csv')):
    df=pd.read_csv(path)
    df=df[df.instrument_id=='PHILIPS_B'].copy()
    df['t']=df.observed_at_utc.map(ts)
    df['stamp']=df.book_timestamp.map(ts)
    price_sessions[path.parent.name]=df.sort_values('t').reset_index(drop=True)
    for row in df.itertuples():
        keys.setdefault(row.stamp,[]).append((row.t,path.parent.name))
analysis=json.loads((ROOT/'analysis/b_loss_review_20260910/metrics.json').read_text(encoding='utf-8'))
derived=pd.read_json(ROOT/'analysis/b_loss_review_20260910/fills.jsonl',lines=True)
hash_before={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
             for p in list((ROOT/'strategies/baseline').glob('*.py'))+list((ROOT/'strategies/baseline').glob('*.ipynb'))+[ROOT/'order_execution.py']}
result=dict(settings=SETTINGS,current_version=CURRENT,runs=[],opportunity=[],inventory_stress=[],input_strategy_sha256=hash_before,
    caveats=['Historical prices, entries, sizes, position and cycle path frozen.',
      'Threshold 10 is an uncalibrated small-position experiment; 3 ticks comes from existing entry protection.',
      'Order-origin classification; later repricing and order queue are not simulated.',
      'Trade-at-price and displayed crossing are observed opportunities, not confirmed fills.',
      'Displayed best quotes are aggregate historical data and may include own orders; they are not certified external executable liquidity.',
      'A fixed candidate limit is inspected after the old exit; dynamic requoting, cancellation, latency and queue are not replayed.',
      'Delay stress moves historical exit quantities in time, preserves historical other fills, ignores altered execution and prices.'])
qrows=[]; frows=[]
for info in analysis['runs']:
    path=ROOT/info['source']
    assert hashlib.sha256(path.read_bytes()).hexdigest()==info['sha256'],'source changed; rerun attribution'
    rows=[json.loads(x) for x in path.open(encoding='utf-8') if x.strip()]
    latest=None; orders={}; quotes=[]
    run=info['run']; version=info['version']
    for r in rows:
        if r.get('instrument')!='PHILIPS_B': continue
        if r.get('type')=='quote':
            c=apply(r)
            for key in ('position','buy_volume','sell_volume','center','fair_value'):
                assert c[key]==r[key]
            assert c['bid_price']<=r['bid_price']+1e-8 and c['ask_price']>=r['ask_price']-1e-8
            side='ask' if r['position']>0 else 'bid' if r['position']<0 else None
            changed=bool(side and abs(c[side+'_price']-r[side+'_price'])>1e-7)
            if changed:
                assert c['cycle']['active'] and abs(c['position'])<=10
            qr=dict(run=run,version=version,t=ts(r['observed_at_utc']),position=r['position'],changed=changed,
                side=side,old_price=r[side+'_price'] if side else None,new_price=c[side+'_price'] if side else None,
                change_ticks=abs(c[side+'_price']-r[side+'_price'])/.1 if side else 0)
            quotes.append(qr); qrows.append(qr); latest=(r,c,qr)
        if r.get('type')=='order_response' and r.get('success') and latest:
            q,c,qr=latest
            if abs(r['price']-q[r['side']+'_price'])<1e-7 and 0<=ts(r['observed_at_utc'])-ts(q['observed_at_utc'])<=2:
                orders[r['order_id']]=(r,q,c,qr)
    original_fills=[r for r in rows if r.get('type')=='fill' and r.get('instrument')=='PHILIPS_B']
    df=derived[derived.run==run].set_index('trade_id')
    affected=set()
    fill_status=Counter()
    run_rows=[]
    for f in original_fills:
        found=orders.get(f['order_id'])
        if not found: fill_status['no_order_match']+=1; continue
        response,q,c,qr=found
        t=ts(f['trade_timestamp']); side=f['side']; sign=1 if side=='bid' else -1
        if ts(q['observed_at_utc'])>t: fill_status['quote_after_execution']+=1;continue
        before=int(df.loc[f['trade_id'],'position_before'])
        is_reduce=before*sign<0
        impact=fill_impact(side,q[side+'_price'],c[side+'_price'],f['price'],before,f['volume'])
        changed=impact['changed']
        # Only an actual reducing fill corresponding to a reducing original order.
        if impact['repriced'] and not impact['fully_reducing']:
            fill_status['repriced_order_but_fill_not_fully_reducing']+=1
        if impact['repriced'] and not impact['original_execution_blocked']:
            fill_status['repriced_but_original_execution_still_allowed']+=1
        if changed:
            affected.add(f['trade_id'])
        entry=dict(run=run,version=version,trade_id=f['trade_id'],order_id=f['order_id'],t=t,
            side=side,volume=f['volume'],position_before=before,is_reduce=is_reduce,**impact,
            old_price=f['price'],old_limit=q[side+'_price'],new_price=c[side+'_price'],quote_t=ts(q['observed_at_utc']),
            price_change_ticks=abs(c[side+'_price']-q[side+'_price'])/.1,
            mark5=df.loc[f['trade_id'],'mark5'],mark15=df.loc[f['trade_id'],'mark15'])
        candidates=[(ot,s) for ot,s in keys.get(ts(q['book_timestamp']),[]) if ot<=entry['quote_t'] and entry['quote_t']-ot<=2]
        entry['session']=max(candidates)[1] if candidates else None
        fill_status['matched']+=1
        run_rows.append(entry);frows.append(entry)
    d=pd.DataFrame(run_rows)
    fill_status['changed_without_market_session']=sum(r['changed'] and not r['session'] for r in run_rows)
    summary=dict(run=run,version=version,source=info['source'],source_sha256=info['sha256'],quotes=len(quotes),
        changed_quotes=sum(q['changed'] for q in quotes),changed_quote_ticks=describe([q['change_ticks'] for q in quotes if q['changed']]),
        fills=len(original_fills),fill_status=dict(fill_status),groups=[])
    summary['actual_reducing_volume']=int(d.reducing_volume.sum())
    summary['partial_zero_crossing_fills']=int((d.is_reduce&~d.fully_reducing).sum())
    for label,z in [('all_fully_reducing',d[d.fully_reducing]),('changed',d[d.changed]),('unchanged_fully_reducing',d[d.fully_reducing&~d.changed])]:
        item=dict(group=label,fills=len(z),volume=int(z.volume.sum()),price_change_ticks=describe(z.price_change_ticks),horizons={})
        for h in (5,15):
            x=z[z['mark'+str(h)].notna()]
            item['horizons'][str(h)]=dict(fills=len(x),volume=int(x.volume.sum()),
                mark_per_share=float(np.average(x['mark'+str(h)],weights=x.volume)) if len(x) else None)
        summary['groups'].append(item)
    result['runs'].append(summary)
    for delay in (0,1,3,5):
        result['inventory_stress'].append(dict(run=run,version=version,**inventory_delay(original_fills,info['first_position'],affected,delay)))

# Inspect future price availability only after the original affected exit occurred.
for f in frows:
    if not f['changed'] or not f['session']: continue
    df=price_sessions[f['session']]
    tradepath=ROOT/'data/market'/f['session']/'trades.csv'
    # cached reads
    if '_trades' not in df.attrs:
        trades=pd.read_csv(tradepath)
        trades=trades[trades.instrument_id=='PHILIPS_B'].copy()
        trades['t']=trades.trade_timestamp.map(ts)
        df.attrs['_trades']=trades
    trades=df.attrs['_trades']
    for h in (1,3,5,15):
        now=f['t'];end=now+h
        x=df[(df.t>now)&(df.t<=end)]
        endpoint=df[df.t>=end].head(1)
        prefix=df[df.t<=now].tail(1)
        # Require complete window and no >2s observation gaps, without quality-selecting future prices.
        times=np.r_[prefix.t.to_numpy(),x.t.to_numpy(),endpoint.t.to_numpy()]
        covered=bool(len(prefix) and len(endpoint) and endpoint.t.iloc[0]-end<=1 and len(x) and np.max(np.diff(times))<=2)
        row=dict(run=f['run'],version=f['version'],trade_id=f['trade_id'],volume=f['volume'],horizon=h,covered=covered,
            new_price=f['new_price'],old_price=f['old_price'])
        if covered:
            if f['side']=='ask':
                crosses=(x.best_bid>=f['new_price']-1e-8)&(x.best_bid_volume>=f['volume'])
                relevant=trades[(trades.t>now)&(trades.t<=end)&(trades.aggressor_side=='bid')&(trades.price>=f['new_price']-1e-8)]
                adverse=np.maximum(0,f['old_price']-x.best_bid)
                end_change=endpoint.best_bid.iloc[0]-f['old_price']
            else:
                crosses=(x.best_ask<=f['new_price']+1e-8)&(x.best_ask_volume>=f['volume'])
                relevant=trades[(trades.t>now)&(trades.t<=end)&(trades.aggressor_side=='ask')&(trades.price<=f['new_price']+1e-8)]
                adverse=np.maximum(0,x.best_ask-f['old_price'])
                end_change=f['old_price']-endpoint.best_ask.iloc[0]
            row.update(displayed_cross=bool(crosses.any()),same_direction_trade=bool(len(relevant)),
                first_cross_seconds=float(x.loc[crosses,'t'].iloc[0]-now) if crosses.any() else None,
                max_adverse_per_share=float(adverse.max()),end_opposite_quote_change=float(end_change))
        result['opportunity'].append(row)

result['opportunity_summary']=[]
op=pd.DataFrame(result['opportunity'])
for (run,h),d in op.groupby(['run','horizon']):
    z=d[d.covered];volume=z.volume.sum()
    result['opportunity_summary'].append(dict(run=run,version=d.version.iloc[0],horizon=int(h),events=len(d),covered=len(z),
        covered_volume=int(volume),displayed_cross_events=int(z.displayed_cross.sum()),
        displayed_cross_fraction=float(z.displayed_cross.mean()) if len(z) else None,
        displayed_cross_volume_fraction=float(z.loc[z.displayed_cross.eq(True),'volume'].sum()/volume) if volume else None,
        trade_price_reached_fraction=float(z.same_direction_trade.mean()) if len(z) else None,
        max_adverse=describe(z.max_adverse_per_share),first_cross_seconds=describe(z.first_cross_seconds),
        end_opposite_quote_change=describe(z.end_opposite_quote_change)))
for file,sha in hash_before.items(): assert hashlib.sha256((ROOT/file).read_bytes()).hexdigest()==sha
result['strategy_files_unchanged']=True
pd.DataFrame(qrows).to_json(OUT/'quotes.jsonl',orient='records',lines=True)
pd.DataFrame(frows).to_json(OUT/'fills.jsonl',orient='records',lines=True)

def clean(o):
    if isinstance(o,dict):return {str(k):clean(v) for k,v in o.items()}
    if isinstance(o,list):return [clean(v) for v in o]
    if isinstance(o,np.generic):o=o.item()
    if isinstance(o,float) and not np.isfinite(o):return None
    return o

(OUT/'metrics.json').write_text(json.dumps(clean(result),ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
print(json.dumps(clean({k:[r for r in result[k] if r.get('version')==CURRENT] for k in ['runs','opportunity_summary','inventory_stress']}),indent=2))
