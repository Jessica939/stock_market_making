"""Offline attribution of actual B fills and completed inventory excursions.

No exchange imports. Markouts are valuation diagnostics, not realized profit.
FIFO is used only as an explicit accounting allocation within recorded inventory.
"""
from collections import defaultdict, deque
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[2]
OUT=Path(__file__).resolve().parent
CURRENT='baseline_cycle_risk_only_v2'


def stamp(s):
    d=datetime.fromisoformat(s.replace('Z','+00:00'))
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def describe(values):
    a=pd.Series(values,dtype=float).dropna()
    if not len(a): return {'n':0}
    return dict(n=len(a),mean=float(a.mean()),median=float(a.median()),
                p10=float(a.quantile(.1)),p90=float(a.quantile(.9)),min=float(a.min()),max=float(a.max()))


def bucket(q):
    v=abs(q)
    return '0' if v==0 else '1-5' if v<=5 else '6-20' if v<=20 else '21-50' if v<=50 else '51-100'


def cycle_fields(q):
    cy=q.get('cycle',{}) if q else {}
    active=bool(cy.get('active'))
    pred=cy.get('predicted_B_change') if active else None
    phase='relative_up' if pred is not None and pred>1e-8 else 'relative_down' if pred is not None and pred< -1e-8 else 'relative_flat' if active else 'inactive'
    return dict(cycle_active=active,cycle_phase=phase,cycle_reason=cy.get('reason','not_logged'),
                cycle_prediction=pred,cycle_weight=cy.get('fit_weight'),cycle_risk_shift=q.get('cycle_risk_shift',q.get('cycle_shift')) if q else None)


all_fills=[]
all_parts=[]
all_lots=[]
all_episodes=[]
all_quotes=[]
result={'current_version':CURRENT,'runs':[],'groups':[],'episode_groups':[],
        'scope':'Actual baseline B fills on September 10; versions analyzed separately.',
        'method':'Order-origin quote features; actual pre-fill inventory; FIFO lot allocation; direct excursion cashflows.'}
