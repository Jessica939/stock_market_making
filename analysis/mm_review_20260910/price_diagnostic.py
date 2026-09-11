"""Historical quote forecast errors and observed order-age diagnostics.

Uses logged external-book quote mids as targets, never hypothetical fills.
Forecast clock starts at quote observation; target book timestamps must reach
the horizon and overshoot by at most one second. Errors are quote-weighted.
Order age is measured from acknowledgement observation to exchange fill time;
it is approximate, excludes unmatched/negative ages, and is not a causal test.
"""
import bisect
import json
import math
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
def timestamp(value):
    return datetime.fromisoformat(value).timestamp()

output = []
for path in sorted((ROOT/'data/runs/baseline').glob('*20260910*/*.jsonl')):
    rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    result = {'run':path.parent.name, 'instruments':{}}
    for instrument in ('PHILIPS_A','PHILIPS_B'):
        quotes = [r for r in rows if r['type']=='quote' and r['instrument']==instrument and r.get('book_scope')=='excluding_own_orders']
        targets = sorted((timestamp(q['book_timestamp']), (q['best_bid']+q['best_ask'])/2) for q in quotes)
        times = [t for t, _ in targets]
        horizons = {}
        for h in (1,5,15):
            errors = {name:[] for name in ('mid','depth_fair','logged_fair')}
            for q in quotes:
                target_time = timestamp(q['observed_at_utc'])+h
                index = bisect.bisect_left(times,target_time)
                if index==len(times) or times[index]-target_time>1:
                    continue
                target = targets[index][1]
                predictions = {'mid':(q['best_bid']+q['best_ask'])/2,
                               'depth_fair':q['fair_value']-q.get('cycle_shift',0),
                               'logged_fair':q['fair_value']}
                for name, prediction in predictions.items():
                    errors[name].append(prediction-target)
            horizons[h] = {name:dict(n=len(e),mae=sum(map(abs,e))/len(e),
                rmse=math.sqrt(sum(x*x for x in e)/len(e)),bias=sum(e)/len(e))
                for name,e in errors.items() if e}
        acknowledgements = {r['order_id']:r for r in rows if r['type']=='order_response' and r.get('success') and r['instrument']==instrument}
        marks = {r['trade_id']:r for r in rows if r['type']=='markout' and r['instrument']==instrument and r['horizon_seconds']==1}
        bins = {name:[] for name in ('0-0.5s','0.5-1s','1-2s','2s+')}
        excluded = 0
        for fill in (r for r in rows if r['type']=='fill' and r['instrument']==instrument):
            ack = acknowledgements.get(fill['order_id'])
            mark = marks.get(fill['trade_id'])
            if not ack or not mark:
                excluded+=1
                continue
            age = timestamp(fill['trade_timestamp'])-timestamp(ack['observed_at_utc'])
            if age<0:
                excluded+=1
                continue
            name = '0-0.5s' if age<.5 else '0.5-1s' if age<1 else '1-2s' if age<2 else '2s+'
            bins[name].append(mark)
        age_stats = {name:dict(fills=len(mm), volume=sum(m['volume'] for m in mm),
                markout_1s=sum(m['total'] for m in mm)/sum(m['volume'] for m in mm))
                for name,mm in bins.items() if mm}
        result['instruments'][instrument] = dict(forecast_errors=horizons, order_age=age_stats, age_excluded_fills=excluded)
    output.append(result)
destination = Path(__file__).with_name('price_diagnostic.json')
destination.write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
for result in output:
    print(result['run'])
    b = result['instruments']['PHILIPS_B']
    for h, values in b['forecast_errors'].items():
        print(h, {name:round(v['mae'],4) for name,v in values.items()}, 'n',values['mid']['n'])
    print('age',b['order_age'], 'excluded',b['age_excluded_fills'])
