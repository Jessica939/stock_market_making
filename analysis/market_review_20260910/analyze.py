"""Offline market analysis. Does not import/connect to the exchange or change inputs."""
import gzip
import json
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent

def stats(x):
    x = pd.Series(x).replace([np.inf, -np.inf], np.nan).dropna()
    return dict(n=len(x), mean=float(x.mean()), median=float(x.median()),
                p05=float(x.quantile(.05)), p95=float(x.quantile(.95)),
                min=float(x.min()), max=float(x.max())) if len(x) else {'n': 0}

def corr(x, y):
    d = pd.DataFrame({'x': np.asarray(x), 'y': np.asarray(y)}).dropna()
    return float(d.x.corr(d.y)) if len(d)>10 and d.x.std()>1e-9 and d.y.std()>1e-9 else None

def epoch(s):
    return pd.to_datetime(s, utc=True, format='mixed').astype('int64') / 1e9

prices, trades, books, issues = [], [], [], []
counts = Counter()
level_sizes = Counter()
for folder in sorted((ROOT/'data/market').iterdir()):
    if not (folder/'prices.csv').exists():
        continue
    p = pd.read_csv(folder/'prices.csv')
    p['session'] = folder.name
    p['t'] = epoch(p.observed_at_utc)
    p['age'] = p.t-epoch(p.book_timestamp)
    prices.append(p)
    tr = pd.read_csv(folder/'trades.csv')
    tr['session'] = folder.name
    trades.append(tr)
    for path in sorted(folder.glob('orderbooks*.gz')):
        try:
            with gzip.open(path, 'rt', encoding='utf-8') as f:
                for line in f:
                    r = json.loads(line)
                    b, a = r.get('bids') or [], r.get('asks') or []
                    row = dict(session=folder.name, sample_id=r['sample_id'], instrument_id=r['instrument_id'])
                    if not b or not a:
                        books.append(row)
                        continue
                    for side, levels in [('bid', b), ('ask', a)]:
                        ps, vs = np.array(levels, dtype=float).T
                        row[side+'_book_top'] = float(ps[0])
                        near = np.abs(ps-ps[0]) <= .40000001
                        row[side+'_depth'] = float(vs.sum())
                        row[side+'_near'] = float(vs[near].sum())
                        row[side+'_cap'] = float(np.minimum(vs[near], 100).sum())
                        row[side+'_levels'] = len(levels)
                        row[side+'_wall'] = bool((vs>=15000).any())
                        row[side+'_wall_touch'] = bool(vs[0]>=15000)
                        row[side+'_wall_share'] = float(vs[vs>=15000].sum()/vs.sum())
                        wall_idx = np.where(vs>=15000)[0]
                        row[side+'_wall_distance'] = float(abs(ps[wall_idx[0]]-ps[0])) if len(wall_idx) else np.nan
                        row[side+'_wall_price'] = float(ps[wall_idx[0]]) if len(wall_idx) else np.nan
                        row[side+'_gap2'] = float(abs(ps[1]-ps[0])) if len(ps)>1 else np.nan
                        for v in vs:
                            level_sizes[(r['instrument_id'], int(v))] += 1
                        for q in (10, 50, 100):
                            remaining, cost = q, 0.
                            for pr, vo in levels:
                                take = min(remaining, vo)
                                cost += take*pr
                                remaining -= take
                                if remaining==0:
                                    break
                            row[f'{side}_slip{q}'] = abs(cost/q-ps[0]) if remaining==0 else np.nan
                    books.append(row)
        except (EOFError, json.JSONDecodeError) as e:
            issues.append(dict(path=str(path.relative_to(ROOT)), error=str(e)))

p = pd.concat(prices, ignore_index=True)
b = pd.DataFrame(books)
assert not p.duplicated(['session','sample_id','instrument_id']).any()
assert not b.duplicated(['session','sample_id','instrument_id']).any()
p = p.merge(b, on=['session','sample_id','instrument_id'], how='left', validate='one_to_one')
for side in ('bid','ask'):
    assert np.allclose(p.loc[p[side+'_book_top'].notna(),'best_'+side],p.loc[p[side+'_book_top'].notna(),side+'_book_top'])