for path in sorted((ROOT/'data/runs/baseline').glob('*/*.jsonl')):
    rows=[json.loads(x) for x in path.open(encoding='utf-8') if x.strip()]
    fills=[r for r in rows if r.get('type')=='fill' and r.get('instrument')=='PHILIPS_B' and r['observed_at_utc'].startswith('2026-09-10')]
    if not fills: continue
    settings=next((r for r in rows if r.get('action')=='settings'),{})
    version=settings.get('strategy_version','unversioned')
    run=path.parent.name
    snaps=[r for r in rows if r.get('type')=='snapshot' and r.get('instrument')=='PHILIPS_B']
    first,last=snaps[0],snaps[-1]
    qpos=first['position']
    origin=stamp(first['observed_at_utc'])
    markouts={}
    for r in rows:
        if r.get('type')=='markout' and r.get('instrument')=='PHILIPS_B':
            if r.get('status')=='ok' and r.get('lateness_seconds',999)<=1:
                markouts[(r['trade_id'],r['horizon_seconds'])]=r
    latest=None
    orders={}
    quotes=[]
    for r in rows:
        if r.get('type')=='quote' and r.get('instrument')=='PHILIPS_B':
            latest=r
            quotes.append(r)
            for side,key in [('bid','buy_volume'),('ask','sell_volume')]:
                vol=r[key]
                reducing=min(vol,max(0,-r['position'] if side=='bid' else r['position']))
                for role,quantity in [('reduce',reducing),('increase',vol-reducing)]:
                    if not quantity: continue
                    mid=(r['best_bid']+r['best_ask'])/2
                    all_quotes.append(dict(run=run,version=version,t=stamp(r['observed_at_utc']),side=side,role=role,
                        volume=quantity,price=r[side+'_price'],position=r['position'],
                        quote_edge=(mid-r[side+'_price'])*(1 if side=='bid' else -1),
                        center_distance=(r['center']-r[side+'_price'])*(1 if side=='bid' else -1),**cycle_fields(r)))
        if r.get('type')=='order_response' and r.get('instrument')=='PHILIPS_B' and r.get('success') and latest:
            if (abs(r['price']-latest[r['side']+'_price'])<1e-7
                    and 0<=stamp(r['observed_at_utc'])-stamp(latest['observed_at_utc'])<=2):
                orders[r['order_id']]=(r,latest)
    assert len({r['trade_id'] for r in fills})==len(fills)
    fills.sort(key=lambda r:(stamp(r['trade_timestamp']),r['trade_id']))
    ledger=deque()
    if qpos:
        ledger.append(dict(sign=1 if qpos>0 else -1,volume=abs(qpos),price=first['mid'],t=origin,
                           trade_id=None,inherited=True,cycle_phase='inherited',quote_edge=None))
    cashflow=0.
    realized=0.
    peak_abs=abs(qpos)
    episode=None
    run_episodes=[]
    run_parts=[]
    unmatched=0
    for r in fills:
        t=stamp(r['trade_timestamp'])
        side=r['side']; sign=1 if side=='bid' else -1
        volume=r['volume']; price=r['price']
        before=qpos
        found=orders.get(r['order_id'])
        quote=found[1] if found and stamp(found[1]['observed_at_utc'])<=t else None
        if quote is None: unmatched+=1
        quote_mid=(quote['best_bid']+quote['best_ask'])/2 if quote else None
        cf=cycle_fields(quote)
        qedge=(quote_mid-price)*sign if quote else None
        pred=cf['cycle_prediction']
        alignment='aligned' if pred is not None and pred*sign>1e-8 else 'opposed' if pred is not None and pred*sign< -1e-8 else 'inactive_or_flat'
        base=dict(run=run,version=version,trade_id=r['trade_id'],order_id=r['order_id'],t=t,
            side=side,price=price,fill_volume=volume,position_before=before,position_after=before+sign*volume,
            inventory_bucket=bucket(before),quote_position=quote.get('position') if quote else None,
            quote_edge=qedge,quote_spread=quote['best_ask']-quote['best_bid'] if quote else None,
            center_distance=(quote['center']-price)*sign if quote else None,
            quote_age=t-stamp(quote['observed_at_utc']) if quote else None,
            order_quote_mapped=quote is not None,cycle_alignment=alignment,
            risk_signal_adverse=bool(cf['cycle_risk_shift'] is not None and sign*cf['cycle_risk_shift']<=-.05+1e-9),
            quote_regime='first_180s' if quote and stamp(quote['observed_at_utc'])-origin<180 else 'after_180s' if quote else 'unmapped',
            **cf)
        for h in (1,3,5,15,30,60):
            m=markouts.get((r['trade_id'],h))
            value=(m['mid']-price)*sign if m else None
            if m: assert abs(value-m['per_share'])<1e-7
            base['mark'+str(h)]=value
        all_fills.append(base)
        reducing=min(volume,abs(before)) if before*sign<0 else 0
        for role,qty in [('reduce',reducing),('increase',volume-reducing)]:
            if not qty: continue
            part={**base,'role':role,'volume':qty}
            all_parts.append(part); run_parts.append(part)
        remaining=volume
        while remaining:
            if qpos==0:
                episode=dict(run=run,version=version,start=t,entry_trade_id=r['trade_id'],
                    direction='long' if sign==1 else 'short',cashflow=0.,volume=0,peak_abs_position=0,
                    entry_cycle_phase=cf['cycle_phase'],entry_cycle_alignment=alignment,
                    entry_quote_regime=base['quote_regime'],fill_ids=set())
            closing=min(remaining,abs(qpos)) if qpos*sign<0 else 0
            qty=closing if closing else remaining
            if closing:
                need=qty
                while need:
                    entry=ledger[0]
                    take=min(need,entry['volume'])
                    pnl=(price-entry['price'])*entry['sign']*take
                    realized+=pnl
                    all_lots.append(dict(run=run,version=version,entry_trade_id=entry['trade_id'],exit_trade_id=r['trade_id'],
                        volume=take,pnl=pnl,per_share=pnl/take,hold_seconds=t-entry['t'],
                        direction='long' if entry['sign']==1 else 'short',inherited=entry['inherited'],
                        entry_cycle_phase=entry['cycle_phase'],exit_cycle_phase=cf['cycle_phase'],
                        entry_quote_edge=entry['quote_edge'],exit_quote_edge=qedge))
                    need-=take; entry['volume']-=take
                    if not entry['volume']: ledger.popleft()
            else:
                ledger.append(dict(sign=sign,volume=qty,price=price,t=t,trade_id=r['trade_id'],
                    inherited=False,cycle_phase=cf['cycle_phase'],quote_edge=qedge))
            qpos+=sign*qty
            flow=-sign*price*qty
            cashflow+=flow
            peak_abs=max(peak_abs,abs(qpos))
            if episode:
                episode['cashflow']+=flow
                episode['volume']+=qty
                episode['peak_abs_position']=max(episode['peak_abs_position'],abs(qpos))
                episode['fill_ids'].add(r['trade_id'])
                if qpos==0:
                    episode.update(end=t,hold_seconds=t-episode['start'],pnl=episode['cashflow'],
                        fill_count=len(episode['fill_ids']),exit_trade_id=r['trade_id'],exit_cycle_phase=cf['cycle_phase'])
                    del episode['fill_ids']; del episode['cashflow']
                    run_episodes.append(episode); all_episodes.append(episode)
                    episode=None
            remaining-=qty
        assert qpos==before+sign*volume
    unrealized=sum((last['mid']-entry['price'])*entry['sign']*entry['volume'] for entry in ledger)
    delta=last['pnl_mid']-first['pnl_mid']
    direct=sum((-1 if r['side']=='bid' else 1)*r['price']*r['volume'] for r in fills)
    assert qpos==last['position']
    assert abs(direct-(last['cash']-first['cash']))<1e-6
    assert abs(direct+last['position']*last['mid']-first['position']*first['mid']-delta)<1e-6
    assert abs(realized+unrealized-delta)<1e-6
    if first['position']==last['position']==0:
        assert abs(sum(e['pnl'] for e in run_episodes)-delta)<1e-6
    snap_t=np.array([stamp(s['observed_at_utc']) for s in snaps])
    snap_q=np.array([s['position'] for s in snaps])
    dt=np.diff(snap_t)
    valid=(dt>=0)&(dt<=2)
    obs_seconds=float(dt[valid].sum())
    occupancy={str(k):float(dt[valid&(abs(snap_q[:-1])>=k)].sum()/obs_seconds) for k in (1,5,20,50,70,100)}
    duration=stamp(last['observed_at_utc'])-origin
    rr=dict(run=run,version=version,source=str(path.relative_to(ROOT)),sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        start=first['observed_at_utc'],end=last['observed_at_utc'],seconds=duration,session_end_present=any(r.get('type')=='session_end' for r in rows),
        first_position=first['position'],last_position=last['position'],fill_count=len(fills),
        fill_volume=sum(r['volume'] for r in fills),buy_volume=sum(r['volume'] for r in fills if r['side']=='bid'),
        sell_volume=sum(r['volume'] for r in fills if r['side']=='ask'),cash_change=direct,pnl_mid_change=delta,
        fifo_realized_from_start_mark=realized,fifo_unrealized=unrealized,peak_abs_fill_position=peak_abs,
        quote_unmapped_fills=unmatched,occupancy_abs_position_at_least=occupancy,observed_seconds=obs_seconds,
        completed_excursions=len(run_episodes),excursion_pnl=sum(e['pnl'] for e in run_episodes),
        excursion_hold=describe([e['hold_seconds'] for e in run_episodes]),
        excursion_win_fraction=float(np.mean([e['pnl']>1e-8 for e in run_episodes])) if run_episodes else None,
        worst_excursions=sorted(run_episodes,key=lambda e:e['pnl'])[:8])
    result['runs'].append(rr)

