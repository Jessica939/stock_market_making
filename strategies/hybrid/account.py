"""One account, one private-trade consumer, strategy-owned virtual inventories.

Only confirmed exchange fills change a strategy's inventory. Offsetting virtual
positions are retained until each strategy trades out; no artificial transfers.
"""
from collections import defaultdict
from collections.abc import Mapping
from types import SimpleNamespace as NS
import math
import time

from stock_market_making.order_sides import side_name
from stock_market_making.strategies.common.execution import RateLimiter
from stock_market_making.strategies.common.market import UnusableBook, price_band
from stock_market_making.quote_helpers import external_price_book
from stock_market_making.strategies.baseline.cycle_signal import usable_book, CycleSettings


class AccountFault(RuntimeError):
    pass


class RiskBlocked(RuntimeError):
    """Known local rejection: no request was transmitted."""


def integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def same_order_price(actual, expected, tick):
    if (isinstance(actual, bool) or not isinstance(actual, (int, float))
            or not math.isfinite(actual) or actual <= 0):
        return False
    # Match representation noise only; never merge neighboring tradable prices.
    tolerance = min(4 * max(math.ulp(actual), math.ulp(expected)), tick / 4)
    return abs(actual - expected) <= tolerance


class SharedAccount:
    def __init__(self, raw, symbols, config, journal, clock=time.monotonic, sleep=time.sleep):
        self.raw, self.symbols, self.config = raw, tuple(symbols), config
        self.journal, self.clock, self.sleep = journal, clock, sleep
        self.owners = ('mm', 'pair')
        self.positions = {o: dict.fromkeys(self.symbols, 0) for o in self.owners}
        self.cash = {o: dict.fromkeys(self.symbols, 0.0) for o in self.owners}
        self.registry, self.seen = {}, {}
        self.queues = defaultdict(list)
        self.halted = False
        self.initialized = False
        self.entry_deadline = self.deadline = math.inf
        self.last_prices = {}
        self.pair_book_reader = None
        self.limiter = RateLimiter(config['max_updates_per_second'], clock, sleep)
        self.market_settings = CycleSettings(max_spread_ticks=config['mm_max_spread_ticks'])

    def fault(self, reason):
        self.halted = True
        raise AccountFault(reason)

    def initialize(self, restored=None):
        # Startup cannot assign inherited inventory to either strategy honestly.
        positions = self.raw.get_positions()
        if not isinstance(positions, Mapping) or any(not integer(q) for q in positions.values()):
            self.fault('Invalid startup positions')
        if any(s not in positions for s in self.symbols):
            self.fault('Missing startup position')
        if any(q for s, q in positions.items() if s not in self.symbols):
            self.fault('Unmanaged account exposure at startup')
        if restored is None:
            if any(positions.values()):
                self.fault('No saved ownership state for nonzero inventory; inherited positions remain untouched')
        else:
            expected = {s: sum(restored['positions'][o][s] for o in self.owners) for s in self.symbols}
            if any(positions[s] != expected[s] for s in self.symbols):
                self.fault(f'Saved ownership does not match exchange: saved={expected}, actual={positions}')
            self.positions = {o: dict(restored['positions'][o]) for o in self.owners}
            self.cash = {o: dict(restored['cash'][o]) for o in self.owners}
        for s in self.raw.get_tradable_instruments():
            if self.raw.get_outstanding_orders(s):
                self.fault('Hybrid requires no inherited outstanding orders at startup')
        self.initialized = True
        self.audit()

    def drain(self):
        for s in self.symbols:
            trades = self.raw.poll_new_trades(s)
            if not isinstance(trades, (tuple, list)):
                self.fault('Private trade stream unavailable')
            for t in trades:
                oid, tid, v = t.order_id, t.trade_id, t.volume
                side = side_name(t.side)
                if (not all(integer(x) and x >= 0 for x in (oid, tid))
                        or not integer(v) or v <= 0 or side not in ('bid', 'ask')
                        or not math.isfinite(t.price) or t.price <= 0
                        or getattr(t, 'instrument_id', s) != s):
                    self.fault('Malformed private fill')
                signature = (oid, side, t.price, v)
                if (s, tid) in self.seen:
                    if self.seen[s, tid] != signature:
                        self.fault('Conflicting duplicate fill')
                    continue
                order = self.registry.get((s, oid))
                if (order is None or side != order['side'] or order['filled'] + v > order['volume']
                        or (t.price > order['price'] + 1e-9 if side == 'bid'
                            else t.price < order['price'] - 1e-9)):
                    self.fault('Unattributable or inconsistent fill')
                self.seen[s, tid] = signature
                owner = order['owner']
                sign = 1 if side == 'bid' else -1
                order['filled'] += v
                self.positions[owner][s] += sign * v
                self.cash[owner][s] -= sign * v * t.price + v * self.config['fee_per_lot']
                if owner == 'pair':
                    self.queues[owner, s].append(NS(
                        instrument_id=s, trade_id=tid, order_id=oid, volume=v,
                        side=side, price=t.price, timestamp=getattr(t, 'timestamp', None)))
                self.journal.emit('owned_fill', owner=owner, instrument=s, order_id=oid,
                                  trade_id=tid, price=t.price, side=side, volume=v)

    def audit(self):
        if not self.initialized:
            self.fault('Account has not been initialized')
        until = self.clock() + self.config['settlement_seconds']
        while True:
            self.drain()
            actual = self.raw.get_positions()
            if (not isinstance(actual, Mapping) or any(s not in actual for s in self.symbols)
                    or any(not integer(q) for q in actual.values())):
                self.fault('Invalid actual account positions')
            if any(q for s, q in actual.items() if s not in self.symbols):
                self.fault('Unmanaged account exposure')
            expected = {s: sum(self.positions[o][s] for o in self.owners) for s in self.symbols}
            if all(actual[s] == expected[s] for s in self.symbols):
                return expected
            if self.clock() >= until:
                self.fault('Actual positions do not reconcile with strategy-owned fills')
            self.sleep(.05)

    def orders(self, s, owner=None):
        raw = self.raw.get_outstanding_orders(s)
        if not isinstance(raw, Mapping):
            self.fault('Outstanding orders unavailable')
        result = {}
        for oid, order in list(raw.items()):
            entry = self.registry.get((s, oid))
            raw_side, price, volume = (getattr(order, key, None) for key in ('side', 'price', 'volume'))
            try:
                side = side_name(raw_side)
            except ValueError:
                side = None
            reason = None
            if not integer(oid) or oid < 0 or getattr(order, 'order_id', oid) != oid:
                reason = 'invalid order id'
            elif getattr(order, 'instrument_id', s) != s:
                reason = 'instrument mismatch'
            elif entry is None:
                reason = 'order id not registered by this process'
            elif side != entry['side']:
                reason = 'side mismatch'
            elif not same_order_price(price, entry['price'], entry['tick']):
                reason = 'price mismatch'
            elif not integer(volume) or not 0 < volume <= entry['volume']:
                reason = 'invalid remaining volume'
            if reason:
                self.halted = True
                actual = dict(side=repr(side), price=repr(price), volume=repr(volume),
                              order_id=repr(getattr(order, 'order_id', oid)),
                              instrument_id=repr(getattr(order, 'instrument_id', s)))
                self.journal.emit('resting_order_mismatch', instrument=s, order_id=repr(oid),
                                  reason=reason, expected=entry, actual=actual)
                self.fault(f'Unowned or inconsistent resting order: {s}/{oid!r}: {reason}; '
                           f'expected={entry!r}; actual={actual!r}')
            if owner is None or entry['owner'] == owner:
                # SDK OrderStatus objects can mutate during subsequent reads.
                result[oid] = NS(order_id=oid, instrument_id=s, side=side, price=price, volume=volume)
        return result

    def external_book(self, s):
        book = self.raw.get_last_price_book(s)
        if isinstance(book, dict):
            from datetime import datetime, timezone
            book = NS(timestamp=datetime.fromtimestamp(book['timestamp'], timezone.utc),
                      bids=[NS(price=p, volume=v) for p, v in book['bids']],
                      asks=[NS(price=p, volume=v) for p, v in book['asks']])
        instruments = self.raw.get_tradable_instruments()
        if s not in instruments:
            return None
        return external_price_book(book, self.orders(s), instruments[s].tick_size)

    def cancel(self, owner, s, oid):
        entry = self.registry.get((s, oid))
        if entry is None or entry['owner'] != owner:
            self.fault('Attempt to cancel another strategy order')
        self.limiter.acquire(emergency=True)
        try:
            response = self.raw.delete_order(s, order_id=oid)
        except Exception as exc:
            self.fault('Unknown cancellation outcome: ' + str(exc))
        if getattr(response, 'success', None) is not True:
            self.fault('Cancellation rejected or unacknowledged')
        self.journal.emit('owned_cancel', owner=owner, instrument=s, order_id=oid)
        return response

    def cancel_owner(self, owner):
        for s in self.symbols:
            for oid in list(self.orders(s, owner)):
                self.cancel(owner, s, oid)
        until = self.clock() + self.config['cancel_confirmation_seconds']
        while any(self.orders(s, owner) for s in self.symbols):
            if self.clock() >= until:
                self.fault('Cancellation has not been confirmed')
            self.sleep(.05)
        self.audit()  # Late fills remain assigned to the original owner.

    def cancel_owner_symbol(self, owner, symbol):
        """Cancel one owner's orders on one instrument and reconcile late fills."""
        if owner not in self.owners or symbol not in self.symbols:
            self.fault('Invalid owner/symbol cancellation scope')
        for oid in list(self.orders(symbol, owner)):
            self.cancel(owner, symbol, oid)
        until = self.clock() + self.config['cancel_confirmation_seconds']
        while self.orders(symbol, owner):
            if self.clock() >= until:
                self.fault('Cancellation has not been confirmed')
            self.sleep(.05)
        self.audit()

    def insert(self, owner, s, *, price, volume, side, order_type):
        if (owner not in self.owners or s not in self.symbols or side not in ('bid', 'ask')
                or not integer(volume) or volume <= 0):
            self.fault('Invalid order intent')
        if order_type != ('limit' if owner == 'mm' else 'ioc'):
            self.fault('Incorrect order type for strategy')
        self.limiter.wait()
        if self.halted or self.journal.failed:
            self.fault('All inserts disabled after an account/execution fault')
        actual = self.audit()
        reducing = (self.positions[owner][s] * (1 if side == 'bid' else -1) < 0
                    and volume <= abs(self.positions[owner][s]))
        if self.clock() >= self.deadline or (self.clock() >= self.entry_deadline
                                             and (owner == 'mm' or not reducing)):
            raise RiskBlocked('Trading deadline')
        all_orders = {i: self.orders(i) for i in self.symbols}
        if owner == 'pair' and self.orders(s, 'mm'):
            raise RiskBlocked('Cancel same-instrument MM orders before IOC execution')
        sign = 1 if side == 'bid' else -1
        own_pending = sum(o.volume for oid, o in all_orders[s].items()
                          if o.side == side and self.registry[s, oid]['owner'] == owner)
        own_limit = self.config['mm_position_limit' if owner == 'mm' else 'pair_position_limit']
        if sign * self.positions[owner][s] + own_pending + volume > own_limit:
            raise RiskBlocked('Strategy inventory allocation')
        pending = sum(o.volume for o in all_orders[s].values() if o.side == side)
        if sign * actual[s] + pending + volume > self.config['position_limit']:
            raise RiskBlocked('Aggregate position plus same-side resting exposure')
        total_pending = sum(o.volume for orders in all_orders.values() for o in orders.values() if o.side == side)
        if sign * sum(actual.values()) + total_pending + volume > self.config['max_net_lots']:
            raise RiskBlocked('Aggregate net exposure including resting orders')
        if sum(o.volume for o in all_orders[s].values()) + volume > self.config['max_outstanding_volume']:
            raise RiskBlocked('Aggregate outstanding volume')
        instruments = self.raw.get_tradable_instruments()
        if s not in instruments:
            raise RiskBlocked('Instrument not tradable')
        tick = instruments[s].tick_size
        if not math.isfinite(price) or price <= 0 or abs(price / tick - round(price / tick)) > 1e-6:
            raise RiskBlocked('Invalid tick price')
        band = price_band(instruments[s], self.last_prices.get(s))
        if band is None or not band[0] <= price <= band[1]:
            raise RiskBlocked('Price change limit')
        if owner == 'mm':
            book = self.external_book(s)
            if not usable_book(book, tick, self.wall(), self.market_settings):
                raise RiskBlocked('Stale/invalid MM execution book')
            if (price >= book.asks[0].price if side == 'bid' else price <= book.bids[0].price):
                raise RiskBlocked('MM quote would be marketable')
        else:
            # The shared limiter may have slept after Executor planned its IOC.
            # Recheck fresh cleaned executable depth without changing its intent.
            if self.pair_book_reader is None:
                raise RiskBlocked('Pair execution market reader unavailable')
            try:
                book = self.pair_book_reader(s, reducing=reducing,
                                             reducing_side=side if reducing else None)
            except UnusableBook as exc:
                raise RiskBlocked(str(exc)) from exc
            levels = book['asks' if side == 'bid' else 'bids']
            available = sum(v for p, v in levels
                            if (p <= price + 1e-9 if side == 'bid' else p >= price - 1e-9))
            if available < volume:
                raise RiskBlocked('IOC depth moved after shared rate-limit wait')
        # Last check after potentially slow account/market reads.
        if self.clock() >= self.deadline or (self.clock() >= self.entry_deadline
                                             and (owner == 'mm' or not reducing)):
            raise RiskBlocked('Trading deadline after market refresh')
        try:
            if not self.limiter.try_acquire():
                raise RiskBlocked('Update budget exhausted before transmission')
            response = self.raw.insert_order(s, price=price, volume=volume, side=side, order_type=order_type)
        except RiskBlocked:
            raise
        except Exception as exc:
            self.fault('Unknown insertion outcome: ' + str(exc))
        success, oid = getattr(response, 'success', None), getattr(response, 'order_id', None)
        if type(success) is not bool or (not success and oid is not None):
            self.fault('Invalid insertion acknowledgment')
        if success:
            if not integer(oid) or oid < 0 or (s, oid) in self.registry:
                self.fault('Missing or reused accepted order id')
            self.registry[s, oid] = dict(owner=owner, side=side, price=price, tick=tick,
                                        volume=volume, filled=0)
        self.journal.emit('owned_order', owner=owner, instrument=s, side=side, price=price,
                          volume=volume, order_type=order_type, success=success, order_id=oid)
        if not success and owner == 'mm':
            raise RiskBlocked(str(getattr(response, 'error_reason', 'order rejected')))
        return response

    wall = staticmethod(time.time)


