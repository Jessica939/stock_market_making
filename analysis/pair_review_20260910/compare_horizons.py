"""Signal-price hypothetical exits, not a fill simulation or additive PnL."""
import bisect
import gzip
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
def stamp(value):
    return datetime.fromisoformat(value).timestamp()
def value(levels, quantity):
    total = 0
    for price, volume in levels:
        take = min(quantity, volume)
        total += price*take
        quantity -= take
        if not quantity:
            return total
    return None

def main():
    frames, incomplete = [], []
    for path in sorted((ROOT/'data/market').glob('*/orderbooks_*.gz')):
        samples = {}
        try:
            with gzip.open(path, 'rt', encoding='utf-8') as stream:
                for line in stream:
                    row = json.loads(line)
                    samples.setdefault(row['sample_id'], {})[row['instrument_id']] = row
        except (EOFError, OSError, ValueError):
            incomplete.append(str(path.relative_to(ROOT)))
        for sample in samples.values():
            if not all(s in sample for s in ('PHILIPS_A','PHILIPS_B')):
                continue
            a,b = (sample[s] for s in ('PHILIPS_A','PHILIPS_B'))
            if any(not r.get('bids') or not r.get('asks') or not r.get('book_timestamp') for r in (a,b)):
                continue
            t = max(stamp(r['observed_at_utc']) for r in (a,b))
            if any(not -1 <= t-stamp(r['book_timestamp']) <= 2 for r in (a,b)):
                continue
            if abs(stamp(a['book_timestamp'])-stamp(b['book_timestamp'])) > .75:
                continue
            frames.append((t,a,b,str(path.relative_to(ROOT))))
    frames.sort(key=lambda r:r[0]); times=[r[0] for r in frames]
    results=[]
    for path in sorted((ROOT/'data/runs/pair').glob('*/events.jsonl')):
        for line in path.read_text(encoding='utf-8').splitlines():
            r=json.loads(line)
            if r['type']!='decision' or r.get('reason') not in ('cycle_long_A_short_B','cycle_short_A_long_B'):
                continue
            t=stamp(r['recorded_at']); n=r['diagnostics']['pair_size']; long=r['targets']['PHILIPS_A']>0
            a,b=(r['books'][s] for s in ('PHILIPS_A','PHILIPS_B'))
            entry=n*(a['ask']-b['bid'] if long else b['ask']-a['bid'])
            outcomes={}
            for horizon in (45,60,90):
                candidates=frames[bisect.bisect_left(times,t+horizon):bisect.bisect_right(times,t+horizon+1)]
                outcomes[str(horizon)]=None
                for when,a,b,source in candidates:
                    sell=value(a['bids'] if long else b['bids'],n)
                    buy=value(b['asks'] if long else a['asks'],n)
                    if sell is not None and buy is not None:
                        outcomes[str(horizon)]=dict(pnl=sell-buy-entry,seconds=when-t,source=source)
                        break
            results.append(dict(time=r['recorded_at'],size=n,outcomes=outcomes))
    output=dict(method='Hypothetical signal bid/ask entry; fresh synchronized displayed-depth exit within 1 second of target. No queue/slippage/price-band guarantee; overlapping signals are not additive. Complete records from incomplete gzip retained and flagged.',incomplete_files=incomplete,entries=results)
    Path(__file__).with_name('horizon_comparison.json').write_text(json.dumps(output,indent=2)+'\n',encoding='utf-8')
    for r in results:
        print(r['time'],{h:None if v is None else round(v['pnl'],2) for h,v in r['outcomes'].items()})
if __name__=='__main__':
    main()
