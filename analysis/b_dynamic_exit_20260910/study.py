"""Run fixed policies across latency/queue/depth assumptions; never optimize them."""
import csv
from dataclasses import asdict
from datetime import datetime
import gzip
import hashlib
import itertools
import json
from pathlib import Path

from replay import ROOT, Replay, Scenario, quote_for_position

OUT = Path(__file__).resolve().parent


def ts(s):
    return datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_run(info, session):
    path = ROOT/info['source']
    assert digest(path) == info['sha256']
    rows = [json.loads(l) for l in path.read_text(encoding='utf-8').splitlines() if l.strip()]
    cfg = next(r for r in rows if r.get('action') == 'settings')
    start, end = ts(rows[0]['observed_at_utc']), ts(rows[-1]['observed_at_utc'])
    quotes = [r for r in rows if r.get('type') == 'quote' and r.get('instrument') == 'PHILIPS_B']
    # At the historical position, the reconstructed source-version quote must
    # reproduce all recorded prices and sizes before any counterfactual run.
    differences = []
    for q in quotes:
        candidate = quote_for_position(q, q['position'], cfg,
                                      price_shift=cfg.get('cycle_price_shift_enabled', True))
        for field in ('bid_price', 'ask_price', 'buy_volume', 'sell_volume', 'center', 'cycle_risk_shift'):
            recorded = q.get(field,q.get('cycle_shift',0.)) if field=='cycle_risk_shift' else q[field]
            if abs(candidate[field]-recorded) > 1e-7:
                differences.append(dict(t=q['observed_at_utc'], field=field,
                                        calculated=candidate[field], logged=recorded))
    assert not differences, differences[:5]
    events = [(ts(q['observed_at_utc']), 'quote', q) for q in quotes]
    for r in rows:
        if r.get('action') == 'cycle_error' or (r.get('action') == 'skip_quote' and r.get('instrument') == 'PHILIPS_B'):
            events.append((ts(r['observed_at_utc']), 'quote', None))
    truncated = []
    inputs = {str(path.relative_to(ROOT)): digest(path)}
    for path in sorted((ROOT/'data/market'/session).glob('orderbooks*.jsonl.gz')):
        inputs[str(path.relative_to(ROOT))] = digest(path)
        with gzip.open(path, 'rt', encoding='utf-8') as source:
            while True:
                try:
                    line = source.readline()
                except EOFError:
                    truncated.append(str(path.relative_to(ROOT)))
                    break
                if not line:
                    break
                b = json.loads(line)
                if b['instrument_id'] != 'PHILIPS_B':
                    continue
                now = ts(b['observed_at_utc'])
                if start <= now <= end:
                    b['stamp'] = ts(b['book_timestamp']) if b.get('book_timestamp') else -1e30
                    events.append((now, 'book', b))
    own = {r['trade_id']: r for r in rows if r.get('type') == 'fill' and r.get('instrument') == 'PHILIPS_B'}
    path = ROOT/'data/market'/session/'trades.csv'
    inputs[str(path.relative_to(ROOT))] = digest(path)
    ids = set()
    with path.open(encoding='utf-8-sig', newline='') as source:
        for r in csv.DictReader(source):
            if r['instrument_id'] != 'PHILIPS_B':
                continue
            now = ts(r['trade_timestamp'])
            tid = int(r['trade_id'])
            if not start <= now <= end:
                continue
            assert tid not in ids
            ids.add(tid)
            assert r['aggressor_side'] in ('bid', 'ask')
            events.append((now, 'trade', dict(price=float(r['price']), volume=int(r['volume']),
                side=r['aggressor_side'], trade_id=tid,
                own_aggressive=tid in own and own[tid]['side'] == r['aggressor_side'])))
    priority = dict(trade=0, book=1, quote=2)
    events.sort(key=lambda e: (e[0], priority[e[1]]))
    return cfg, events, dict(run=info['run'], source_version=info['version'], start=start, end=end,
        original_pnl=info['pnl_mid_change'], original_volume=info['fill_volume'],
        original_initial_position=info['first_position'], original_peak_position=info['peak_abs_fill_position'],
        source_quote_reconstruction_checked=len(quotes), events=len(events),
        public_trades=len(ids), market_session=session, truncated_files=truncated, inputs_sha256=inputs)


def main():
    prior = json.loads((ROOT/'analysis/b_loss_review_20260910/metrics.json').read_text(encoding='utf-8'))
    attribution = [json.loads(l) for l in (ROOT/'analysis/b_exit_guard_20260910/fills.jsonl').read_text().splitlines()]
    strategy_files = [ROOT/'order_execution.py', ROOT/'quote_helpers.py'] + list((ROOT/'strategies/baseline').glob('*.py')) + list((ROOT/'strategies/baseline').glob('*.ipynb'))
    sha_before = {str(p.relative_to(ROOT)): digest(p) for p in strategy_files}
    output = dict(description='Frozen historical signal, dynamic inventory and order execution scenarios; not verified counterfactual PnL.',
        policies=[dict(name=n, guard=g, timeout=h) for n,g,h in (
            ('baseline',False,None),('guard',True,None),('guard_1s',True,1),('guard_3s',True,3),
            ('baseline_1s',False,1),('baseline_3s',False,3))],
        assumptions=dict(initial_position=0,initial_cash=0,recipe='v2 on both windows',
            immediate_private_fill_knowledge=True,signal_and_quote_schedule='historical frozen',
            atomic_two_side_replace_after_latency=True,half_visible_depth_accessible=True,
            own_aggressive_public_trades_excluded=True,private_fills_injected=False,
            passive='through-price prints, or equality after initial displayed queue depleted',
            queue_cancellations_inferred=False,market_impact=False,fees='sensitivity per traded share, not exchange tariff',
            final_inventory='marked to last valid mid, never silently liquidated'),
        runs=[], scenarios=[])
    for info in prior['runs']:
        if not info['version'].startswith('baseline_cycle_'):
            continue
        sessions = {r['session'] for r in attribution if r['run']==info['run'] and r['session']}
        assert len(sessions)==1
        cfg, events, meta = load_run(info, sessions.pop())
        output['runs'].append(meta)
        for queue, latency, removal in itertools.product(('through','displayed'),(.05,.25),(0,200)):
            for policy in output['policies']:
                s = Scenario(policy=policy['name'], exit_guard=policy['guard'], timeout=policy['timeout'],
                             queue=queue, latency=latency, depth_removal=removal)
                replay = Replay(cfg, s, meta['start'], meta['end'])
                result = replay.run(events)
                output['scenarios'].append(dict(run=info['run'], **asdict(s), **result))
                # Detailed traces for the predeclared main assumption only.
                if queue=='displayed' and latency==.05 and removal==200:
                    name=info['run']+'_'+s.policy
                    for kind, records in [('fills',replay.fills),('quotes',replay.quotes),('episodes',replay.episodes),('marks',replay.marks)]:
                        (OUT/(name+'_'+kind+'.jsonl')).write_text(''.join(json.dumps(r)+'\n' for r in records), encoding='utf-8')
                    print(info['version'],s.policy,'equity',round(result['terminal_equity'],3),
                          'position',result['final_position'],'peak',result['peak_abs_position'],
                          'volume',result['stats'].get('volume',0),'drawdown',round(result['observed_mid_drawdown'],3))
    for path, expected in sha_before.items():
        assert digest(ROOT/path)==expected
    output['strategy_files_unchanged']=True
    output['strategy_sha256']=sha_before
    (OUT/'metrics.json').write_text(json.dumps(output,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')


if __name__=='__main__':
    main()