parts=pd.DataFrame(all_parts)
fills=pd.DataFrame(all_fills)
episodes=pd.DataFrame(all_episodes)
lots=pd.DataFrame(all_lots)
quotes=pd.DataFrame(all_quotes)
dimensions=[['role'],['side'],['role','side'],['role','cycle_phase'],['role','cycle_alignment'],
    ['role','inventory_bucket'],['role','quote_regime'],['role','risk_signal_adverse']]
for run,d in parts.groupby('run'):
    for columns in dimensions:
        for keys,z in d.groupby(columns,dropna=False):
            if not isinstance(keys,tuple): keys=(keys,)
            base=dict(run=run,version=z.version.iloc[0],dimension='/'.join(columns),
                labels={k:(v.item() if isinstance(v,np.generic) else v) for k,v in zip(columns,keys)},
                fills=int(z.trade_id.nunique()),volume=int(z.volume.sum()),
                quote_edge_volume_weighted=float(np.average(z.quote_edge.dropna(),weights=z.loc[z.quote_edge.notna(),'volume'])) if z.quote_edge.notna().any() else None,
                quote_center_distance=describe(z.center_distance),quote_age=describe(z.quote_age),horizons={})
            for h in (1,3,5,15,30,60):
                x=z[z['mark'+str(h)].notna()]
                v=int(x.volume.sum())
                total=float((x.volume*x['mark'+str(h)]).sum())
                base['horizons'][str(h)]=dict(fills=int(x.trade_id.nunique()),volume=v,coverage=v/z.volume.sum(),
                    mark_total=total,mark_per_share=total/v if v else None,
                    negative_volume_fraction=float(x.loc[x['mark'+str(h)]<0,'volume'].sum()/v) if v else None)
            result['groups'].append(base)
    for dim in ('direction','entry_cycle_phase','entry_cycle_alignment','entry_quote_regime'):
        es=episodes[episodes.run==run]
        for label,z in es.groupby(dim):
            result['episode_groups'].append(dict(run=run,version=z.version.iloc[0],dimension=dim,label=label,
                episodes=len(z),pnl=float(z.pnl.sum()),hold=describe(z.hold_seconds),win_fraction=float((z.pnl>1e-8).mean())))
