"""Fixed mechanism ablations after the dynamic exit study; no parameter search."""
import bisect
from dataclasses import asdict
import itertools
import json
import math
from pathlib import Path
import statistics
import sys

ROOT=Path(__file__).resolve().parents[2]
OUT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'analysis/b_dynamic_exit_20260910'))
from study import load_run, digest
from replay import Replay, Scenario
from study import ts


def future_quote(quotes,times,target):
    index=bisect.bisect_left(times,target)
    while index<len(times) and times[index]<=target+1:
        q=quotes[index][1]
        if ts(q['book_timestamp'])>=target:
            return q
        index+=1
    return None


def pricing_diagnostic(events):
    quotes=[(t,q) for t,k,q in events if k=='quote' and q is not None]
    times=[t for t,_ in quotes]
    result=dict(quotes=len(quotes),outside=0,offsets=[],forecasts=[])
    for t,q in quotes:
        fair=q['fair_value']-q.get('cycle_shift',0.)
        mid=(q['best_bid']+q['best_ask'])/2
        result['outside']+=fair<q['best_bid']-1e-8 or fair>q['best_ask']+1e-8
        result['offsets'].append(fair-mid)
    offsets=result.pop('offsets')
    result['absolute_offset_median']=statistics.median(abs(x) for x in offsets)
    result['absolute_offset_max']=max(abs(x) for x in offsets)
    for h in (1,3,5):
        errors={mode:[] for mode in ('depth','clipped','mid')}
        for t,q in quotes:
            future=future_quote(quotes,times,t+h)
            if future is None:
                continue
            target=(future['best_bid']+future['best_ask'])/2
            fair=q['fair_value']-q.get('cycle_shift',0.)
            preds=dict(depth=fair,clipped=min(q['best_ask'],max(q['best_bid'],fair)),
                       mid=(q['best_bid']+q['best_ask'])/2)
            for mode,pred in preds.items():
                errors[mode].append(pred-target)
        for mode,e in errors.items():
            result['forecasts'].append(dict(horizon=h,mode=mode,n=len(e),mae=statistics.mean(abs(x) for x in e),
                rmse=math.sqrt(statistics.mean(x*x for x in e)),bias=statistics.mean(e)))
    return result


def fill_diagnostic(replay,events):
    quotes=[(t,q) for t,k,q in events if k=='quote' and q is not None]
    times=[t for t,_ in quotes]
    groups={}
    def add(label,volume,mark):
        group=groups.setdefault(label,dict(volume=0,value=0.,parts=0))
        group['volume']+=volume
        group['value']+=volume*mark
        group['parts']+=1
    for f in replay.fills:
        sign=1 if f['side']=='bid' else -1
        reduced=min(abs(f['before']),f['volume']) if f['before']*sign<0 else 0
        parts=[('reduce',reduced),('increase',f['volume']-reduced)]
        for h in (1,3,5):
            q=future_quote(quotes,times,f['t']+h)
            if q is None:
                continue
            mark=sign*((q['best_bid']+q['best_ask'])/2-f['price'])
            for role,volume in parts:
                if volume:
                    add(role+'_'+str(h)+'s',volume,mark)
                    ctx=f.get('context')
                    if ctx:
                        age=f['t']-ctx['decision_t']
                        add(role+'_'+str(h)+'s_'+('age_le_0.5' if age<=.5 else 'age_gt_0.5'),volume,mark)
    for g in groups.values():
        g['mark_per_share']=g.pop('value')/g['volume']
    return groups


def main():
    prior=json.loads((ROOT/'analysis/b_dynamic_exit_20260910/metrics.json').read_text(encoding='utf-8'))
    original=json.loads((ROOT/'analysis/b_loss_review_20260910/metrics.json').read_text(encoding='utf-8'))
    policies=[dict(policy='depth_guard',fair_mode='depth',entry_extra_ticks=0),
              dict(policy='clipped_guard',fair_mode='clipped',entry_extra_ticks=0),
              dict(policy='mid_guard',fair_mode='mid',entry_extra_ticks=0),
              dict(policy='wider_entry_guard',fair_mode='depth',entry_extra_ticks=1)]
    hashes={str(p.relative_to(ROOT)):digest(p) for p in
        [ROOT/'order_execution.py',ROOT/'quote_helpers.py']+list((ROOT/'strategies/baseline').glob('*.py'))+list((ROOT/'strategies/baseline').glob('*.ipynb'))}
    out=dict(policies=policies,runs=[],scenarios=[],description='In-sample pricing mechanism ablations. All policies retain exit guard; no timeout.')
    for previous in prior['runs']:
        info=next(r for r in original['runs'] if r['run']==previous['run'])
        cfg,events,meta=load_run(info,previous['market_session'])
        meta['pricing_diagnostic']=pricing_diagnostic(events)
        out['runs'].append(meta)
        for queue,latency,removal in itertools.product(('through','displayed'),(.05,.25),(0,200)):
            for policy in policies:
                s=Scenario(exit_guard=True,queue=queue,latency=latency,depth_removal=removal,**policy)
                replay=Replay(cfg,s,meta['start'],meta['end'])
                result=replay.run(events)
                if s.policy=='depth_guard':
                    old=next(r for r in prior['scenarios'] if r['run']==meta['run'] and r['policy']=='guard'
                             and r['queue']==queue and r['latency']==latency and r['depth_removal']==removal)
                    for k in ('cash','final_position','peak_abs_position','terminal_equity'):
                        assert abs(result[k]-old[k])<1e-7,(k,result[k],old[k])
                out['scenarios'].append(dict(run=meta['run'],**asdict(s),**result))
                if queue=='displayed' and latency==.05 and removal==200:
                    out['scenarios'][-1]['fill_diagnostic']=fill_diagnostic(replay,events)
                    prefix=meta['run']+'_'+s.policy
                    for kind,records in [('fills',replay.fills),('quotes',replay.quotes),('episodes',replay.episodes)]:
                        (OUT/(prefix+'_'+kind+'.jsonl')).write_text(''.join(json.dumps(r)+'\n' for r in records),encoding='utf-8')
                    print(meta['source_version'],s.policy,round(result['terminal_equity'],3),
                          'volume',result['stats'].get('volume',0),'pos',result['final_position'],
                          'peak',result['peak_abs_position'],flush=True)
    for path,sha in hashes.items():
        assert digest(ROOT/path)==sha
    out['strategy_sha256']=hashes
    out['strategy_files_unchanged']=True
    (OUT/'metrics.json').write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')


if __name__=='__main__':
    main()