class OwnedExchange:
    """Narrow execution view; never exposes another strategy's orders or fills."""
    def __init__(self, account, owner):
        self.account, self.owner = account, owner
    def is_connected(self):
        return self.account.raw.is_connected()
    def get_positions(self):
        self.account.audit()
        return self.account.positions[self.owner].copy()
    def get_positions_and_cash(self):
        self.account.audit()
        return {s: dict(volume=q, cash=self.account.cash[self.owner][s])
                for s, q in self.account.positions[self.owner].items()}
    def get_outstanding_orders(self, s):
        return self.account.orders(s, self.owner)
    def poll_new_trades(self, s):
        self.account.drain()
        return self.account.queues.pop((self.owner, s), [])
    def delete_order(self, s, *, order_id):
        return self.account.cancel(self.owner, s, order_id)
    def insert_order(self, s, **order):
        try:
            return self.account.insert(self.owner, s, **order)
        except RiskBlocked as exc:
            self.account.journal.emit('risk_blocked', owner=self.owner, instrument=s, reason=str(exc))
            if self.owner == 'mm':
                raise
            return NS(success=False, order_id=None, error_reason=str(exc))


class MarketView:
    """Public-data owner; Feed drains public ticks, recording receives a copy."""
    def __init__(self, account):
        self.account = account
    def get_tradable_instruments(self):
        return self.account.raw.get_tradable_instruments()
    def get_last_price_book(self, s):
        return self.account.external_book(s)
    def poll_new_trade_ticks(self, s):
        return self.account.raw.poll_new_trade_ticks(s)