p['day'] = p.observed_at_utc.str[:10]
p['good'] = ((p.status=='ok') & (p.spread>0) & (p.spread<=2.00000001)
             & p.age.between(-.5,2) & (p.best_bid_volume<15000) & (p.best_ask_volume<15000))
for name, bc, ac in [('top','best_bid_volume','best_ask_volume'),
                     ('all','bid_depth','ask_depth'),('near','bid_near','ask_near'),('cap','bid_cap','ask_cap')]:
    p['imb_'+name] = (p[bc]-p[ac])/(p[bc]+p[ac])

tr = pd.concat(trades, ignore_index=True)
trade_before = len(tr)
tr = tr.drop_duplicates(['instrument_id','trade_timestamp','trade_id'])
tr['day'] = tr.trade_timestamp.str[:10]
tr['t'] = epoch(tr.trade_timestamp)
result = dict(source='data/market/*/{prices.csv,trades.csv,orderbooks*.jsonl.gz}',
              sessions=p.session.nunique(), price_rows=len(p), book_rows=len(b),
              unique_trades=len(tr), duplicate_trades_removed=trade_before-len(tr), issues=issues,
              buyer_nonempty=int(tr.buyer.notna().sum()), seller_nonempty=int(tr.seller.notna().sum()),
              statuses=p.status.value_counts().to_dict(), days={}, sessions_detail=[], signals=[], cycles=[])

for (day, inst), d in p.groupby(['day','instrument_id']):
    td = tr[(tr.day==day)&(tr.instrument_id==inst)]
    vol = td.volume.sum()
    result['days'].setdefault(day, {})[inst] = dict(
        rows=len(d), good_fraction=float(d.good.mean()), mid=stats(d.mid), spread=stats(d.spread),
        bid_volume=stats(d.best_bid_volume), ask_volume=stats(d.best_ask_volume),
        wide_spread_fraction=float((d.spread>2.00000001).mean()),
        wall_either_fraction=float((d.bid_wall.eq(True)|d.ask_wall.eq(True)).mean()),
        wall_touch_fraction=float((d.bid_wall_touch.eq(True)|d.ask_wall_touch.eq(True)).mean()),
        wall_depth_share=stats((d.bid_wall_share*d.bid_depth+d.ask_wall_share*d.ask_depth)/(d.bid_depth+d.ask_depth)),
        wall_distance=stats(pd.concat([d.bid_wall_distance,d.ask_wall_distance])),
        gap2=stats(pd.concat([d.bid_gap2,d.ask_gap2])),
        book_age=stats(d.age), trade_count=len(td), trade_volume=int(vol),
        trade_size=stats(td.volume), buy_aggressor_volume_fraction=float(td.loc[td.aggressor_side=='bid','volume'].sum()/vol),
        volume_ge1000_fraction=float(td.loc[td.volume>=1000,'volume'].sum()/vol),
        common_trade_sizes={str(k):int(v) for k,v in td.volume.value_counts().head(8).items()},
        slippage={str(q):stats(pd.concat([d['bid_slip'+str(q)],d['ask_slip'+str(q)]])) for q in (10,50,100)})

for session, d in p.groupby('session', sort=True):
    detail = dict(session=session, start=d.observed_at_utc.iloc[0], end=d.observed_at_utc.iloc[-1],
                  minutes=float((d.t.max()-d.t.min())/60), instruments={})
    for inst, x in d.groupby('instrument_id'):
        detail['instruments'][inst] = dict(start_mid=float(x.mid.iloc[0]),end_mid=float(x.mid.iloc[-1]),
                                          spread=stats(x.spread), interval=stats(x.t.diff()))
    result['sessions_detail'].append(detail)

