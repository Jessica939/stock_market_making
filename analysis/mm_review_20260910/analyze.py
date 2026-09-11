"""Read-only baseline log review; no exchange imports or connections."""
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
results = []
for path in sorted((ROOT / 'data/runs/baseline').glob('*/*.jsonl')):
    rows = [json.loads(s) for s in path.read_text(encoding='utf-8').splitlines() if s.strip()]
    settings = next((r for r in rows if r.get('action') == 'settings'), {})
    start, end = [datetime.fromisoformat(r['observed_at_utc']) for r in (rows[0], rows[-1])]
    result = dict(run=path.parent.name, source=str(path.relative_to(ROOT)),
                  version=settings.get('strategy_version', 'unversioned'),
                  start_beijing=(start + timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S'),
                  end_beijing=(end + timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S'),
                  minutes=(end-start).total_seconds()/60,
                  session_end_present=any(r['type']=='session_end' for r in rows), instruments={})
    for inst in ('PHILIPS_A', 'PHILIPS_B'):
        snapshots = [r for r in rows if r['type']=='snapshot' and r['instrument']==inst and r.get('mid') is not None]
        fills = [r for r in rows if r['type']=='fill' and r['instrument']==inst]
        first, last = snapshots[0], snapshots[-1]
        cashflow = sum((1 if r['side']=='ask' else -1)*r['price']*r['volume'] for r in fills)
        quantity = sum((1 if r['side']=='bid' else -1)*r['volume'] for r in fills)
        assert abs(last['cash']-first['cash']-cashflow) < 1e-6, path
        assert last['position']-first['position']==quantity, path
        pnl = last['pnl_mid']-first['pnl_mid']
        carry = first['position']*(last['mid']-first['mid'])
        high = first['pnl_mid']
        drawdown = 0
        for s in snapshots:
            high = max(high, s['pnl_mid'])
            drawdown = max(drawdown, high-s['pnl_mid'])
        marks = {}
        for horizon in (1,3,5,15,30,60):
            mm = [r for r in rows if r['type']=='markout' and r['instrument']==inst and r['horizon_seconds']==horizon]
            if mm:
                volume = sum(r['volume'] for r in mm)
                marks[horizon] = dict(per_share=sum(r['total'] for r in mm)/volume,
                                      fills=len(mm), volume=volume,
                                      coverage=len(mm)/len(fills),
                                      status_counts=dict(Counter(r.get('status') for r in mm)))
        result['instruments'][inst] = dict(pnl_mid_change=pnl, initial_position=first['position'],
            final_position=last['position'], initial_mid=first['mid'], final_mid=last['mid'],
            inherited_inventory_mtm=carry, new_fills_marked_at_end=pnl-carry,
            max_abs_observed_position=max(abs(r['position']) for r in snapshots),
            observed_mid_drawdown=drawdown, fill_count=len(fills),
            fill_volume=sum(r['volume'] for r in fills), markouts=marks)
    results.append(result)
(OUT/'metrics.json').write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding='utf-8')
for r in results:
    if r['start_beijing'].startswith('2026-09-10'):
        print(r['start_beijing'], r['version'], round(r['minutes'],2))
        for inst, m in r['instruments'].items():
            print(inst, 'PnL',round(m['pnl_mid_change'],2),'drawdown',round(m['observed_mid_drawdown'],2))
print('All eight runs: fill cashflow and position changes reconcile.')
