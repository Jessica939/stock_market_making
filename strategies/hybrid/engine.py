"""Simultaneous baseline market making and cycle-pair positions, one sender."""
import time

from stock_market_making.strategies.common.quoting import QuoteManager
from stock_market_making.strategies.baseline.run import load_strategy
from stock_market_making.strategies.baseline.cycle_signal import CycleSignal, apply_cycle_quote, usable_book
from stock_market_making.strategies.pair.policy import Policy
from stock_market_making.strategies.common.execution import Executor
from stock_market_making.strategies.common.runner import Feed
from stock_market_making.strategies.common.market import UnusableBook
from .account import SharedAccount, OwnedExchange, MarketView, RiskBlocked
from .state import restore_policy


class Hybrid:
    def __init__(self, raw, config, journal, clock=time.monotonic, sleep=time.sleep, wall=time.time,
                 *, mm_strategy=None, state_store=None, restored=None):
        self.mm = load_strategy() if mm_strategy is None else mm_strategy
        self.config, self.journal = config, journal
        self.clock, self.sleep, self.wall = clock, sleep, wall
        self.start = clock()
        self.state_store = state_store
        self.symbols = tuple(config['symbols'])
        self.account = SharedAccount(raw, self.symbols, config, journal, clock, sleep)
        self.account.wall = wall
        self.account.entry_deadline = self.start + config['session_seconds'] - config['closeout_seconds']
        self.account.deadline = self.start + config['session_seconds']
        self.account.initialize(restored)
        self.mm_view, self.pair_view = OwnedExchange(self.account, 'mm'), OwnedExchange(self.account, 'pair')
        self.mm.update(POSITION_LIMIT=config['mm_position_limit'], SOFT_LIMIT=config['mm_soft_limit'],
                       ORDER_VOLUME=config['mm_order_volume'])
        self.mm_model = CycleSignal(self.mm['CYCLE_SETTINGS'])
        self.mm_orders = QuoteManager(self.mm_view, position_limit=config['mm_position_limit'],
                                       soft_limit=config['mm_soft_limit'])
        pair_config = dict(config, position_limit=config['pair_position_limit'],
                           lot_size=config['pair_lot_size'], max_order_lots=config['pair_lot_size'])
        self.pair_policy = Policy(pair_config)
        self.feed = Feed(MarketView(self.account), self.symbols, config, clock, wall, journal)
        self.account.pair_book_reader = self.feed.one
        self.pair_executor = Executor(self.pair_view, self.symbols, self.feed, pair_config, journal, clock, sleep)
        self.pair_executor.entry_deadline = self.account.entry_deadline
        self.pair_executor.reduction_deadline = self.account.deadline
        self.stopping = False
        self.stop_reason = None
        self.market_paused = False
        self.last_decision = None
        self.baseline = self.peak = None
        if restored is not None:
            restore_policy(self, restored)
            self.journal.emit('ownership_restored', positions=self.account.positions,
                              saved_at=restored['saved_at'])
        if self.state_store is not None:
            self.state_store.checkpoint(self)

    def request_stop(self, reason, **details):
        self.stopping = True
        if self.stop_reason is None:
            self.stop_reason = reason
            self.journal.emit('hybrid_stop', reason=reason, elapsed_seconds=self.clock()-self.start,
                              **details)
            print(f'Hybrid stopping: {reason}; {details}', flush=True)

    def equity(self):
        """Conservative top-of-book marks, including both strategies' cash."""
        actual = self.account.audit()
        equity = sum(sum(c.values()) for c in self.account.cash.values())
        for s, q in actual.items():
            if not q:
                continue
            book = self.account.external_book(s)
            instrument = self.account.raw.get_tradable_instruments().get(s)
            if instrument is None:
                raise UnusableBook(f'{s}: instrument unavailable for inventory valuation')
            tick = instrument.tick_size
            if not usable_book(book, tick, self.wall(), self.account.market_settings):
                raise UnusableBook(f'{s}: cannot value inventory on fresh, valid depth')
            equity += q * (book.bids[0].price if q > 0 else book.asks[0].price)
        return equity

    def _pair_apply(self, targets, reference=None):
        current = self.pair_view.get_positions()
        if targets == current:
            return
        # Short execution critical section only. MM resumes while pair is held.
        self.account.cancel_owner('mm')
        try:
            self.pair_executor.apply(targets, 'pair', reference)
        except Exception:
            self.account.halted = True
            raise
        if self.pair_executor.hard_fault or self.pair_executor.unresolved:
            self.account.fault('Pair IOC unresolved; both strategies disabled')

    def step(self):
        try:
            if self.state_store is not None:
                self.state_store.invalidate()
            self._step()
            if self.state_store is not None:
                self.state_store.checkpoint(self)
        except BaseException:
            # Ctrl+C during a request is also an unknown outcome. Shutdown may
            # cancel orders, but must not turn this into a resumable checkpoint.
            self.account.halted = True
            raise

    def _step(self):
        if self.account.halted or self.pair_executor.hard_fault:
            self.account.fault('Hybrid stopped after execution fault')
        self.account.audit()
        positions = self.pair_executor.audit('hybrid_cycle')
        frame = None
        try:
            frame = self.feed.frame()
        except UnusableBook as exc:
            self.journal.emit('pair_market_blocked', reason=str(exc))
        self.account.last_prices = self.feed.last_prices
        valuation_error = None
        try:
            equity = self.equity()
            if self.baseline is None:
                self.baseline = self.peak = equity
            self.peak = max(self.peak, equity)
            if equity - self.baseline <= -self.config['max_session_loss']:
                self.request_stop('session_loss_limit', equity=equity, baseline=self.baseline)
            elif self.peak - equity >= self.config['max_drawdown']:
                self.request_stop('drawdown_limit', equity=equity, peak=self.peak)
        except UnusableBook as exc:
            valuation_error = str(exc)
        if self.clock() >= self.account.entry_deadline:
            self.request_stop('entry_deadline')
        if self.journal.failed:
            self.request_stop('journal_failure')
        if self.pair_executor.halted:
            self.request_stop('pair_executor_halted')
        if self.stopping:
            self.close_pair()
            return
        if valuation_error is not None:
            # Keep reconciling fills, but send no new orders until inventory can
            # be valued. Do not erase the session loss baseline or high water mark.
            self.account.cancel_owner('mm')
            if not self.market_paused:
                self.market_paused = True
                self.journal.emit('hybrid_paused', reason=valuation_error)
                print(f'Hybrid paused, waiting for market data: {valuation_error}', flush=True)
            return
        if self.market_paused:
            self.market_paused = False
            self.journal.emit('hybrid_resumed', reason='inventory_valuation_restored')
            print('Hybrid resumed: inventory valuation restored', flush=True)
        decision = self.pair_policy.decide(
            frame or dict(now=self.clock(), books={}), positions, self.clock()-self.start)
        self.last_decision = decision
        self.journal.emit('pair_decision', **decision)
        self._pair_apply(decision['targets'], frame['books'] if frame else None)
        if self.pair_executor.halted:
            self.request_stop('pair_executor_halted')
            self.close_pair()
            return
        # Refresh after any IOC: never reuse the pre-pair MM book or inventory.
        books = {s: self.account.external_book(s) for s in self.symbols}
        instruments = self.account.raw.get_tradable_instruments()
        ticks = {s: instruments[s].tick_size for s in self.symbols if s in instruments}
        self.mm_model.observe(books, ticks, self.clock(), self.wall())
        for s in self.symbols:
            try:
                book = books[s]
                if not usable_book(book, ticks.get(s, 0), self.wall(), self.account.market_settings):
                    self.mm_orders.reconcile(s, None)
                    continue
                q = self.mm_view.get_positions()[s]
                base = self.mm['calculate_quote'](book, q, ticks[s])
                signal = self.mm_model.observe(books, ticks, self.clock(), self.wall())
                quote = apply_cycle_quote(base, book, q, ticks[s], s, signal,
                                          self.mm['CYCLE_SETTINGS'], self.config['mm_soft_limit'])
                self.mm_orders.reconcile(s, quote)
                self.journal.emit('mm_quote', instrument=s, mm_position=q,
                                  pair_positions=self.account.positions['pair'].copy(), **quote)
            except RiskBlocked as exc:
                self.journal.emit('mm_admission_blocked', instrument=s, reason=str(exc))
                self.mm_orders.reconcile(s, None)
        self.journal.emit('hybrid_account', actual=self.account.audit(),
                          owned={o: dict(q) for o, q in self.account.positions.items()})

    def close_pair(self):
        """Retain baseline's cancel-and-retain-inventory shutdown semantics.

        Pair positions are reduced while the MM virtual inventory is preserved.
        Unknown outcomes disallow further inserts, including attempted closeout.
        """
        self.account.cancel_owner('mm')
        if not self.account.halted and not self.pair_executor.hard_fault and self.clock() < self.account.deadline:
            self._pair_apply(dict.fromkeys(self.symbols, 0))

    def finish(self, reason='shutdown_requested'):
        if self.state_store is not None:
            try:
                self.state_store.invalidate()
            except Exception:
                self.account.halted = True
                raise
        self.request_stop(reason)
        try:
            self.close_pair()
        except Exception as exc:
            self.account.halted = True
            self.journal.emit('closeout_error', error=str(exc))
        for owner in self.account.owners:
            try:
                self.account.cancel_owner(owner)
            except Exception as exc:
                self.account.halted = True
                self.journal.emit('final_cancel_error', owner=owner, error=str(exc))
        actual = None
        try:
            actual = self.account.audit()
        except Exception as exc:
            self.account.halted = True
            self.journal.emit('final_audit_error', error=str(exc))
        summary = dict(actual_positions=actual, owned_positions=self.account.positions,
                       pair_flat=(actual is not None and not self.pair_executor.unresolved
                                  and not any(self.account.positions['pair'].values())),
                       halted=self.account.halted, unresolved_pair_ioc=self.pair_executor.unresolved,
                       mm_inventory_retained=True, stop_reason=self.stop_reason,
                       elapsed_seconds=self.clock()-self.start)
        self.journal.emit('session_end', **summary)
        if self.state_store is not None:
            self.state_store.checkpoint(self, final=True)
            summary['state_recoverable'] = self.state_store.data['recoverable']
        return summary