result['lot_hold_groups']=[]
lots['hold_bucket']=pd.cut(lots.hold_seconds,[-1e-9,1,2,5,15,np.inf],labels=['0-1s','1-2s','2-5s','5-15s','>15s'])
for (run,b),d in lots[~lots.inherited].groupby(['run','hold_bucket'],observed=True):
    result['lot_hold_groups'].append(dict(run=run,hold_bucket=str(b),volume=int(d.volume.sum()),pnl=float(d.pnl.sum()),
        entry_orders=int(d.entry_trade_id.nunique())))
result['quote_summary']=[]
for (run,role),d in quotes.groupby(['run','role']):
    result['quote_summary'].append(dict(run=run,role=role,quote_sides=len(d),target_volume_sum=int(d.volume.sum()),
        quote_edge=describe(d.quote_edge),center_distance=describe(d.center_distance)))
fills.to_json(OUT/'fills.jsonl',orient='records',lines=True)
parts.to_json(OUT/'fill_parts.jsonl',orient='records',lines=True)
lots.to_json(OUT/'fifo_matches.jsonl',orient='records',lines=True)
episodes.to_json(OUT/'excursions.jsonl',orient='records',lines=True)
(OUT/'metrics.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
print('RUNS',json.dumps([{k:v for k,v in r.items() if k!='worst_excursions'} for r in result['runs']],indent=2))
print('CURRENT GROUPS',json.dumps([g for g in result['groups'] if g['version']==CURRENT and g['dimension'] in ('role','role/side','role/cycle_phase')],indent=2))
