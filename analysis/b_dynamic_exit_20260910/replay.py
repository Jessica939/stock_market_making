"""Offline, frozen-signal stateful execution scenarios; not exchange emulation.

No client imports. Public trades drive hypothetical fills, which change inventory
and subsequent quotes. Unknown queues, latency and accessible depth are explicit
scenario assumptions. No historical private fills are injected as new fills.
"""
from collections import Counter
from dataclasses import dataclass
import heapq
import importlib.util
import math
from pathlib import Path
import sys
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[2]


def pure_module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cycle = pure_module('exit_replay_cycle', 'strategies/baseline/cycle_signal.py')
protection = pure_module('exit_replay_protection', 'strategies/baseline/quote_protection.py')
guard = pure_module('exit_replay_guard', 'analysis/b_exit_guard_20260910/guard.py')


def quote_for_position(logged, position, config, *, price_shift=False, protected=True):
    """Reconstruct the inventory-dependent formula, keep past market inputs only."""
    tick = .1
    fair = logged['fair_value'] - logged.get('cycle_shift', 0.)
    adjustment = config['inventory_skew_ticks'] * tick * position / config['inventory_scale']
    center = fair - adjustment
    half = logged['half_spread']
    buy = min(math.ceil(config['order_volume'] * max(0., 1-max(position, 0)/config['soft_limit'])-1e-9),
              max(0, config['position_limit']-position))
    sell = min(math.ceil(config['order_volume'] * max(0., 1-max(-position, 0)/config['soft_limit'])-1e-9),
               max(0, config['position_limit']+position))
    q = dict(fair_value=fair, center=center, half_spread=half, inventory_adjustment=adjustment,
             bid_price=round(math.floor((center-half)/tick+1e-9)*tick, 10),
             ask_price=round(math.ceil((center+half)/tick-1e-9)*tick, 10),
             buy_volume=buy, sell_volume=sell)
    book = NS(bids=[NS(price=logged['best_bid'])], asks=[NS(price=logged['best_ask'])])
    q = cycle.apply_cycle_quote(q, book, position, tick, 'PHILIPS_B', logged['cycle'],
                               cycle.CycleSettings(**config['cycle_settings']), config['soft_limit'],
                               apply_price_shift=price_shift)
    if protected:
        q = protection.protect_quote(q, book, position, tick, 'PHILIPS_B',
                                    protection.ProtectionSettings(**config['b_protection']))
    q.update(instrument='PHILIPS_B', position=position,
             best_bid=logged['best_bid'], best_ask=logged['best_ask'])
    return q


@dataclass(frozen=True)
class Scenario:
    policy: str = 'baseline'
    exit_guard: bool = False
    timeout: float | None = None
    queue: str = 'displayed'
    latency: float = .05
    depth_removal: int = 200
    depth_fraction: float = .5
    fair_mode: str = 'depth'
    entry_extra_ticks: int = 0


