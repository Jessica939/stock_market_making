"""Inventory-constrained conditional replay; NOT a passive-fill backtest.

Observed historical favorable increasing fills are candidate entry opportunities.
After changing inventory/quotes those fills are not guaranteed to recur. All
book executions are sampled displayed-depth assumptions with no market impact.
"""
import gzip
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent

def ts(value):
    return datetime.fromisoformat(value).timestamp()

def valid(book, now):
    return (book.get('status')=='ok' and book.get('bids') and book.get('asks')
            and book['bids'][0][0]<book['asks'][0][0]
            and 0<=now-ts(book['book_timestamp'])<=1)

def sweep(book, sign, quantity, remove=200):
    levels = book['bids'] if sign>0 else book['asks']
    best = levels[0][0]
    remaining, cash = quantity, 0.
    for price, size in levels:
        if abs(price-best)>1.0000001:
            break
        subtract = min(remove,size)
        remove -= subtract
        take = min(remaining,size-subtract)
        cash += take*price
        remaining -= take
        if remaining==0:
            return cash/quantity
    return None

def simulate(events, hold, stop=None, cap=6, lot_cap=2, session_loss=20):
    position, cost, realized = 0, 0., 0.
    opened, deadline, book = None,None,None
    intent, stopped = None,False
    high, drawdown, peak = 0.,0.,0
    stats, exits = Counter(), []
    last_mark = None
    accepted = []
    for now, kind, item in events:
        if kind==0:
            book = item
        if position and now>=deadline and intent is None:
            intent = 'timeout'
        # Only execute/mark on new observed book events. Never reuse the same
        # displayed depth for multiple exits; close the entire net position once.
        if kind==0 and position:
            if not valid(book,now) or ts(book['book_timestamp'])<opened:
                stats['unusable_held_book']+=1
                continue
            px = sweep(book,position,abs(position))
            if px is None:
                stats['insufficient_exit_depth']+=1
                continue
            unrealized = position*(px-cost)
            equity = realized+unrealized
            last_mark = dict(time=now,equity=equity,position=position)
            high = max(high,equity)
            drawdown = max(drawdown,high-equity)
            if equity<=-session_loss:
                stopped = True
                intent = 'session_loss'
            elif stop is not None and unrealized/abs(position)<=-stop:
                intent = 'position_stop'
            if intent:
                exits.append(dict(time=now,reason=intent,quantity=abs(position),
                    side='long' if position>0 else 'short',pnl=unrealized,
                    seconds=now-opened,exit_price=px,entry_vwap=cost))
                realized += unrealized
                high = max(high,realized)
                drawdown = max(drawdown,high-realized)
                position,cost,opened,deadline,intent = 0,0.,None,None,None
                last_mark = dict(time=now,equity=realized,position=0)
            continue
        if kind!=1:
            continue
        stats['candidates']+=1
        if stopped:
            stats['rejected_session_stop']+=1
            continue
        if intent:
            stats['rejected_exit_pending']+=1
            continue
        if book is None or not valid(book,now):
            stats['rejected_no_fresh_book']+=1
            continue
        sign = item['sign']
        if position*sign<0:
            stats['rejected_opposite']+=1
            continue
        quantity = min(item['quantity'],lot_cap,cap-abs(position))
        if quantity<=0:
            stats['rejected_inventory_cap']+=1
            continue
        if sweep(book,sign,abs(position)+quantity) is None:
            stats['rejected_entry_exit_depth']+=1
            continue
        cost = (cost*abs(position)+item['price']*quantity)/(abs(position)+quantity)
        if position==0:
            opened = now
            deadline = now+hold
        position += sign*quantity
        peak = max(peak,abs(position))
        assert abs(position)<=cap
        stats['accepted']+=1
        stats['accepted_volume']+=quantity
        accepted.append(dict(time=now,trade_id=item['trade_id'],quantity=quantity))
        # Old inventory marks no longer describe the enlarged position.
        last_mark = None
    assert sum(e['quantity'] for e in exits)+abs(position)==stats['accepted_volume']
    return dict(hold_seconds=hold,stop_per_share=stop,cap=cap,lot_cap=lot_cap,
        session_loss_threshold=session_loss,realized_pnl=realized,
        residual_position=position,terminal_equity=realized if position==0 else (last_mark['equity'] if last_mark else None),
        terminal_mark=last_mark,session_stopped=stopped,peak_abs_position=peak,
        observed_drawdown=drawdown,stats=dict(stats),exits=exits,accepted=accepted)

def main():
    prior = json.loads((OUT/'exit_replay.json').read_text(encoding='utf-8'))
    output = []
    for source in prior:
        run = source['run']
        log = next((ROOT/'data/runs/baseline'/run).glob('*.jsonl'))
        rows = [json.loads(s) for s in log.read_text(encoding='utf-8').splitlines() if s.strip()]
        start,end = ts(rows[0]['observed_at_utc']),ts(rows[-1]['observed_at_utc'])
        fills = {r['trade_id']:r for r in rows if r['type']=='fill' and r['instrument']=='PHILIPS_B'}
        candidates = [e for e in source['entries'] if e['cycle']=='favorable']
        # Include every favorable entry, including ones missing a future exit
        # in earlier analysis. Entry eligibility never uses future availability.
        events = [(ts(fills[e['trade_id']]['observed_at_utc']),1,e) for e in candidates]
        truncated = []
        for directory in source['market_sources']:
            for path in sorted((ROOT/directory).glob('orderbooks*.jsonl.gz')):
                with gzip.open(path,'rt',encoding='utf-8') as f:
                    while True:
                        try:
                            line = f.readline()
                        except EOFError:
                            truncated.append(path.relative_to(ROOT).as_posix())
                            break
                        if not line:
                            break
                        b = json.loads(line)
                        now = ts(b['observed_at_utc'])
                        if b['instrument_id']=='PHILIPS_B' and start<=now<=end:
                            events.append((now,0,b))
        events.sort(key=lambda event:(event[0],event[1]))
        scenarios = [simulate(events,h) for h in (1,5,15)] + [simulate(events,15,stop=2)]
        for s in scenarios:
            if s['terminal_mark']:
                s['terminal_mark_age_seconds'] = end-s['terminal_mark']['time']
        result = dict(run=run,end_utc=rows[-1]['observed_at_utc'],truncated_files=truncated,
                      favorable_candidates=len(candidates),scenarios=scenarios)
        output.append(result)
        print(run)
        for s in scenarios:
            print({k:s[k] for k in ('hold_seconds','stop_per_share','realized_pnl','residual_position','terminal_equity','session_stopped','peak_abs_position','observed_drawdown')},s['stats'])
    (OUT/'constrained_replay.json').write_text(json.dumps(output,indent=2),encoding='utf-8')

if __name__=='__main__':
    main()
