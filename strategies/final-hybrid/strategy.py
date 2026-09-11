"""The B sniper's decisions, with A-only passive MM under one account-wide sender."""
from dataclasses import fields
import math
import time

from stock_market_making.quote_helpers import external_price_book
from .execution import B, ExecutionFault
from .maker import calculate_quote
from .market import UnusableBook, bounded_book, cap_depth, sized_execution, vwap
from .model import BasisSettings, CausalBasisModel
from .cycle_target import TargetCycle, settings as cycle_settings

A = 'PHILIPS_A'
SYMBOLS = (A, B)


def validate(config):
    cycle_settings(config)
    for key, default in (('max_requests_per_second', 200),):
        value = config.get(key, default)
        if type(value) is not int or not 1 <= value <= 200:
            raise ValueError(key + ' must be an integer in 1..200')
    maker_seconds = config.get('maker_seconds', .25)
    if type(maker_seconds) not in (int, float) or not math.isfinite(maker_seconds) or maker_seconds < .05:
        raise ValueError('maker_seconds must be finite and >=50ms')
    positive = ('hold_seconds', 'execution_delay_seconds', 'entry_wait_seconds',
                'entry_edge_ticks', 'cooldown_seconds', 'max_book_age_seconds',
                'max_pair_time_gap_seconds', 'max_spread_ticks', 'loop_seconds',
                'max_session_loss', 'max_drawdown', 'settlement_seconds',
                'size_step_ticks', 'recording_seconds', 'session_seconds',
                'closeout_seconds', 'shutdown_grace_seconds')
    for key in positive:
        value = config[key]
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(key + ' must be finite and positive')
    for key, low, high in (
        ('order_lots', 1, 50), ('max_order_lots', 1, 50), ('lots_per_step', 1, 50),
        ('depth_reserve_lots', 0, 1000), ('max_sweep_ticks', 0, 20),
        ('max_updates_per_second', 1, 200),
    ):
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise ValueError(key + f' must be an integer in {low}..{high}')
    if not config['order_lots'] <= config['max_order_lots']:
        raise ValueError('base size must not exceed maximum size')
    if config['loop_seconds'] < .05 or config['settlement_seconds'] > 30:
        raise ValueError('loop must be >=50ms and settlement <=30s')
    if not config['hold_seconds'] < config['closeout_seconds'] < config['session_seconds']:
        raise ValueError('require hold < closeout < session')
    if type(config['fee_per_lot']) not in (int, float) or not math.isfinite(config['fee_per_lot']) or config['fee_per_lot'] < 0:
        raise ValueError('fee_per_lot must be finite and nonnegative')
    BasisSettings(**{f.name: config[f.name] for f in fields(BasisSettings)})


