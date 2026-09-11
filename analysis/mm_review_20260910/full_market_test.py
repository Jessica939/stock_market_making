"""Causal all-market forecast evaluation and delayed taker execution scenarios.

No private fills are used. Fixed current CycleSettings, reset per recording.
Observed books include own quotes: raw-depth and 200-lot removal sensitivity.
This tests directional signal monetization, not passive market-making fills.
"""
import bisect
import gzip
import json
import math
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from strategies.baseline.cycle_signal import CycleSignal, CycleSettings
OUT = Path(__file__).resolve().parent

def ts(s):
    return datetime.fromisoformat(s).timestamp()

def fresh(b,now):
    return (b and b['status']=='ok' and b.get('bids') and b.get('asks')
            and b['bids'][0][0]<b['asks'][0][0]
            and 0<=now-ts(b['book_timestamp'])<=1)

def mid(b):
    return (b['bids'][0][0]+b['asks'][0][0])/2

def cost(b,buy,remove,quantity=2):
    levels = b['asks'] if buy else b['bids']
    best = levels[0][0]
    left,total = quantity,0.
    for price,size in levels:
        if abs(price-best)>1.0000001:
            break
        takeout = min(remove,size)
        remove-=takeout
        take = min(left,size-takeout)
        total += price*take
        left-=take
        if not left:
            return total/quantity
    return None

def adapter(b):
    if not b or b['status']!='ok':
        return None
    return SimpleNamespace(timestamp=datetime.fromisoformat(b['book_timestamp']),
        bids=[SimpleNamespace(price=p,volume=v) for p,v in b['bids']],
        asks=[SimpleNamespace(price=p,volume=v) for p,v in b['asks']])

def load(directory):
    rows,truncated = [],[]
    for path in sorted(directory.glob('orderbooks*.jsonl.gz')):
        with gzip.open(path,'rt',encoding='utf-8') as f:
            while True:
                try:
                    line=f.readline()
                except EOFError:
                    truncated.append(path.name)
                    break
                if not line:
                    break
                rows.append(json.loads(line))
    return sorted(rows,key=lambda b:ts(b['observed_at_utc'])),truncated

def signals(rows):
    model=CycleSignal(CycleSettings())
    latest,frames={},[]
    last_decision=-math.inf
    for b in rows:
        latest[b['instrument_id']]=b
        if b['instrument_id']!='PHILIPS_B':
            continue
        now=ts(b['observed_at_utc'])
        sig=model.observe({k:adapter(v) for k,v in latest.items()},
                          {'PHILIPS_A':.1,'PHILIPS_B':.1},now,now)
        decision=now-last_decision>=1
        if decision:
            last_decision=now
        frames.append(dict(now=now,b=b,a=latest.get('PHILIPS_A'),signal=sig,decision=decision))
    return frames

def forecast_rows(frames):
    times=[f['now'] for f in frames]
    results=[]
    for f in frames:
        s=f['signal']; now=f['now']
        if not f['decision'] or not s.get('active') or not fresh(f['b'],now) or not fresh(f['a'],now):
            continue
        target=now+15
        i=bisect.bisect_left(times,target)
        row=dict(time=now,valid=False)
        while i<len(frames) and times[i]<=target+1:
            g=frames[i]
            if fresh(g['b'],g['now']) and ts(g['b']['book_timestamp'])>=target:
                delta=mid(g['b'])-mid(f['b'])
                prediction=s['predicted_B_change']
                weighted=prediction*s['fit_weight']
                row.update(valid=True,delta=delta,prediction=prediction,weighted=weighted,
                    zero_error=-delta,raw_error=prediction-delta,weighted_error=weighted-delta)
                if (fresh(g['a'],g['now']) and ts(g['a']['book_timestamp'])>=target
                        and abs(ts(g['b']['book_timestamp'])-ts(g['a']['book_timestamp']))<=.75):
                    basis=(mid(g['a'])-mid(g['b']))-(mid(f['a'])-mid(f['b']))
                    row.update(basis_delta=basis,basis_zero_error=-basis,basis_error=-prediction-basis)
                break
            i+=1
        results.append(row)
    return results

def summarize_forecasts(rows):
    good=[r for r in rows if r['valid']]
    result=dict(candidates=len(rows),valid=len(good),missing=len(rows)-len(good))
    if not good:
        return result
    for field in ('zero_error','raw_error','weighted_error','basis_zero_error','basis_error'):
        e=[r[field] for r in good if field in r]
        if e:
            result[field]=dict(n=len(e),mae=sum(map(abs,e))/len(e),rmse=math.sqrt(sum(x*x for x in e)/len(e)))
    moving=[r for r in good if abs(r['delta'])>1e-8 and abs(r['prediction'])>1e-8]
    result['direction_nonzero_n']=len(moving)
    result['direction_accuracy']=sum(r['delta']*r['prediction']>0 for r in moving)/len(moving) if moving else None
    result['up_fraction']=sum(r['delta']>0 for r in moving)/len(moving) if moving else None
    return result