class Replay:
    def __init__(self, config, scenario, start, end):
        self.config, self.scenario, self.start, self.end = config, scenario, start, end
        self.position, self.cash = 0, 0.
        self.opened = None
        self.episode_start_cash = 0.
        self.episode_id = 0
        self.exiting = False
        self.orders = {}
        self.book = None
        self.book_stamp = -math.inf
        self.available = {'bids': {}, 'asks': {}}
        self.stats = Counter()
        self.fills, self.quotes, self.episodes, self.marks = [], [], [], []
        self.pending = []
        self.serial = 0
        self.peak_position = 0
        self.high, self.drawdown = 0., 0.
        self.last_event = start
        self.inventory_seconds = 0.
        self.last_mid = self.last_mid_stamp = None

    def risk(self):
        assert abs(self.position) <= 100
        assert self.position + sum(o['volume'] for s, o in self.orders.items() if s == 'bid') <= 100
        assert self.position - sum(o['volume'] for s, o in self.orders.items() if s == 'ask') >= -100
        assert sum(o['volume'] for o in self.orders.values()) <= 200

    def fill(self, now, side, price, volume, reason, context=None):
        assert type(volume) is int and 0 < volume <= 200
        before = self.position
        sign = 1 if side == 'bid' else -1
        # A resting exit may become an entry after other fills; do not impose
        # fictitious exchange atomic reduce-only semantics on passive orders.
        cash_before = self.cash
        self.position += sign * volume
        self.cash -= sign * price * volume
        self.peak_position = max(self.peak_position, abs(self.position))
        self.fills.append(dict(t=now, side=side, price=price, volume=volume,
                               before=before, after=self.position, reason=reason,
                               context=context))
        self.stats['volume'] += volume
        self.stats['fills'] += 1
        self.stats[reason+'_volume'] += volume
        if before == 0:
            self.opened = now
            self.episode_start_cash = cash_before
            self.episode_id += 1
        elif before * self.position < 0:
            # Split a crossing fill's cash at zero for episode bookkeeping.
            flat_cash = cash_before - sign*price*abs(before)
            self.episodes.append(dict(t=now, seconds=now-self.opened,
                                      pnl=flat_cash-self.episode_start_cash, crossed_zero=True))
            self.opened = now
            self.episode_start_cash = flat_cash
            self.episode_id += 1
            self.exiting = False
        elif self.position == 0:
            self.episodes.append(dict(t=now, seconds=now-self.opened,
                                      pnl=self.cash-self.episode_start_cash, crossed_zero=False))
            self.opened = None
            self.exiting = False
        self.risk()

    def fresh(self, now):
        return bool(self.book and self.book.get('status') == 'ok' and self.book.get('bids')
                    and self.book.get('asks') and 0 <= now-self.book['stamp'] <= 1
                    and self.book['bids'][0][0] < self.book['asks'][0][0])

    def observe_book(self, now, book):
        self.book = book
        if not self.fresh(now):
            self.available = {'bids': {}, 'asks': {}}
            self.stats['unusable_books'] += 1
            return
        if book['stamp'] < self.book_stamp:
            self.available = {'bids': {}, 'asks': {}}
            self.book = None
            self.stats['reversed_books'] += 1
            return
        self.last_mid = (book['bids'][0][0]+book['asks'][0][0])/2
        self.last_mid_stamp = book['stamp']
        if book['stamp'] <= self.book_stamp:
            self.stats['repeated_books'] += 1
            return  # A repeated snapshot must not refill consumed liquidity.
        self.book_stamp = book['stamp']
        for side in ('bids', 'asks'):
            remove = self.scenario.depth_removal
            self.available[side] = {}
            for price, volume in book[side]:
                take = min(remove, volume)
                remove -= take
                self.available[side][round(price, 8)] = math.floor((volume-take)*self.scenario.depth_fraction)

    def sweep(self, now, side, quantity, limit, reason):
        if not self.fresh(now):
            self.stats['sweep_no_fresh_book'] += 1
            return 0
        key = 'asks' if side == 'bid' else 'bids'
        levels = self.book[key]
        best = levels[0][0]
        remaining = quantity
        for price, _ in levels:
            if abs(price-best) > 1.0000001:
                break
            if side == 'bid' and price > limit+1e-8 or side == 'ask' and price < limit-1e-8:
                break
            pkey = round(price, 8)
            take = min(remaining, self.available[key].get(pkey, 0))
            if take:
                self.available[key][pkey] -= take
                remaining -= take
                self.fill(now, side, price, int(take), reason)
            if not remaining:
                break
        return quantity-remaining

    def mark(self, now):
        if not self.fresh(now):
            return
        mid = (self.book['bids'][0][0]+self.book['asks'][0][0])/2
        equity = self.cash+self.position*mid
        self.high = max(self.high, equity)
        self.drawdown = max(self.drawdown, self.high-equity)
        self.marks.append(dict(t=now, equity=equity, position=self.position))

    def schedule(self, now, payload):
        self.serial += 1
        heapq.heappush(self.pending, (now+self.scenario.latency, self.serial, payload))

    def decision(self, now, logged):
        if logged is None:
            self.schedule(now, dict(kind='cancel'))
            return
        due = (self.position != 0 and self.scenario.timeout is not None
               and now-self.opened >= self.scenario.timeout)
        if self.position and (self.exiting or due):
            self.exiting = True
            self.schedule(now, dict(kind='exit', episode=self.episode_id))
            self.stats['exit_decisions'] += 1
            return
        pricing = dict(logged)
        base_fair = logged['fair_value']-logged.get('cycle_shift',0.)
        if self.scenario.fair_mode == 'mid':
            base_fair = (logged['best_bid']+logged['best_ask'])/2
        elif self.scenario.fair_mode == 'clipped':
            base_fair = min(logged['best_ask'],max(logged['best_bid'],base_fair))
        elif self.scenario.fair_mode != 'depth':
            raise ValueError('unknown fair mode')
        pricing['fair_value'] = base_fair+logged.get('cycle_shift',0.)
        q = quote_for_position(pricing, self.position, self.config)
        before = (q['bid_price'], q['ask_price'])
        if self.scenario.exit_guard:
            q = guard.apply(q)
        if self.position >= 0:
            q['bid_price'] = round(q['bid_price']-.1*self.scenario.entry_extra_ticks,10)
        if self.position <= 0:
            q['ask_price'] = round(q['ask_price']+.1*self.scenario.entry_extra_ticks,10)
        q['_decision_time'] = now
        changed = before != (q['bid_price'], q['ask_price'])
        self.stats['guard_changed_quotes'] += changed
        self.quotes.append(dict(t=now, position=self.position, bid=q['bid_price'], ask=q['ask_price'],
                                buy=q['buy_volume'], sell=q['sell_volume'], changed=changed,
                                fair=q['fair_value'], center=q['center'],
                                mid=(logged['best_bid']+logged['best_ask'])/2))
        self.schedule(now, dict(kind='quote', q=q))

    def capacity(self, side, q):
        soft = self.config['soft_limit']
        room = (0 if self.position >= soft else 100-self.position) if side == 'bid' else (
                0 if self.position <= -soft else 100+self.position)
        if q.get('reduce_only'):
            room = min(room, -self.position if side == 'bid' else self.position)
        return max(0, min(q['buy_volume' if side == 'bid' else 'sell_volume'], room))

    def activate(self, now, payload):
        if payload['kind'] == 'cancel':
            self.stats['cancelled'] += len(self.orders)
            self.orders.clear()
            return
        if payload['kind'] == 'exit':
            # A timeout tied to a closed episode must not liquidate a new one.
            if self.episode_id != payload['episode'] or not self.position:
                self.stats['expired_exit_requests'] += 1
                return
            self.stats['cancelled'] += len(self.orders)
            self.orders.clear()
            side = 'ask' if self.position > 0 else 'bid'
            volume = abs(self.position)
            done = self.sweep(now, side, volume, -math.inf if side == 'ask' else math.inf, 'timeout')
            self.stats['timeout_attempts'] += 1
            if done < volume:
                self.stats['timeout_partial_or_missing_depth'] += 1
            return
        q = payload['q']
        if self.exiting:
            return  # No stale pre-timeout quote may reopen exposure while exiting.
        for side in ('bid', 'ask'):
            old = self.orders.get(side)
            capacity = self.capacity(side, q)
            if old and (abs(old['price']-q[side+'_price']) > 1e-8 or old['volume'] > capacity or capacity == 0):
                self.orders.pop(side)
                self.stats['cancelled'] += 1
        for side in ('bid', 'ask'):
            if side in self.orders:
                self.stats['retained'] += 1
                continue  # Preserve queue; don't top up partial fills.
            volume = self.capacity(side, q)
            if not volume:
                continue
            if not self.fresh(now):
                self.stats['insert_no_fresh_book'] += 1
                continue
            price = q[side+'_price']
            opposite = self.orders.get('ask' if side == 'bid' else 'bid')
            if opposite and (price >= opposite['price'] if side == 'bid' else price <= opposite['price']):
                self.stats['self_cross_prevented'] += 1
                continue
            # A passive decision may become marketable during order latency.
            done = self.sweep(now, side, volume, price, 'marketable_insert')
            volume -= done
            if not volume:
                continue
            queue = 0
            if self.scenario.queue == 'displayed' and self.fresh(now):
                levels = self.book['bids' if side == 'bid' else 'asks']
                queue = sum(v for p, v in levels if abs(p-price) <= 1e-8)
            self.orders[side] = dict(price=price, volume=volume, queue=queue, placed=now,
                                    context=dict(decision_t=q.get('_decision_time',now),placed_t=now,
                                        fair=q.get('fair_value'), center=q.get('center'),
                                        mid=(q.get('best_bid',0)+q.get('best_ask',0))/2))
            self.stats['inserted'] += 1
        self.risk()

    def trade(self, now, trade):
        # Remove consumed historical depth before a later IOC can reuse it.
        key = 'asks' if trade['side'] == 'bid' else 'bids'
        pkey = round(trade['price'], 8)
        if self.book and self.book['stamp'] < now:
            self.available[key][pkey] = max(0, self.available[key].get(pkey, 0)-trade['volume'])
        if trade.get('own_aggressive'):
            self.stats['own_aggressive_prints_excluded'] += 1
            return
        side = 'ask' if trade['side'] == 'bid' else 'bid'
        order = self.orders.get(side)
        if not order or order['placed'] >= now:
            return
        signed = (trade['price']-order['price']) * (1 if side == 'ask' else -1)
        if signed < -1e-8:
            return
        volume = trade['volume']
        if abs(signed) <= 1e-8:
            if self.scenario.queue == 'through':
                return
            ahead = min(order['queue'], volume)
            order['queue'] -= ahead
            volume -= ahead
        else:
            order['queue'] = 0
        take = min(order['volume'], volume)
        if take:
            order['volume'] -= take
            if not order['volume']:
                self.orders.pop(side)
            self.fill(now, side, order['price'], int(take), 'passive',context=order.get('context'))

    def advance_time(self, now):
        assert now >= self.last_event
        self.inventory_seconds += abs(self.position)*(now-self.last_event)
        self.last_event = now

    def run(self, events):
        for now, kind, item in events:
            while self.pending and self.pending[0][0] < now:
                when, _, payload = heapq.heappop(self.pending)
                self.advance_time(when)
                self.activate(when, payload)
                self.mark(when)
            self.advance_time(now)
            if kind == 'book':
                self.observe_book(now, item)
            elif kind == 'trade':
                self.trade(now, item)
            else:
                self.decision(now, item)
            self.mark(now)
        while self.pending and self.pending[0][0] <= self.end:
            when, _, payload = heapq.heappop(self.pending)
            self.advance_time(when)
            self.activate(when, payload)
            self.mark(when)
        self.advance_time(self.end)
        self.risk()
        assert sum((1 if f['side'] == 'bid' else -1)*f['volume'] for f in self.fills) == self.position
        assert abs(sum((1 if f['side'] == 'ask' else -1)*f['price']*f['volume'] for f in self.fills)-self.cash) < 1e-7
        terminal_equity = self.cash + self.position*self.last_mid if self.last_mid is not None else None
        # Final equity is a mark, never label an open inventory as realized PnL.
        return dict(cash=self.cash, final_position=self.position,
                    terminal_equity=terminal_equity,
                    terminal_mark_age=self.end-self.last_mid_stamp if self.last_mid_stamp is not None else None,
                    peak_abs_position=self.peak_position, observed_mid_drawdown=self.drawdown,
                    average_abs_inventory=self.inventory_seconds/(self.end-self.start),
                    completed_episodes=len(self.episodes), episode_pnl=sum(e['pnl'] for e in self.episodes),
                    stats=dict(self.stats),
                    fee_sensitivity={str(fee):terminal_equity-fee*self.stats['volume'] if terminal_equity is not None else None
                                     for fee in (0., .01, .05)})