class HybridStrategy:
    def __init__(self, exchange, executor, config, *, event=lambda *a, **kw: None,
                 clock=time.monotonic, epoch=time.time):
        validate(config)
        self.exchange, self.executor, self.config = exchange, executor, config
        self.event, self.clock, self.epoch = event, clock, epoch
        instruments = exchange.get_tradable_instruments()
        if any(i not in instruments for i in SYMBOLS):
            raise ValueError('both PHILIPS_A and PHILIPS_B must be tradable')
        self.ticks = {i: instruments[i].tick_size for i in SYMBOLS}
        if any(type(t) not in (int, float) or not math.isfinite(t) or t <= 0 for t in self.ticks.values()):
            raise ValueError('invalid tick sizes')
        self.baseline_b = self.positions()[B]
        if exchange.get_outstanding_orders(B):
            raise ExecutionFault('cancel existing B orders before startup')
        self.model = CausalBasisModel(BasisSettings(
            **{f.name: config[f.name] for f in fields(BasisSettings)}))
        self.target_cycle = TargetCycle(config)
        self.start = clock()
        self.last_clock = self.start
        self.last_maker = -math.inf
        self.cutoff = self.start + config['session_seconds'] - config['closeout_seconds']
        self.pending = self.entry = self.exit_intent = None
        self.cycle_position = 0
        self.cycle_cash = 0.0
        self.high = 0.0
        self.cooldown_until = self.start
        self.stopped = self.risk_stopped = False
        self.event('inventory_baseline', baseline_B=self.baseline_b,
                   cycle_position=0)

    def positions(self):
        positions = self.exchange.get_positions()
        if (not isinstance(positions, dict) or any(i not in positions for i in SYMBOLS)
                or any(type(positions[i]) is not int or abs(positions[i]) > 100 for i in SYMBOLS)):
            raise ExecutionFault('missing or invalid actual A/B positions')
        return positions

    def book(self, iid):
        tick = self.ticks[iid]
        raw = self.exchange.get_last_price_book(iid)
        orders = self.exchange.get_outstanding_orders(iid)
        book = external_price_book(raw, orders, tick)
        # A wide spread can still be used to reduce maker risk, as in baseline.
        settings = dict(self.config, max_spread_ticks=1e12)
        return book, bounded_book(book, tick, self.epoch(), settings)

    def pair(self, *, check_spread=True):
        raw, bounded = {}, {}
        for iid in SYMBOLS:
            raw[iid], bounded[iid] = self.book(iid)
            if check_spread and bounded[iid]['ask']-bounded[iid]['bid'] > self.config['max_spread_ticks']*self.ticks[iid]+1e-9:
                raise UnusableBook('wide pair spread')
        if abs(bounded[A]['timestamp']-bounded[B]['timestamp']) > self.config['max_pair_time_gap_seconds']:
            raise UnusableBook('unsynchronized pair books')
        return raw, bounded

    def _record_fill(self, report):
        if report is None:
            return 0
        sign = 1 if report['side'] == 'bid' else -1
        self.cycle_position += sign * report['filled']
        self.cycle_cash -= sign * report['notional'] + report['filled'] * self.config['fee_per_lot']
        limit = (self.target_cycle.position.target_lots if self.target_cycle.position.active
                 else self.config['max_order_lots'])
        if abs(self.cycle_position) > limit:
            raise ExecutionFault('sniper-owned B inventory exceeds its limit')
        return report['filled']

    def _equity(self, book):
        q = self.cycle_position
        if not q:
            return self.cycle_cash
        return (self.cycle_cash + q*vwap(book, q < 0, abs(q))
                - abs(q)*self.config['fee_per_lot'])

    def _risk(self, book):
        try:
            equity = self._equity(book)
        except UnusableBook:
            return
        self.high = max(self.high, equity)
        if equity <= -self.config['max_session_loss'] or self.high-equity >= self.config['max_drawdown']:
            self.stopped = self.risk_stopped = True
            self.pending = None
            self.event('stale_risk_stop', equity=equity, high=self.high)

    def request_exit(self, reason):
        if self.exit_intent is None or reason != 'fair_reached':
            self.exit_intent = dict(reason=reason, epoch=self.epoch())
            self.event('stale_exit_intent', **self.exit_intent)

    def _exit(self):
        intent = self.exit_intent
        if not self.cycle_position:
            self.entry = self.exit_intent = None
            return
        def planner(actual):
            _, book = self.book(B)
            q = self.cycle_position
            sign = -1 if q > 0 else 1
            if intent['reason'] == 'fair_reached':
                if book['timestamp'] <= intent['epoch']:
                    raise UnusableBook('await post-decision exit book')
                execution = vwap(book, sign > 0, abs(q))
                if (1 if q > 0 else -1)*(execution-self.entry['fair_B']) < -1e-9:
                    self.exit_intent = None
                    self.event('stale_exit_cancelled', fair_B=self.entry['fair_B'], execution_vwap=execution)
                    raise UnusableBook('fair exit disappeared')
            # Restore the inherited B baseline, bounded by the account limit.
            room = 100-sign*actual
            levels = cap_depth(book['asks' if sign > 0 else 'bids'], min(abs(q), max(0, room)))
            if not levels:
                raise UnusableBook('no bounded exit depth or account capacity')
            return dict(price=levels[-1][0], volume=sum(v for _, v in levels),
                        side='bid' if sign > 0 else 'ask')
        filled = self._record_fill(self.executor.send_ioc(planner))
        if not self.cycle_position:
            self.event('stale_exit_confirmed', filled=filled, cycle_cash=self.cycle_cash)
            cycle_exit = self.entry and self.entry.get('kind') == 'cycle_target'
            self.entry = self.exit_intent = None
            cooldown = (self.target_cycle.position.cooldown_seconds if cycle_exit
                        else self.config['cooldown_seconds'])
            if cycle_exit:
                self.target_cycle.position.active = None
                self.target_cycle.position.next_entry = self.clock()+cooldown
            self.cooldown_until = self.clock()+cooldown

    def _enter(self):
        pending = self.pending
        self.pending = None  # Exactly one recheck for this decision.
        recheck = {}
        def planner(actual):
            if self.clock() >= pending['expires'] or self.clock() >= self.cutoff or self.stopped:
                raise UnusableBook('entry expired before transmission')
            _, books = self.pair()
            fair = books[A]['mid']-self.model.predict_snapshot(pending['model'], self.clock())
            sign = pending['sign']
            threshold = self.config['entry_edge_ticks']*self.ticks[B]+2*self.config['fee_per_lot']
            key = 'asks' if sign > 0 else 'bids'
            # Every price swept still has the required edge; size also passes
            # the original whole-order VWAP/tier check.
            filtered = [(p,v) for p,v in books[B][key] if sign*(fair-p)+1e-9 >= threshold]
            candidate = dict(books[B], **{key: filtered})
            room = min(pending['volume'], max(0, 100-sign*actual))
            if room < self.config['order_lots']:
                raise UnusableBook('insufficient account room for base size')
            size, execution, edge = sized_execution(candidate, sign > 0, fair, self.config, room)
            if size < self.config['order_lots'] or edge+1e-9 < threshold:
                raise UnusableBook('stale-quote edge disappeared')
            levels = cap_depth(filtered, size)
            recheck.update(fair_B=fair, execution_vwap=execution, edge=edge, volume=size)
            return dict(price=levels[-1][0], volume=size, side='bid' if sign > 0 else 'ask')
        report = self.executor.send_ioc(planner)
        if self._record_fill(report):
            self.entry = dict(sign=pending['sign'], fair_B=recheck['fair_B'],
                              decision_fair_B=pending['fair_B'], decision_edge=pending['edge'],
                              volume=abs(self.cycle_position), opened_at=self.clock())
            self.event('stale_entry_confirmed', entry=self.entry, recheck=recheck)

    def _cycle_enter(self):
        pending, self.pending = self.pending, None
        controller = self.target_cycle.position
        held = controller.active
        def planner(actual):
            now = self.clock()
            if (held is None or now >= pending['expires'] or now >= self.cutoff
                    or self.stopped or held['exit_reason']):
                raise UnusableBook('cycle entry expired before transmission')
            raw, books = self.pair()
            self.target_cycle.observe(raw, self.exchange, self.ticks, now, self.epoch(), self.event)
            quote = self.target_cycle.quote(raw[B], self.cycle_position, self.ticks[B], now)
            side = 'bid' if held['sign'] > 0 else 'ask'
            quantity = quote['buy_volume' if side == 'bid' else 'sell_volume']
            # The target was frozen before the delay. Every swept level must
            # leave more than one tick plus round-trip fees to that target.
            edge = controller.edge_buffer_ticks*self.ticks[B]+2*self.config['fee_per_lot']
            levels = [(p, v) for p, v in books[B]['asks' if side == 'bid' else 'bids']
                      if held['sign']*(held['target_price']-p) > edge+1e-9]
            levels = cap_depth(levels, min(quantity, max(0, 100-held['sign']*actual)))
            if not levels:
                raise UnusableBook('cycle target has no executable entry edge')
            return dict(side=side, price=levels[-1][0], volume=sum(v for _, v in levels))
        report = self.executor.send_ioc(planner)
        if self._record_fill(report):
            held['filled'] = True
            self.entry = dict(kind='cycle_target', sign=held['sign'],
                fair_B=held['target_price'], target_price=held['target_price'],
                model_target_price=held['model_target_price'], opened_at=held['opened_at'],
                volume=abs(self.cycle_position))
            self.event('cycle_entry_confirmed', entry=self.entry)

    def _cycle(self):
        controller = self.target_cycle.position
        now = self.clock()
        if self.stopped:
            self.pending = None
            if self.cycle_position:
                self.request_exit('risk' if self.risk_stopped else 'stopping')
                self._exit()
            else:
                controller.active = None
            return
        if self.exit_intent:
            self._exit()
            return
        # Exit observations need only a fresh B book, even if A is unusable.
        raw, book = self.book(B)
        quote = self.target_cycle.quote(raw, self.cycle_position, self.ticks[B], now)
        self.event('cycle_position', **quote['cycle_position'])
        held = controller.active
        if held and held['exit_reason'] and self.cycle_position:
            self.pending = None
            self.request_exit(held['exit_reason'])
            self._exit()
            return
        if self.pending:
            if held is None or now >= self.pending['expires']:
                self.pending = None
            elif now >= self.pending['ready']:
                self._cycle_enter()
            return
        if held is None or not (quote['buy_volume'] or quote['sell_volume']):
            return
        edge = controller.edge_buffer_ticks*self.ticks[B]+2*self.config['fee_per_lot']
        touch = book['ask'] if held['sign'] > 0 else book['bid']
        if held['sign']*(held['target_price']-touch) <= edge+1e-9:
            return
        ready = now+self.config['execution_delay_seconds']
        self.pending = dict(kind='cycle_target', ready=ready,
            expires=min(ready+self.config['entry_wait_seconds'], held['build_until'], self.cutoff))
        self.event('cycle_entry_pending', target_price=held['target_price'], **self.pending)

    def _sniper(self, books, signal):
        now = self.clock()
        if now >= self.cutoff:
            self.stopped = True
        if books is not None:
            self._risk(books[B])
        if self.target_cycle.position.active:
            self._cycle()
            return
        if self.cycle_position:
            if self.entry is None:
                raise ExecutionFault('sniper inventory has no confirmed entry')
            if self.stopped:
                self.request_exit('risk' if self.risk_stopped else 'stopping')
            elif now-self.entry['opened_at'] >= self.config['hold_seconds']:
                self.request_exit('timeout')
            elif self.exit_intent is None:
                try:
                    _, book = self.book(B)
                    execution = vwap(book, self.cycle_position < 0, abs(self.cycle_position))
                    if self.entry['sign']*(execution-self.entry['fair_B']) >= -1e-9:
                        self.request_exit('fair_reached')
                except UnusableBook:
                    pass
            if self.exit_intent:
                self._exit()
            return
        if self.stopped:
            self.pending = None
            return
        if self.pending:
            if now >= self.pending['expires']:
                self.event('stale_entry_expired', decision=self.pending)
                self.pending = None
            elif now >= self.pending['ready']:
                self._enter()
            return
        if books is None or not signal.get('active') or now < self.cooldown_until:
            return
        buy = vwap(books[B], True, self.config['order_lots'])
        sell = vwap(books[B], False, self.config['order_lots'])
        fair = signal['fair_B']
        long_edge, short_edge = fair-buy, sell-fair
        threshold = self.config['entry_edge_ticks']*self.ticks[B]+2*self.config['fee_per_lot']
        if max(long_edge, short_edge)+1e-9 < threshold:
            return
        sign = 1 if long_edge >= short_edge else -1
        size, execution, edge = sized_execution(books[B], sign > 0, fair, self.config)
        if edge+1e-9 < threshold:
            return
        delay = self.config['execution_delay_seconds']
        self.pending = dict(sign=sign, fair_B=fair, edge=edge, volume=size,
                            execution_vwap=execution, model=signal['model'],
                            decided_at=now, ready=now+delay,
                            expires=min(now+delay+self.config['entry_wait_seconds'], self.cutoff))
        self.event('stale_entry_pending', **self.pending)

    def maker_quote(self, iid, raw, book, actual):
        quote = calculate_quote(raw, actual, self.ticks[iid])
        if quote is None:
            return None
        wide = book['ask']-book['bid'] > self.config['max_spread_ticks']*self.ticks[iid]+1e-9
        if iid != A:
            raise ValueError('market making is allowed only for A')
        if self.stopped:
            return None
        if wide:
            quote.update(reduce_only=True, buy_volume=max(0, -actual), sell_volume=max(0, actual))
        quote.update(order_type='limit', execution_policy={'bid':'maker_limit', 'ask':'maker_limit'})
        return quote

    def step(self):
        now = self.clock()
        if not math.isfinite(now) or now < self.last_clock:
            raise ExecutionFault('invalid or reversed clock')
        self.last_clock = now
        if self.positions()[B] != self.baseline_b + self.cycle_position:
            raise ExecutionFault('B inventory changed outside confirmed sniper fills')
        if self.exchange.get_outstanding_orders(B):
            raise ExecutionFault('unexpected resting B order')
        raw, books, signal = {}, None, dict(active=False, reason='invalid_pair')
        try:
            raw, books = self.pair(check_spread=False)
            if any(books[i]['ask']-books[i]['bid'] > self.config['max_spread_ticks']*self.ticks[i]+1e-9
                   for i in SYMBOLS):
                raise UnusableBook('wide pair spread')
            signal = self.model.observe(now=now, a_mid=books[A]['mid'], b_mid=books[B]['mid'],
                                        book_stamps=(books[A]['timestamp'], books[B]['timestamp']))
        except UnusableBook as exc:
            signal['reason'] = str(exc)
        self.target_cycle.observe(raw, self.exchange, self.ticks, now, self.epoch(), self.event)
        self.event('stale_signal', signal=signal)
        try:
            self._sniper(books, signal)
            if (self.target_cycle.enabled and not self.stopped and self.pending is None
                    and self.entry is None and self.target_cycle.position.active is None
                    and self.clock() >= self.cooldown_until):
                self._cycle()
        except UnusableBook as exc:
            self.event('stale_execution_blocked', reason=str(exc))
        if not self.stopped and self.clock()-self.last_maker < self.config.get('maker_seconds', .25):
            return
        # Always fetch again: IOC, cancellations or rate waits may have changed
        # both displayed depth and actual inventory since the signal snapshot.
        for iid in (A,):
            try:
                self.executor.exchange._limiter.acquire()
                raw, book = self.book(iid)
                actual = self.positions()[iid]
                quote = self.maker_quote(iid, raw, book, actual)
                if quote:
                    quote['valid_until'] = self.clock() + max(0.,
                        self.config['max_book_age_seconds']-(self.epoch()-book['timestamp']))
            except UnusableBook:
                quote = None
            if quote:
                self.event('maker_quote', instrument=iid, **quote)
            try:
                self.executor.reconcile(iid, quote)
            except UnusableBook as exc:
                self.event('maker_quote_skipped', reason=str(exc))
                self.executor.reconcile(iid, None)
        self.last_maker = self.clock()

    def stop(self):
        self.stopped = True
        self.pending = None
        for iid in SYMBOLS:
            self.executor.reconcile(iid, None)
