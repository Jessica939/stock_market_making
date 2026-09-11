"""Offline threshold proxies using the user's >200 official-order rule.

Aggregated levels and matched trade quantities are not original order sizes.
Small quantities are therefore participant candidates, not verified identities.
"""
import gzip
import json
from pathlib import Path
import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[2]
OUT=Path(__file__).resolve().parent
prices=[]
trades=[]
depth=[]
for folder in sorted((ROOT/'data/market').iterdir()):
    if not (folder/'prices.csv').exists(): continue
    p=pd.read_csv(folder/'prices.csv').assign(session=folder.name)
    prices.append(p)
    trades.append(pd.read_csv(folder/'trades.csv').assign(session=folder.name))
    for path in folder.glob('orderbooks*.gz'):
        try:
            with gzip.open(path,'rt',encoding='utf-8') as f:
                for line in f:
                    r=json.loads(line)
                    row=dict(session=folder.name,sample_id=r['sample_id'],instrument_id=r['instrument_id'])
                    for side, key in [('bid','bids'),('ask','asks')]:
                        levels=r.get(key) or []
                        if not levels: continue
                        near=[(pr,v) for pr,v in levels if abs(pr-levels[0][0])<=.40000001]
                        row[side+'_small_near']=sum(v for _,v in near if v<=200)
                        row[side+'_large_near']=sum(v for _,v in near if v>200)
                        large=next((pr for pr,v in levels if v>200),None)
                        row[side+'_small_ahead']=levels[0][1]<=200 and large is not None
                        row[side+'_improvement']=abs(levels[0][0]-large) if row[side+'_small_ahead'] else np.nan
                    depth.append(row)
        except EOFError:
            pass
p=pd.concat(prices,ignore_index=True).merge(pd.DataFrame(depth),on=['session','sample_id','instrument_id'],how='left',validate='one_to_one')
p['t']=pd.to_datetime(p.observed_at_utc,utc=True).astype('int64')/1e9
p['day']=p.observed_at_utc.str[:10]
age=p.t-pd.to_datetime(p.book_timestamp,utc=True).astype('int64')/1e9
p['good']=(p.status=='ok')&p.spread.between(.000001,2.000001)&age.between(-.5,2)&(p.best_bid_volume<15000)&(p.best_ask_volume<15000)
tr=pd.concat(trades,ignore_index=True).drop_duplicates(['instrument_id','trade_timestamp','trade_id'])
tr['day']=tr.trade_timestamp.str[:10]
# Exclude only positively matched own fills when describing others' small trades.
own={}
for path in sorted((ROOT/'data/runs').glob('*/*/*.jsonl')):
    with path.open(encoding='utf-8') as f:
        for line in f:
            if not line.strip(): continue
            r=json.loads(line)
            if r.get('type')!='fill' or r.get('trade_id') is None: continue
            stamp=r.get('trade_timestamp') or r.get('exchange_timestamp')
            if not stamp: continue
            key=(str(stamp)[:10],r.get('instrument'),str(r['trade_id']))
            value=(float(r['price']),int(r['volume']),r.get('side'))
            if key in own:
                assert own[key]==value, (path,key)
            own[key]=value
matches=[]
for row in tr.itertuples():
    key=(row.day,row.instrument_id,str(row.trade_id))
    value=own.get(key)
    matched=value is not None and np.isclose(row.price,value[0]) and row.volume==value[1]
    matches.append(matched)
tr['known_own']=matches
result={'rule_source':'User: original orders >200 are official robots; others probably participants.',
        'caveat':'Price-level totals and matched quantities are proxies, not original order identities.',
        'participant_order_limit':200,'participant_position_limit_per_instrument':100,
        'known_own_matched_trades':int(tr.known_own.sum()),
        'trade_groups':[],'depth_groups':[],'signals':[],'signals_common_sample':[],'book_topology':[]}
