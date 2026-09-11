"""Coordinate baseline-refine LIMIT quotes and stale B IOC trades safely."""
import importlib
import math
import time

from stock_market_making.strategies.hybrid.account import (
    SharedAccount, OwnedExchange, RiskBlocked,
)
from stock_market_making.strategies.stale_quote_sniping.engine import Engine as StaleEngine

A, B = 'PHILIPS_A', 'PHILIPS_B'


class StaleView:
    """Stale-owned inventory plus a shared, own-order-free public market view."""

    def __init__(self, account):
        self.account = account
        self.owned = OwnedExchange(account, 'pair')

    def is_connected(self):
        return self.account.raw.is_connected()

    def get_positions(self):
        return self.owned.get_positions()

    def get_positions_and_cash(self):
        return self.owned.get_positions_and_cash()

    def get_outstanding_orders(self, instrument_id):
        return self.owned.get_outstanding_orders(instrument_id)

    def poll_new_trades(self, instrument_id):
        return self.owned.poll_new_trades(instrument_id)

    def delete_order(self, instrument_id, *, order_id):
        return self.owned.delete_order(instrument_id, order_id=order_id)

    def insert_order(self, instrument_id, **order):
        return self.owned.insert_order(instrument_id, **order)

    def get_tradable_instruments(self):
        return self.account.raw.get_tradable_instruments()

    def get_last_price_book(self, instrument_id):
        return self.account.external_book(instrument_id)

    def poll_new_trade_ticks(self, instrument_id):
        ticks = self.account.raw.poll_new_trade_ticks(instrument_id)
        for trade in ticks:
            price = getattr(trade, 'price', None)
            if (isinstance(price, (int, float)) and not isinstance(price, bool)
                    and math.isfinite(price) and price > 0):
                self.account.last_prices[instrument_id] = price
        return ticks