# Forward outcomes use observed time within a recording, never bridge recordings or >2s gaps.
future_parts=[]
for (session, inst), d in p.groupby(['session','instrument_id']):
    d = d.sort_values('t').copy().reset_index(drop=True)
    t = d.t.to_numpy()
    seg = (d.t.diff()>2).cumsum().to_numpy()
    for h in (1,5,15,45):
        j = np.searchsorted(t, t+h)
        safe = np.minimum(j,len(d)-1)
        valid = (j<len(d)) & (t[safe]-t-h<=1) & (seg[safe]==seg)
        valid &= d.good.to_numpy() & d.good.to_numpy()[safe]
        d[f'future{h}'] = np.where(valid, d.mid.to_numpy()[safe]-d.mid.to_numpy(), np.nan)
    future_parts.append(d)
pf=pd.concat(future_parts,ignore_index=True)
for (day,inst), d in pf.groupby(['day','instrument_id']):
    # one observation per second prevents faster sessions dominating
    d=d.assign(sec=np.floor(d.t)).drop_duplicates(['session','sec'],keep='first')
    for h in (1,5,15,45):
        for feat in ('top','all','near','cap'):
            z=d[['imb_'+feat, f'future{h}']].dropna()
            x,y=z.iloc[:,0],z.iloc[:,1]
            strong=x.abs()>=.6
            move=y.abs()>.000001
            result['signals'].append(dict(day=day,instrument=inst,horizon=h,feature=feat,n=len(z),
                correlation=corr(x,y),strong_n=int(strong.sum()),
                strong_signed_move=float((np.sign(x[strong])*y[strong]).mean()),
                strong_direction_accuracy=float((np.sign(x[strong&move])==np.sign(y[strong&move])).mean()),
                strong_nonzero_n=int((strong&move).sum()),
                positive_move=float(y[x>=.6].mean()),negative_move=float(y[x<=-.6].mean())))

# Pair at each sampling cycle. Endpoints filtered for age, touch wall, spread and pair timing.
a=p[p.instrument_id=='PHILIPS_A'].set_index(['session','sample_id'])
c=p[p.instrument_id=='PHILIPS_B'].set_index(['session','sample_id'])
pair=a.join(c,lsuffix='_A',rsuffix='_B').reset_index()
pair['t']=pair[['t_A','t_B']].max(axis=1)
pair['diff']=pair.mid_A-pair.mid_B
pair['good']=pair.good_A & pair.good_B & ((pair.t_A-pair.t_B).abs()<=.75)
pair['day']=pair.day_A
pair['wall_mid_A']=(pair.bid_wall_price_A+pair.ask_wall_price_A)/2
pair['wall_mid_B']=(pair.bid_wall_price_B+pair.ask_wall_price_B)/2
result['wall_anchor_daily']={day:dict(A_wall_width=stats(d.ask_wall_price_A-d.bid_wall_price_A),
    B_wall_width=stats(d.ask_wall_price_B-d.bid_wall_price_B),
    wall_mid_A_minus_B=stats(d.wall_mid_A-d.wall_mid_B),
    A_mid_minus_wall=stats(d.mid_A-d.wall_mid_A),B_mid_minus_wall=stats(d.mid_B-d.wall_mid_B))
    for day,d in pair.groupby('day')}
result['pair_daily']={day:dict(raw_diff=stats(d['diff']),clean_diff=stats(d.loc[d.good,'diff']),
                             price_correlation=corr(d.loc[d.good,'mid_A'],d.loc[d.good,'mid_B']))
                      for day,d in pair.groupby('day')}

def design(t, period):
    return np.column_stack([np.ones(len(t)),np.sin(2*np.pi*t/period),np.cos(2*np.pi*t/period)])

