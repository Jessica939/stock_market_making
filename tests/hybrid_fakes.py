"""One shared account with genuinely concurrent MM limits and pair inventory."""
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import math
import sys
from types import SimpleNamespace as NS
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from stock_market_making.strategies.hybrid.account import SharedAccount, OwnedExchange, AccountFault, RiskBlocked, same_order_price
from stock_market_making.strategies.hybrid.engine import Hybrid
from stock_market_making.strategies.hybrid.run import load_config

A, B = 'PHILIPS_A', 'PHILIPS_B'


class Clock:
    now = 0.0
    def time(self):
        return self.now
    def sleep(self, t):
        self.now += t


class Journal:
    failed = False
    def __init__(self):
        self.rows = []
    def emit(self, kind, **fields):
        self.rows.append(dict(kind=kind, **fields))


class Exchange:
    def __init__(self, clock):
        self.clock = clock
        self.positions = {A: 0, B: 0}
        self.orders = {A: {}, B: {}}
        self.private = defaultdict(list)
        self.next_id = self.trade_id = 1
        self.sent, self.private_reads, self.public_reads = [], [], []
        self.partial_ioc = False
        self.unknown_insert = False
        self.cancel_fill = False
        self.pending_cancel = False
        self.stale = False
        self.mid = {A: 100., B: 100.}
    def is_connected(self):
        return True
    def get_positions(self):
        return self.positions.copy()
    def get_tradable_instruments(self):
        return {s: NS(tick_size=.1, price_change_limit=None) for s in (A, B)}
    def get_outstanding_orders(self, s):
        return self.orders[s].copy()
    def get_last_price_book(self, s):
        mid = round(self.mid[s], 1)
        sides = {'bid': {round(mid-.1, 1): 100}, 'ask': {round(mid+.1, 1): 100}}
        for o in self.orders[s].values():
            sides[o.side][o.price] = sides[o.side].get(o.price, 0) + o.volume
        return NS(timestamp=datetime.fromtimestamp(self.clock.time() - (5 if self.stale else 0), timezone.utc),
                  bids=[NS(price=p, volume=v) for p, v in sorted(sides['bid'].items(), reverse=True)],
                  asks=[NS(price=p, volume=v) for p, v in sorted(sides['ask'].items())])
    def poll_new_trades(self, s):
        self.private_reads.append(s)
        trades, self.private[s] = self.private[s], []
        return trades
    def poll_new_trade_ticks(self, s):
        self.public_reads.append(s)
        return []
    def fill(self, s, oid, volume=None):
        order = self.orders[s][oid]
        v = order.volume if volume is None else volume
        self.positions[s] += v if order.side == 'bid' else -v
        trade = NS(instrument_id=s, trade_id=self.trade_id, order_id=oid, volume=v,
                   side=order.side, price=order.price,
                   timestamp=datetime.fromtimestamp(self.clock.time(), timezone.utc))
        self.trade_id += 1
        self.private[s].append(trade)
        order.volume -= v
        if not order.volume:
            del self.orders[s][oid]
        return trade
    def insert_order(self, s, *, price, volume, side, order_type):
        self.sent.append((self.clock.time(), s, side, volume, order_type))
        if self.unknown_insert:
            raise TimeoutError('unknown outcome')
        oid = self.next_id
        self.next_id += 1
        self.orders[s][oid] = NS(price=price, volume=volume, side=side)
        if order_type == 'ioc':
            self.fill(s, oid, max(1, volume//2) if self.partial_ioc else volume)
            self.orders[s].pop(oid, None)
        return NS(success=True, order_id=oid)
    def delete_order(self, s, *, order_id):
        self.sent.append((self.clock.time(), s, None, 0, 'cancel'))
        if self.cancel_fill and order_id in self.orders[s]:
            self.fill(s, order_id, 1)
        if not self.pending_cancel:
            self.orders[s].pop(order_id, None)
        return NS(success=True)

