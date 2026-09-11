"""Finite-liquidity IOC replay double. Synthetic tests do not establish an edge."""
from collections import defaultdict
from datetime import datetime, timezone
import glob
import gzip
import json
import math
import random
from types import SimpleNamespace as NS

from .market import timestamp_seconds


_RECORDING_STATUSES = {'ok', 'empty', 'ask_only', 'bid_only',
                       'no_book', 'read_error', 'invalid_book'}
_UNAVAILABLE_STATUSES = {'empty', 'no_book', 'read_error', 'invalid_book'}


class SimClock:
    def __init__(self):
        self.now = 0.0
    def monotonic(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


class ReplayExchange:
    def __init__(self, symbols, clock, fee=0.0, fill_fraction=0.5):
        self.symbols, self.clock = tuple(symbols), clock
        self.fee, self.fill_fraction = fee, fill_fraction
        self.books, self.instruments, self.last_stamp = {}, {}, {}
        self.accessible = {}
        self.positions = {i: 0 for i in symbols}
        self.cash = {i: 0.0 for i in symbols}
        self.private, self.public = defaultdict(list), defaultdict(list)
        self.next_id, self.connected = 1, True
        self.update_times, self.fills = [], []
        self.epoch = 0.0
        self.terminal_quantities = {}

    def ioc_terminal_quantity(self, iid, oid):
        """Simulator-only proof: insert_order finishes matching synchronously."""
        return self.terminal_quantities.get((iid, oid))

    def advance(self, frame):
        self.epoch = timestamp_seconds(frame['epoch'])
        if not isinstance(frame['books'], dict):
            raise ValueError('replay books must be an object')
        for iid in self.symbols:
            raw = frame['books'].get(iid)
            if raw is not None and not isinstance(raw, dict):
                raise ValueError('replay book must be an object or null')
            status = raw.get('status') if raw is not None else None
            if status is not None and status not in _RECORDING_STATUSES:
                raise ValueError('unknown replay book status')
            stamp = (timestamp_seconds(raw['timestamp'])
                     if raw is not None and raw.get('timestamp') is not None else None)
            if raw is None or stamp is None or status in _UNAVAILABLE_STATUSES:
                # A failed/missing observation invalidates even a still-fresh old
                # book. Keep exchange timestamps, never observation time, as the
                # liquidity watermark so recovery cannot refill the same book.
                self.books[iid] = None
                self.accessible[iid] = {'bids': {}, 'asks': {}}
                if stamp is not None:
                    self.last_stamp[iid] = max(stamp, self.last_stamp.get(iid, -float('inf')))
                continue
            if stamp <= self.last_stamp.get(iid, -float('inf')):
                continue  # Repeated snapshots cannot replenish replay liquidity.
            self.last_stamp[iid] = stamp
            self.books[iid] = dict(timestamp=stamp, bids=[list(x) for x in raw['bids']],
                                   asks=[list(x) for x in raw['asks']])
            self.accessible[iid] = {side: {p: int(v * self.fill_fraction) for p, v in raw[side]}
                                    for side in ('bids', 'asks')}
            self.instruments[iid] = NS(tick_size=raw.get('tick', 0.1), price_change_limit=None)

    def is_connected(self):
        return self.connected
    def disconnect(self):
        self.connected = False
    def get_tradable_instruments(self):
        return self.instruments.copy()
    def get_positions(self):
        return self.positions.copy()
    def get_positions_and_cash(self):
        return {i: {'volume': self.positions[i], 'cash': self.cash[i]} for i in self.symbols}
    def get_last_price_book(self, iid):
        return self.books.get(iid)
    def get_outstanding_orders(self, iid):
        return {}
    def poll_new_trades(self, iid):
        result, self.private[iid] = self.private[iid], []
        return result
    def poll_new_trade_ticks(self, iid):
        result, self.public[iid] = self.public[iid], []
        return result
    def delete_order(self, iid, order_id):
        self.update_times.append(self.clock.monotonic())
        return NS(success=True, error_reason=None)
    def insert_order(self, iid, *, price, volume, side, order_type):
        assert order_type == 'ioc'
        self.update_times.append(self.clock.monotonic())
        if not self.books.get(iid):
            return NS(success=False, order_id=None, error_reason='no current replay book')
        direction = 1 if side == 'bid' else -1
        if abs(self.positions[iid] + direction * volume) > 100 or volume > 200:
            return NS(success=False, order_id=None, error_reason='simulated exchange risk rejection')
        oid, self.next_id = self.next_id, self.next_id + 1
        remaining = volume
        levels = self.books[iid]['asks' if side == 'bid' else 'bids']
        for level in levels:
            p, displayed = level
            if (side == 'bid' and p > price + 1e-9) or (side == 'ask' and p < price - 1e-9):
                break
            key = 'asks' if side == 'bid' else 'bids'
            take = min(remaining, self.accessible[iid][key].get(p, 0))
            if take <= 0:
                continue
            self.positions[iid] += direction * take
            self.cash[iid] -= direction * p * take + self.fee * take
            level[1] -= take
            self.accessible[iid][key][p] -= take
            remaining -= take
            trade = NS(trade_id=len(self.fills) + 1, order_id=oid, instrument_id=iid,
                       side=side, price=p, volume=take,
                       timestamp=datetime.fromtimestamp(self.epoch, timezone.utc))
            self.private[iid].append(trade)
            self.fills.append(trade)
            if not remaining:
                break
        self.terminal_quantities[(iid, oid)] = volume - remaining
        return NS(success=True, order_id=oid, error_reason=None)


def demo_frames(symbols, seconds=1800, step=0.5, seed=7):
    """Constructed common trend, relative oscillation, noise and moving 20k walls."""
    rng = random.Random(seed)
    for n in range(int(seconds / step) + 1):
        t = n * step
        common = 100 + 0.12 * math.sin(t / 7) + 0.001 * t
        basis = 0.8 * math.sin(t / 12) + rng.gauss(0, 0.02)
        books = {}
        for j, iid in enumerate(symbols):
            center = common + (1 if j == 0 else -1) * basis / 2
            bid = round(math.floor((center - 0.1) / 0.1) * 0.1, 1)
            ask = round(bid + 0.2, 1)
            bids = [[round(bid - k * 0.1, 1), 40] for k in range(5)] + [[round(bid - 5, 1), 20000]]
            asks = [[round(ask + k * 0.1, 1), 40] for k in range(5)] + [[round(ask + 5, 1), 20000]]
            books[iid] = dict(timestamp=1800000000 + t, tick=0.1, bids=bids, asks=asks)
        yield dict(epoch=1800000000 + t, books=books)


def read_frames(pattern, symbols, tick_size=0.1):
    """Read bundles or recorder full-depth JSONL(.gz); never fabricate CSV depth."""
    paths = sorted(glob.glob(pattern))
    if not paths:
        raise ValueError('no replay files matched')
    grouped, key, observed, previous = {}, None, None, -float('inf')
    for path in paths:
        opener = gzip.open if path.endswith('.gz') else open
        with opener(path, 'rt', encoding='utf-8-sig') as source:
            for line in source:
                if not line.strip():
                    continue
                row = json.loads(line)
                if 'books' in row:
                    if grouped:
                        yield dict(epoch=observed, books=grouped)
                        grouped, key = {}, None
                    epoch = timestamp_seconds(row['epoch'])
                    if epoch < previous:
                        raise ValueError('replay must be chronological')
                    previous = epoch
                    yield row
                    continue
                iid = row.get('instrument_id')
                if iid not in symbols:
                    continue
                status = row.get('status')
                if status is not None and status not in _RECORDING_STATUSES:
                    raise ValueError('unknown recorder book status')
                raw_stamp = row.get('book_timestamp')
                stamp = timestamp_seconds(raw_stamp) if raw_stamp is not None else None
                # A recorder error still has a real observation time. Do not
                # replace its absent exchange timestamp with that wall clock.
                epoch = timestamp_seconds(row.get('observed_at_utc', stamp))
                if epoch < previous:
                    raise ValueError('replay must be chronological')
                previous = epoch
                group_key = row.get('sample_id', epoch)
                if key is not None and key != group_key:
                    yield dict(epoch=observed, books=grouped)
                    grouped = {}
                if iid in grouped:
                    raise ValueError('duplicate instrument in recorder sample')
                key, observed = group_key, epoch
                grouped[iid] = dict(timestamp=stamp, tick=row.get('tick_size', tick_size),
                                    status=status,
                                    bids=row.get('bids') or [], asks=row.get('asks') or [])
    if grouped:
        yield dict(epoch=observed, books=grouped)