cycle_predictions=[]
for session,d in pair[pair.good].groupby('session'):
    raw=pair[pair.session==session].sort_values('t').reset_index(drop=True)
    d=d.assign(sec=np.floor(d.t)).drop_duplicates('sec').sort_values('t')
    t=d.t.to_numpy()-d.t.iloc[0]
    y=d['diff'].to_numpy()
    if len(t)<120 or t[-1]<360:
        continue
    split=t[-1]*2/3
    train=t<=split
    periods=np.arange(120.,241.)
    losses=[]
    for period in periods:
        x=design(t,period)
        coef=np.linalg.lstsq(x[train],y[train],rcond=None)[0]
        losses.append(np.mean((y[train]-x[train]@coef)**2))
    best=periods[np.argmin(losses)]
    entry=dict(session=session,day=d.day.iloc[0],seconds=float(t[-1]),n=len(t),train_best_period=float(best),models={})
    for period in (180.,best):
        x=design(t,period)
        coef=np.linalg.lstsq(x[train],y[train],rcond=None)[0]
        pred=x@coef
        base=float(np.sqrt(np.mean((y[~train]-y[train][-1])**2)))
        entry['models'][str(period)]=dict(amplitude=float(np.hypot(coef[1],coef[2])),offset=float(coef[0]),
            train_r2=float(1-np.mean((y[train]-pred[train])**2)/np.var(y[train])),
            test_rmse=float(np.sqrt(np.mean((y[~train]-pred[~train])**2))),test_last_value_rmse=base,
            test_r2=float(1-np.mean((y[~train]-pred[~train])**2)/np.var(y[~train])))
    result['cycles'].append(entry)
    # Causal fixed-period estimate, refit every 5 seconds on trailing 360 seconds.
    # No period search in this forecast evaluation; period existed in strategy before this analysis.
    last=-np.inf
    raw_t=raw.t.to_numpy()-d.t.iloc[0]
    for i,now in enumerate(t):
        if now<180 or now-last<5:
            continue
        last=now
        sel=(t<=now)&(t>=now-360)
        if sel.sum()<100 or now-t[sel][0]<170:
            continue
        coef=np.linalg.lstsq(design(t[sel],180),y[sel],rcond=None)[0]
        for h in (5,15,45):
            j=np.searchsorted(raw_t,now+h)
            ri=np.searchsorted(raw_t,now)
            if j>=len(raw_t) or raw_t[j]-now-h>1 or np.max(np.diff(raw_t[ri:j+1]))>2:
                continue
            delta=float(((design(np.array([now+h]),180)-design(np.array([now]),180))@coef).item())
            actual=float(raw['diff'].iloc[j]-y[i])
            direction=np.sign(delta)
            # Displayed top prices for one share on both legs; no fill/latency assumption validated.
            cost=(d.spread_A.iloc[i]+d.spread_B.iloc[i]+raw.spread_A.iloc[j]+raw.spread_B.iloc[j])/2
            cycle_predictions.append(dict(day=d.day.iloc[0],session=session,h=h,t=float(now),pred=delta,
                actual=float(round(actual,8)),cost=float(cost),hypothetical_net=float(direction*actual-cost),
                future_good=bool(raw.good.iloc[j])))
cp=pd.DataFrame(cycle_predictions)
result['cycle_forward']=[]
for (day,h),d in cp.groupby(['day','h']):
    result['cycle_forward'].append(dict(day=day,horizon=int(h),n=len(d),
        direction_accuracy=float((np.sign(d.pred)==np.sign(d.actual)).mean()),
        pred_rmse=float(np.sqrt(np.mean((d.pred-d.actual)**2))),
        zero_change_rmse=float(np.sqrt(np.mean(d.actual**2))),
        future_bad_n=int((~d.future_good).sum()),
        mean_displayed_roundtrip_cost=float(d.cost.mean()),hypothetical_net=stats(d.hypothetical_net),
        actual_change=stats(d.actual)))

result['common_level_sizes']={inst:{str(v):n for (_,v),n in level_sizes.most_common() if _==inst}
                              for inst in ('PHILIPS_A','PHILIPS_B')}
result['common_level_sizes']={k:dict(list(v.items())[:12]) for k,v in result['common_level_sizes'].items()}

# Observed survival of a price level, not lifetime of an individual order.
result['wall_observed_runs']={}
for inst,g in p.groupby('instrument_id'):
    runs=[]
    for session,d in g.groupby('session'):
        d=d.sort_values('t')
        for side in ('bid','ask'):
            key=d[side+'_wall_price'].round(8)
            groups=((key!=key.shift())|(d.t.diff()>2)).cumsum()
            for _,z in d[key.notna()].groupby(groups):
                runs.append(z.t.iloc[-1]-z.t.iloc[0])
    result['wall_observed_runs'][inst]=stats(runs)

