"""Append-only trading diagnostics using an existing exchange connection."""

import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def midpoint(book):
    if not book or not book.bids or not book.asks:
        return None
    bid, ask = book.bids[0].price, book.asks[0].price
    if not (math.isfinite(bid) and math.isfinite(ask) and 0 < bid < ask):
        return None
    return (bid + ask) / 2


class TradeLogger:
    """Record fills, account snapshots, and future-price markouts."""

    def __init__(self, exchange, instrument_ids, log_dir,
                 horizons=(1, 3, 5, 15, 30, 60), *,
                 markout_timeout_seconds=None, markout_grace_seconds=30):
        self.exchange = exchange
        self.instrument_ids = tuple(instrument_ids)
        self.horizons = tuple(sorted(set(horizons)))
        if (not self.horizons or any(not isinstance(h, (int, float))
                                    or not math.isfinite(h) or h <= 0
                                    for h in self.horizons)):
            raise ValueError('horizons must contain positive finite seconds')
        if (not isinstance(markout_grace_seconds, (int, float))
                or not math.isfinite(markout_grace_seconds) or markout_grace_seconds < 0):
            raise ValueError('markout_grace_seconds must be finite and nonnegative')
        minimum_timeout = max(self.horizons) + markout_grace_seconds
        if markout_timeout_seconds is None:
            markout_timeout_seconds = minimum_timeout
        if (not isinstance(markout_timeout_seconds, (int, float))
                or not math.isfinite(markout_timeout_seconds)
                or markout_timeout_seconds < minimum_timeout):
            raise ValueError('markout_timeout_seconds must be >= max(horizons) + grace')
        self.markout_timeout_seconds = markout_timeout_seconds
        self.markout_grace_seconds = markout_grace_seconds
        self.pending = []
        self.baselines = {}
        self.closed = False
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        self.path = directory / f'trading_{stamp}_{uuid4().hex[:8]}.jsonl'
        self.file = self.path.open('x', encoding='utf-8', buffering=1)
        self.write('session', instruments=self.instrument_ids,
                   horizons_seconds=self.horizons,
                   markout_timeout_seconds=self.markout_timeout_seconds,
                   markout_grace_seconds=self.markout_grace_seconds,
                   markout_timeout_clock='monotonic elapsed since fill detection',
                   markout_clock='exchange book timestamp minus trade timestamp',
                   snapshot_note='Reads are sequential, not an atomic snapshot; mids may include own orders.')

    def write(self, kind, **fields):
        self.file.write(json.dumps(
            {'type': kind, 'observed_at_utc': datetime.now(timezone.utc).isoformat(),
             **fields}, ensure_ascii=False, default=str, allow_nan=False) + '\n')

    def event(self, action, **fields):
        self.write('event', action=action, **fields)

    def _read(self, method, *args):
        try:
            return getattr(self.exchange, method)(*args)
        except Exception as error:
            self.event('read_error', method=method, args=args, error=str(error))
            return None

    def sample(self):
        """Poll through the strategy's connection and update pending markouts."""
        for iid in self.instrument_ids:
            trades = self._read('poll_new_trades', iid)
            for trade in trades or ():
                fields = dict(instrument=iid, trade_id=trade.trade_id,
                              order_id=trade.order_id, side=trade.side,
                              price=trade.price, volume=trade.volume,
                              trade_timestamp=trade.timestamp)
                self.write('fill', **fields)
                if not isinstance(trade.timestamp, datetime) or trade.side not in ('bid', 'ask'):
                    self.event('markout_unavailable', **fields,
                               reason='invalid trade timestamp or side')
                    continue
                self.pending.append(dict(fields=fields, received=time.monotonic(),
                                         horizons=list(self.horizons)))

        books = {iid: self._read('get_last_price_book', iid) for iid in self.instrument_ids}
        holdings = self._read('get_positions_and_cash')
        self.write('account', pnl_last_trade=self._read('get_pnl'))
        for iid, book in books.items():
            mid = midpoint(book)
            holding = holdings.get(iid) if holdings is not None else None
            quantity = holding.get('volume') if holding is not None else None
            cash = holding.get('cash') if holding is not None else None
            pnl = cash + quantity * mid if None not in (cash, quantity, mid) else None
            if pnl is not None:
                self.baselines.setdefault(iid, pnl)
            self.write('snapshot', instrument=iid,
                       book_timestamp=getattr(book, 'timestamp', None),
                       best_bid=book.bids[0].price if book and book.bids else None,
                       best_ask=book.asks[0].price if book and book.asks else None,
                       mid=mid, position=quantity, cash=cash, pnl_mid=pnl,
                       pnl_mid_change=pnl - self.baselines[iid] if pnl is not None else None)

        keep = []
        for item in self.pending:
            fields = item['fields']
            book = books.get(fields['instrument'])
            mid = midpoint(book)
            age = None
            if mid is not None and isinstance(getattr(book, 'timestamp', None), datetime):
                try:
                    age = (book.timestamp - fields['trade_timestamp']).total_seconds()
                except TypeError:
                    pass
            for horizon in item['horizons'][:]:
                if age is not None and age >= horizon:
                    edge = (1 if fields['side'] == 'bid' else -1) * (mid - fields['price'])
                    self.write('markout', **fields, horizon_seconds=horizon,
                               actual_seconds=age, lateness_seconds=age - horizon,
                               status='ok' if age - horizon <= 1 else 'late',
                               book_timestamp=book.timestamp, mid=mid,
                               per_share=edge, total=edge * fields['volume'])
                    item['horizons'].remove(horizon)
            if (item['horizons'] and time.monotonic() - item['received']
                    > self.markout_timeout_seconds):
                self.write('markout_missing', **fields, horizons_seconds=item['horizons'],
                           reason=('no usable future book within '
                                   f'{self.markout_timeout_seconds:g} seconds of detection'))
            elif item['horizons']:
                keep.append(item)
        self.pending = keep

    def close(self):
        if self.closed:
            return
        try:
            for item in self.pending:
                self.write('markout_missing', **item['fields'],
                           horizons_seconds=item['horizons'],
                           reason='session ended before observation')
            self.write('session_end')
        finally:
            self.pending.clear()
            self.file.close()
            self.closed = True