assert ((tr.volume>200)|(tr.volume<=200)).all()
assert not tr.loc[tr.known_own,'volume'].gt(200).any()
for (day,inst),d in p.groupby(['day','instrument_id']):
    small_b=d.best_bid_volume<=200
    small_a=d.best_ask_volume<=200
    result['book_topology'].append(dict(day=day,instrument=inst,n=len(d),
        both_small_fraction=float((small_b&small_a).mean()),
        either_small_fraction=float((small_b|small_a).mean()),
        small_best_size_median=float(pd.concat([d.loc[small_b,'best_bid_volume'],d.loc[small_a,'best_ask_volume']]).median())))
    for side in ('bid','ask'):
        imp=d[side+'_improvement'].dropna()
        result['depth_groups'].append(dict(day=day,instrument=inst,side=side,
            small_best_fraction=float((d['best_'+side+'_volume']<=200).mean()),
            improvement_n=len(imp),improvement_median=float(imp.median()),
            improvement_one_tick_fraction=float(np.isclose(imp,.1).mean())))
for (day,inst),td in tr.groupby(['day','instrument_id']):
    d=p[(p.day==day)&(p.instrument_id==inst)&p.good].sort_values('t')
    dt=d.t.to_numpy()
    tt=pd.to_datetime(td.observed_at_utc,utc=True).astype('int64').to_numpy()/1e9
    j=np.searchsorted(dt,tt+5)
    safe=np.minimum(j,len(d)-1)
    valid=(j<len(d))&(dt[safe]-tt-5<=1)&(td.session.to_numpy()==d.session.to_numpy()[safe])
    marks=np.where(td.aggressor_side=='bid',1,-1)*(d.mid.to_numpy()[safe]-td.price.to_numpy())
    for label,mask in [('gt200',td.volume.to_numpy()>200),('le200',td.volume.to_numpy()<=200),
                       ('le200_excluding_known_own',(td.volume.to_numpy()<=200)&~td.known_own.to_numpy()),
                       ('known_own',td.known_own.to_numpy())]:
        selected=valid&mask
        result['trade_groups'].append(dict(day=day,instrument=inst,group=label,n=int(mask.sum()),
            count_fraction=float(mask.mean()),volume=int(td.volume.to_numpy()[mask].sum()),
            volume_fraction=float(td.volume.to_numpy()[mask].sum()/td.volume.sum()),markout_n=int(selected.sum()),
            markout5_equal=float(marks[selected].mean()) if selected.any() else None,
            markout5_volume=float(np.average(marks[selected],weights=td.volume.to_numpy()[selected])) if selected.any() else None))
parts=[]
for (session,inst),d in p.groupby(['session','instrument_id']):
    d=d.sort_values('t').copy()
    t=d.t.to_numpy()
    j=np.searchsorted(t,t+5)
    safe=np.minimum(j,len(d)-1)
    seg=(d.t.diff()>2).cumsum().to_numpy()
    valid=(j<len(d))&(t[safe]-t-5<=1)&(seg[safe]==seg)&d.good.to_numpy()&d.good.to_numpy()[safe]
    d['future5']=np.where(valid,d.mid.to_numpy()[safe]-d.mid.to_numpy(),np.nan)
    d['sec']=np.floor(t)
    parts.append(d.drop_duplicates('sec',keep='first'))
for (day,inst),d in pd.concat(parts).groupby(['day','instrument_id']):
    features={}
    for group in ('small','large'):
        bv,av=d['bid_'+group+'_near'],d['ask_'+group+'_near']
        imbalance=(bv-av)/(bv+av).replace(0,np.nan)
        features[group]=imbalance
        z=pd.DataFrame({'x':imbalance,'y':d.future5}).dropna()
        result['signals'].append(dict(day=day,instrument=inst,group=group,n=len(z),
            correlation=float(z.x.corr(z.y))))
    common=pd.DataFrame({**features,'y':d.future5}).dropna()
    result['signals_common_sample'].append(dict(day=day,instrument=inst,n=len(common),
        small_correlation=float(common.small.corr(common.y)),large_correlation=float(common.large.corr(common.y))))
(OUT/'participant_split.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
print(json.dumps(result,indent=2))