def trade(frames,remove=200,stop=None,entry_delay=.25,reverse=False):
    position=None; pending=None; intent=None
    closed=[];stats=Counter();high=drawdown=0.;realized=0.;mark=None
    for f in frames:
        now=f['now'];b=f['b'];s=f['signal']
        if position:
            if now>=position['time']+15 and intent is None:
                intent=dict(reason='timeout',due=position['time']+15+entry_delay)
            if not fresh(b,now) or ts(b['book_timestamp'])<position['time']:
                stats['held_invalid_book']+=1
                continue
            px=cost(b,position['sign']<0,remove)
            if px is None:
                stats['held_insufficient_depth']+=1
                continue
            pnl=2*position['sign']*(px-position['price'])
            mark=dict(time=now,equity=realized+pnl)
            high=max(high,realized+pnl);drawdown=max(drawdown,high-realized-pnl)
            position['worst']=min(position['worst'],pnl/2)
            if stop is not None and pnl/2<=-stop and intent is None:
                intent=dict(reason='stop',due=now+entry_delay)
            if intent and now>=intent['due'] and ts(b['book_timestamp'])>=intent['due']:
                closed.append(dict(**position,exit_time=now,exit_price=px,pnl=pnl,reason=intent['reason']))
                realized+=pnl;position=None;intent=None;mark=dict(time=now,equity=realized)
            continue
        if pending:
            if now>pending['signal_time']+entry_delay+1:
                stats['entry_expired']+=1;pending=None
            elif now>=pending['signal_time']+entry_delay and fresh(b,now) and ts(b['book_timestamp'])>=pending['signal_time']+entry_delay:
                buy=cost(b,True,remove);sell=cost(b,False,remove)
                if buy is not None and sell is not None:
                    # Recheck spread/price movement against the frozen forecast,
                    # without refitting the prediction at a future entry price.
                    px=buy if pending['sign']>0 else sell
                    remaining=pending['sign']*(pending['target_mid']-px)
                    exit_half=(buy-sell)/2
                    if remaining>exit_half+.1:
                        position=dict(time=now,price=px,sign=pending['sign'],worst=0.,
                                      signal_time=pending['signal_time'])
                        stats['entered']+=1;mark=None
                    else:
                        stats['rejected_after_delay']+=1
                    pending=None
            continue
        if not f['decision'] or not s.get('active') or not fresh(b,now):
            continue
        prediction=s['predicted_B_change']*s['fit_weight']*(-1 if reverse else 1)
        buy=cost(b,True,remove);sell=cost(b,False,remove)
        if buy is None or sell is None:
            stats['entry_insufficient_depth']+=1
            continue
        # Estimated round-trip cost = current full executable spread, plus .1
        # buffer. Prediction is relative-value based; A-flat is an assumption.
        if abs(prediction)>buy-sell+.1:
            pending=dict(signal_time=now,sign=1 if prediction>0 else -1,
                         target_mid=mid(b)+prediction)
            stats['signals']+=1
    assert stats['entered']==len(closed)+(position is not None)
    return dict(remove_depth=remove,stop_per_share=stop,entry_delay=entry_delay,reverse=reverse,
        closed_trades=len(closed),realized_pnl=realized,
        terminal_equity=realized if position is None else (mark['equity'] if mark else None),
        residual_position=2*position['sign'] if position else 0,terminal_mark=mark,
        pending_entry=pending is not None,observed_drawdown=drawdown,
        wins=sum(t['pnl']>1e-8 for t in closed),stats=dict(stats),trades=closed)

def main():
    output=[]
    for directory in sorted((ROOT/'data/market').iterdir()):
        rows,truncated=load(directory)
        if not rows:
            continue
        fs=signals(rows)
        forecasts=forecast_rows(fs)
        scenarios=[trade(fs,0),trade(fs,200),trade(fs,200,stop=2),trade(fs,200,entry_delay=1),trade(fs,200,reverse=True)]
        result=dict(recording=directory.name,start=rows[0]['observed_at_utc'],end=rows[-1]['observed_at_utc'],
            minutes=(ts(rows[-1]['observed_at_utc'])-ts(rows[0]['observed_at_utc']))/60,
            rows=len(rows),truncated=truncated,signal_states=dict(Counter(f['signal']['reason'] for f in fs)),
            forecasts=summarize_forecasts(forecasts),scenarios=scenarios,forecast_details=forecasts)
        output.append(result)
        print(directory.name,'min',round(result['minutes'],1),'forecasts',result['forecasts']['valid'],
              'PnL',[(round(s['realized_pnl'],2),s['residual_position'],s['closed_trades']) for s in scenarios],flush=True)
    (OUT/'full_market_test.json').write_text(json.dumps(output,indent=2),encoding='utf-8')
    for day in ('20260909','20260910'):
        selected=[r for r in output if day in r['recording']]
        print(day,'forecast',summarize_forecasts([f for r in selected for f in r['forecast_details']]))
        print('scenario totals',[(sum(r['scenarios'][i]['realized_pnl'] for r in selected),sum(r['scenarios'][i]['closed_trades'] for r in selected)) for i in range(5)])

if __name__=='__main__':
    main()