# Public transaction continuation/reversion after known trade arrival, not hypothetical own fills.
result['trade_markouts']=[]
for (day,inst), td in tr.groupby(['day','instrument_id']):
    d=p[(p.day==day)&(p.instrument_id==inst)&p.good].sort_values('t')
    tt=epoch(td.observed_at_utc).to_numpy()
    dt=d.t.to_numpy()
    for h in (1,5,15):
        j=np.searchsorted(dt,tt+h)
        safe=np.minimum(j,len(d)-1)
        valid=(j<len(d))&(dt[safe]-tt-h<=1)&(td.session.to_numpy()==d.session.to_numpy()[safe])
        mark=np.where(valid,np.where(td.aggressor_side=='bid',1,-1)*(d.mid.to_numpy()[safe]-td.price.to_numpy()),np.nan)
        for label,mask in [('all',np.ones(len(td),bool)),('small_le100',td.volume.to_numpy()<=100),('large_ge1000',td.volume.to_numpy()>=1000)]:
            z=valid&mask
            result['trade_markouts'].append(dict(day=day,instrument=inst,horizon=h,size=label,n=int(z.sum()),
                aggressor_markout_equal=float(np.nanmean(mark[z])),
                aggressor_markout_volume=float(np.average(mark[z],weights=td.volume.to_numpy()[z])) if z.any() else None))

def clean_json(x):
    if isinstance(x,dict): return {str(k):clean_json(v) for k,v in x.items()}
    if isinstance(x,list): return [clean_json(v) for v in x]
    if isinstance(x,(np.integer,np.floating)): x=x.item()
    if isinstance(x,float) and not np.isfinite(x): return None
    return x

(OUT/'metrics.json').write_text(json.dumps(clean_json(result),ensure_ascii=False,indent=2),encoding='utf-8')
cp.to_json(OUT/'cycle_predictions.jsonl',orient='records',lines=True)

plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'axes.spines.top':False,'axes.spines.right':False})
fig,axes=plt.subplots(3,1,figsize=(12,10),layout='constrained')
longest=max(result['cycles'],key=lambda x:x['seconds'])['session']
d=pair[(pair.session==longest)&pair.good].copy()
elapsed=(d.t-d.t.iloc[0])/60
axes[0].plot(elapsed,d.mid_A,label='PHILIPS_A',lw=.9,color='#2563eb')
axes[0].plot(elapsed,d.mid_B,label='PHILIPS_B',lw=.9,color='#e08024')
axes[0].set(title='Longest recording: common price trend with A/B oscillation',ylabel='Mid price',xlabel='Minutes from recording start')
axes[0].legend(loc='upper right')
sel=elapsed<15
axes[1].plot(elapsed[sel],d.loc[sel,'diff'],color='#2563eb',lw=1,label='Observed A minus B')
axes[1].axhline(0,color='#888',lw=.6)
axes[1].set(title='First 15 minutes: the relative price repeats about every 3 minutes',ylabel='A minus B',xlabel='Minutes from recording start')
axes[1].legend()
data=[x for x in result['signals'] if x['day']=='2026-09-10' and x['horizon']==5]
features=['top','all','near','cap']
labels=['Best level','All depth','Within 0.4','Within 0.4, capped at 100']
xx=np.arange(4)
for i,inst in enumerate(('PHILIPS_A','PHILIPS_B')):
    vals=[next(z for z in data if z['instrument']==inst and z['feature']==f)['correlation'] for f in features]
    axes[2].bar(xx+(i-.5)*.34,vals,.34,label=inst,color=['#2563eb','#e08024'][i])
axes[2].set_xticks(xx,labels)
axes[2].axhline(0,color='#888',lw=.6)
axes[2].set(title='September 10: imbalance correlation with 5-second future mid change',ylabel='Correlation')
axes[2].legend()
fig.savefig(OUT/'market_analysis.png',dpi=150)
print(json.dumps(clean_json({k:v for k,v in result.items() if k not in ('signals','sessions_detail','trade_markouts','common_level_sizes')}),indent=2))