class CombinedEngine:
    """One sender with separate baseline and stale virtual inventories.

    SharedAccount's legacy owner names ``mm`` and ``pair`` mean baseline-refine
    and stale sniper respectively in this strategy.
    """

    def __init__(self, raw, config, stale_config, journal, baseline,
                 *, clock=time.monotonic, sleep=time.sleep, wall=time.time,
                 price_data_dir=None, terminal_quantity=None, restored=None):
        self.raw, self.config, self.journal = raw, config, journal
        self.baseline = baseline
        self.clock, self.sleep, self.wall = clock, sleep, wall
        self.symbols = tuple(config['symbols'])
        self.start = clock()
        account_config = dict(
            config,
            mm_position_limit=config['position_limit'],
            pair_position_limit=stale_config['max_order_lots'],
            fee_per_lot=stale_config['fee_per_lot'],
            settlement_seconds=stale_config['settlement_seconds'],
            cancel_confirmation_seconds=stale_config['settlement_seconds'],
            mm_max_spread_ticks=baseline['CYCLE_SETTINGS'].max_spread_ticks,
        )
        self.account = SharedAccount(raw, self.symbols, account_config, journal, clock, sleep)
        self.account.wall = wall
        self.account.entry_deadline = self.start + config['session_seconds'] - config['closeout_seconds']
        self.account.deadline = (self.start + config['session_seconds']
                                 + stale_config['shutdown_grace_seconds'])

        actual = raw.get_positions()
        if (not isinstance(actual, dict) or any(s not in actual for s in self.symbols)
                or any(isinstance(actual[s], bool) or not isinstance(actual[s], int)
                       for s in self.symbols)):
            raise ValueError('cannot adopt startup A/B inventory')
        ownership = restored or dict(
            positions={'mm': {s: actual[s] for s in self.symbols},
                       'pair': dict.fromkeys(self.symbols, 0)},
            cash={'mm': dict.fromkeys(self.symbols, 0.0),
                  'pair': dict.fromkeys(self.symbols, 0.0)},
        )
        self.account.initialize(ownership)
        self.mm_view = OwnedExchange(self.account, 'mm')
        self.stale_view = StaleView(self.account)

        orders_module = importlib.import_module(
            'stock_market_making.strategies.baseline-refine.order_execution')
        self.order_limit_error = orders_module.OrderLimitError
        manager = orders_module.QuoteManager
        self.quote_managers = {
            A: manager(self.mm_view, position_limit=config['position_limit'],
                       soft_limit=min(baseline['SOFT_LIMIT'], config['position_limit']),
                       net_position_limit=config['max_net_lots'], net_symbols=self.symbols),
            B: manager(self.mm_view, position_limit=config['baseline_b_position_limit'],
                       soft_limit=config['baseline_b_soft_limit'],
                       net_position_limit=config['max_net_lots'], net_symbols=self.symbols),
        }
        cycle_position = importlib.import_module(
            'stock_market_making.strategies.baseline-refine.cycle_position').CyclePosition
        b_settings = dict(baseline['B_POSITION_SETTINGS'])
        b_settings['target_lots'] = config['baseline_b_position_limit']
        self.b_position = cycle_position(**b_settings)
        self.cycle_module = importlib.import_module(
            'stock_market_making.strategies.baseline-refine.cycle_signal')
        self.cycle_model = self.cycle_module.CycleSignal(baseline['CYCLE_SETTINGS'])
        self.price_data_dir = price_data_dir
        self.bootstrap_pending = True
        self.history_retry_at = -math.inf

        stale_config = dict(stale_config, session_seconds=config['session_seconds'],
                            closeout_seconds=config['closeout_seconds'],
                            loop_seconds=config['loop_seconds'],
                            max_updates_per_second=config['max_updates_per_second'])
        self.stale = StaleEngine(self.stale_view, stale_config, journal, clock, sleep, wall,
                                 terminal_quantity=terminal_quantity)
        self.stale.startup()
        self.account.pair_book_reader = self.stale.feed.one
        self.stopping = False
        self.stop_reason = None
        self.journal.emit('combined_inventory_adopted', baseline_positions=ownership['positions']['mm'],
                          stale_positions=dict.fromkeys(self.symbols, 0))

    def _books(self):
        return {s: self.account.external_book(s) for s in self.symbols}

    def _bootstrap(self, ticks):
        if not (self.bootstrap_pending or
                (self.cycle_model.needs_bootstrap and self.clock() >= self.history_retry_at)):
            return
        history = importlib.import_module(
            'stock_market_making.strategies.baseline-refine.cycle_history')
        report = history.bootstrap_cycle(
            self.cycle_model, self.raw, self.price_data_dir or '.', ticks,
            self.clock(), self.wall(), scan_recordings=self.price_data_dir is not None)
        self.journal.emit('cycle_bootstrap', **report)
        self.bootstrap_pending = False
        self.history_retry_at = self.clock() + self.baseline['CYCLE_SETTINGS'].refit_seconds

    def _cancel_baseline_b(self):
        self.account.cancel_owner_symbol('mm', B)

    def _run_baseline(self, allow_b):
        books = self._books()
        instruments = self.raw.get_tradable_instruments()
        ticks = {s: instruments[s].tick_size for s in self.symbols}
        self._bootstrap(ticks)
        signal = self.cycle_model.observe(books, ticks, self.clock(), self.wall())
        for symbol in self.symbols:
            if symbol == B and not allow_b:
                self._cancel_baseline_b()
                continue
            book = books.get(symbol)
            usable = self.cycle_module.usable_book(
                book, ticks[symbol], self.wall(), self.baseline['CYCLE_SETTINGS'],
                check_spread=False)
            if not usable:
                self.quote_managers[symbol].reconcile(symbol, None)
                self.journal.emit('baseline_skip_quote', instrument=symbol,
                                  reason='stale or invalid external book')
                continue
            position = self.mm_view.get_positions()[symbol]
            quote = self.baseline['plan_quote'](
                book, position, ticks[symbol], symbol, signal,
                self.b_position, self.clock())
            if quote is not None and symbol == B:
                limit = self.config['baseline_b_position_limit']
                quote['target_position'] = max(-limit, min(limit, quote.get('target_position', 0)))
            try:
                result = self.quote_managers[symbol].reconcile(symbol, quote)
                self.journal.emit('baseline_quote_reconciled', instrument=symbol, result=result,
                                  baseline_position=position)
            except (RiskBlocked, self.order_limit_error) as exc:
                # A passive order can fill between QuoteManager's order and
                # position snapshots.  That makes its remaining quantity look
                # too large for the newly reduced capacity, but the account is
                # still fully attributable.  Remove this symbol's MM orders
                # and calculate a fresh quote on the next loop instead of
                # ending the whole session.
                self.journal.emit(
                    'baseline_quote_recovered', instrument=symbol,
                    reason=str(exc), error_type=type(exc).__name__)
                self.account.cancel_owner_symbol('mm', symbol)

    def step(self):
        if self.account.halted:
            raise RuntimeError('combined account halted')
        self.account.audit()
        if self.clock() >= self.account.entry_deadline or self.journal.failed:
            self.stopping = True
            self.stop_reason = self.stop_reason or (
                'journal_failure' if self.journal.failed else 'entry_deadline')
            self.stale.stopped = True

        self.stale.step()
        stale_busy = bool(self.stale.pending or self.stale.executor.positions()[B]
                          or self.stale.exit_reason)
        if self.stopping:
            self.account.cancel_owner('mm')
            self.journal.emit('combined_account', actual=self.account.audit(),
                              baseline_owned=self.account.positions['mm'].copy(),
                              stale_owned=self.account.positions['pair'].copy(),
                              stale_busy=stale_busy)
            return
        if stale_busy:
            self._cancel_baseline_b()
        self._run_baseline(allow_b=not stale_busy and not self.stopping)
        self.journal.emit('combined_account', actual=self.account.audit(),
                          baseline_owned=self.account.positions['mm'].copy(),
                          stale_owned=self.account.positions['pair'].copy(),
                          stale_busy=stale_busy)

    def finish(self, reason='shutdown'):
        self.stopping = True
        self.stop_reason = self.stop_reason or reason
        self.account.cancel_owner('mm')
        stale_summary = self.stale.finish(live=self.raw.is_connected())
        self.account.cancel_owner('pair')
        actual = self.account.audit() if self.raw.is_connected() else None
        summary = dict(
            actual_positions=actual,
            baseline_positions=self.account.positions['mm'].copy(),
            stale_positions=self.account.positions['pair'].copy(),
            stale_flat=stale_summary.get('flat', False),
            halted=self.account.halted or self.stale.executor.hard_fault,
            stop_reason=self.stop_reason,
        )
        self.journal.emit('session_end', **summary)
        return summary


