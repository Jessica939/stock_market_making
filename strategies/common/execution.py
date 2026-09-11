"""Single sender, IOC execution, verified private fills, and fail-closed recovery.

The documented API has no terminal IOC fill-count/sequence barrier.  An accepted
IOC is therefore settled only when its entire submitted quantity is present in
deduplicated private trades AND the corresponding account position change.  An
unresolved partial/zero fill or transport error disables *all* later inserts,
including liquidation.  Disconnect and review residual positions before restart.
Offline adapters may inject an authoritative terminal quantity; live runners
do not inject one. Partial/zero observations alone never constitute this proof.
Timeouts and repeated identical snapshots are not evidence that delayed fills
cannot arrive.  Cash logging is diagnostic; it never establishes a fill.
"""
from collections import deque
from collections.abc import Mapping
import math
import time

from .market import UnusableBook, ioc_plan


class ExecutionFault(RuntimeError):
    pass


def lots(value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExecutionFault('positions and volumes must be integer lots')
    return value


def finite(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


class RateLimiter:
    """One 1.001-second rolling window for actual update attempts across symbols.

    Routine updates stop at ``budget`` total attempts, preserving the remaining
    slots for cancels/reductions.  Emergency attempts still cannot exceed 25.
    ``wait`` reserves nothing; ``try_acquire`` records immediately before the
    transport call.  This separation permits fresh risk checks after sleeping.
    """
    def __init__(self, budget=20, clock=time.monotonic, sleep=time.sleep):
        if not isinstance(budget, int) or isinstance(budget, bool) or not 1 <= budget <= 22:
            raise ValueError('routine update budget must be in 1..22, preserving emergency slots')
        self.budget, self.clock, self.sleep = budget, clock, sleep
        self.sent = deque()
        self.last_now = None

    def _now(self):
        now = self.clock()
        if not finite(now) or (self.last_now is not None and now < self.last_now):
            raise ExecutionFault('invalid or reversed monotonic clock')
        self.last_now = now
        while self.sent and now - self.sent[0] >= 1.001:
            self.sent.popleft()
        return now

    def try_acquire(self, emergency=False, deadline=None):
        now = self._now()
        if deadline is not None and now >= deadline:
            return False
        if len(self.sent) >= (25 if emergency else self.budget):
            return False
        self.sent.append(now)
        return True

    def wait(self, emergency=False):
        cap = 25 if emergency else self.budget
        while True:
            now = self._now()
            if len(self.sent) < cap:
                return
            self.sleep(max(0.001, min(0.2, self.sent[0] + 1.002 - now)))

    def acquire(self, emergency=False):
        """Compatibility helper; call directly adjacent to an actual update."""
        while True:
            self.wait(emergency)
            if self.try_acquire(emergency):
                return


class Executor:
    def __init__(self, exchange, symbols, book_provider, config, journal,
                 clock=time.monotonic, sleep=time.sleep, *, terminal_quantity=None):
        self.exchange, self.symbols = exchange, tuple(symbols)
        if not self.symbols or len(set(self.symbols)) != len(self.symbols):
            raise ValueError('symbols must be a nonempty unique sequence')
        self.book_provider, self.config, self.journal = book_provider, dict(config), journal
        self.clock, self.sleep = clock, sleep
        # Explicit injection only: the live API supplies no terminal-quantity proof.
        # A trusted adapter must return an immutable final fill count, or None.
        self.terminal_quantity = terminal_quantity
        self.limiter = RateLimiter(config.get('max_updates_per_second', 20), clock, sleep)
        for key, default, ceiling in (('position_limit', 100, 100),
                                      ('max_outstanding_volume', 200, 200),
                                      ('max_order_lots', 20, 100), ('max_net_lots', 30, 200)):
            value = config.get(key, default)
            if lots(value) < 1 or value > ceiling:
                raise ValueError(f'{key} must be in 1..{ceiling}')
        for key, default in (('settlement_seconds', 0.25), ('cancel_confirmation_seconds', 0.25)):
            value = config.get(key, default)
            if not finite(value) or not 0 < value <= 30:
                raise ValueError(f'{key} must be finite and in (0, 30]')
        for key, default in (('entry_slippage_ticks', 1), ('exit_slippage_ticks', 3)):
            value = config.get(key, default)
            if not finite(value) or value < 0:
                raise ValueError(f'{key} must be finite and nonnegative')
        self.halted = False  # Soft stop: settled inventory may still be reduced.
        self.hard_fault = False  # Unknown state: NO more IOC, including reductions.
        self.fault_reason = None
        self.cooldown_until = 0.0
        # Runner supplies absolute monotonic deadlines.  These gate new API
        # invocations; they cannot interrupt an already blocking synchronous RPC.
        self.entry_deadline = None
        self.reduction_deadline = None
        self.private_totals = {}
        self.entry_reference = {}
        self._trade_ids = {}
        self._accepted = {}
        self._legacy_orders = set()
        self._started = False
        self._pending = None
        self._settled_positions = None
        self._last_audit_positions = None

    @property
    def unresolved(self):
        return self.hard_fault or self._pending is not None

    @property
    def account_consistent(self):
        if self.unresolved or self._last_audit_positions is None:
            return False
        if self._settled_positions is None:
            return not self._started
        return self._last_audit_positions == self._settled_positions

    def _fault(self, message):
        self.halted = True
        self.hard_fault = True
        self.fault_reason = self.fault_reason or str(message)
        raise ExecutionFault(self.fault_reason)

    def _insert_allowed(self):
        if self.hard_fault:
            raise ExecutionFault(self.fault_reason or 'execution state is unknown; inserts disabled')
        if self._pending is not None:
            self._fault('previous IOC is unresolved; all new inserts are disabled')
        if not self.exchange.is_connected():
            self._fault('exchange connection was lost')

    def _deadline_allows(self, reducing):
        deadline = self.reduction_deadline if reducing else self.entry_deadline
        if deadline is None:
            return True
        now = self.clock()
        if not finite(deadline) or not finite(now):
            self._fault('invalid execution deadline or monotonic clock')
        return now < deadline

    def _deadline_block(self, iid, reducing):
        self.journal.emit('blocked_order', instrument=iid, reducing=reducing,
                          reason='reduction deadline' if reducing else 'entry deadline')
        return 0

    def positions(self):
        try:
            raw = self.exchange.get_positions()
            if not isinstance(raw, Mapping) or any(i not in raw for i in self.symbols):
                raise ExecutionFault('positions unavailable; cannot infer flat')
            result = {i: lots(raw[i]) for i in self.symbols}
            if any(abs(q) > 100 for q in result.values()):
                raise ExecutionFault('observed position exceeds exchange hard limit')
            return result
        except Exception as exc:
            self._fault(f'position snapshot failed: {exc}')

    def orders(self, iid):
        if iid not in self.symbols:
            raise ExecutionFault('unknown instrument')
        try:
            raw = self.exchange.get_outstanding_orders(iid)
            if not isinstance(raw, Mapping):
                raise ExecutionFault('outstanding orders unavailable')
            orders = {}
            for oid, order in list(raw.items()):
                if lots(oid) < 0 or getattr(order, 'order_id', oid) != oid:
                    raise ExecutionFault('invalid outstanding order id')
                side, volume = order.side, lots(order.volume)
                price = order.price
                if side not in ('bid', 'ask') or volume <= 0 or not finite(price) or price <= 0:
                    raise ExecutionFault('invalid outstanding order')
                orders[oid] = (side, volume)  # Immutable copies, not live SDK objects.
            return orders
        except Exception as exc:
            self._fault(f'order snapshot failed for {iid}: {exc}')

    def _private_batch(self, iid, phase):
        try:
            trades = self.exchange.poll_new_trades(iid)
            if not isinstance(trades, (list, tuple)):
                raise ExecutionFault('private trade poll must return a list, including when empty')
            for trade in trades:
                oid, tid, volume = lots(trade.order_id), lots(trade.trade_id), lots(trade.volume)
                side, price = trade.side, trade.price
                if (oid < 0 or tid < 0 or volume <= 0 or side not in ('bid', 'ask')
                        or not finite(price) or price <= 0
                        or getattr(trade, 'instrument_id', iid) != iid):
                    raise ExecutionFault('malformed private trade')
                key = (iid, oid)
                fingerprint = (oid, side, float(price), volume)
                seen = self._trade_ids.get((iid, tid))
                if seen is not None:
                    if seen != fingerprint:
                        raise ExecutionFault('duplicate trade id has conflicting contents')
                    continue
                accepted = self._accepted.get(key)
                unattributed = False
                if accepted is None:
                    if self._started and key not in self._legacy_orders:
                        # A lost insert acknowledgment can leave its later fill
                        # unattributed.  Preserve it during final read-only audit;
                        # it cannot clear the hard fault or authorize another IOC.
                        unattributed = True
                    else:
                        self._legacy_orders.add(key)
                elif (accepted['side'] != side or self.private_totals.get(key, 0) + volume > accepted['volume']
                      or (side == 'bid' and price > accepted['price'] + 1e-9)
                      or (side == 'ask' and price < accepted['price'] - 1e-9)):
                    raise ExecutionFault('private fill contradicts the accepted IOC')
                # Persist each consumed item before polling another symbol.  A
                # failure on B cannot discard already consumed fills from A.
                self._trade_ids[(iid, tid)] = fingerprint
                self.private_totals[key] = self.private_totals.get(key, 0) + volume
                self.journal.emit('fill', phase=phase, instrument=iid,
                                  trade_id=tid, order_id=oid, side=side, price=price, volume=volume,
                                  attributable=not unattributed,
                                  exchange_timestamp=str(getattr(trade, 'timestamp', None)))
                if unattributed and not self.hard_fault:
                    raise ExecutionFault('private fill belongs to an unknown order')
        except Exception as exc:
            self._fault(f'private trade reconciliation failed for {iid}: {exc}')

    def audit(self, phase):
        # Remains usable after a hard fault for final read-only reconciliation.
        for iid in self.symbols:
            self._private_batch(iid, phase)
        positions = self.positions()
        self._last_audit_positions = positions.copy()
        try:
            holdings = self.exchange.get_positions_and_cash()
        except Exception as exc:
            holdings = None
            self.journal.emit('holdings_unavailable', phase=phase, error=str(exc))
        self.journal.emit('account', phase=phase, positions=positions, holdings=holdings)
        return positions

    def cancel_all(self, instruments=None):
        """Count per-ID cancellation attempts at transmission; confirm disappearance.

        Cancellation remains allowed after a hard fault because it cannot add a
        position.  It does not clear the fault or settle an ambiguous IOC.
        """
        selected = self.symbols if instruments is None else tuple(instruments)
        if any(iid not in self.symbols for iid in selected):
            raise ExecutionFault('invalid cancellation instruments')
        for iid in selected:
            initial = self.orders(iid)
            if not self._started:
                self._legacy_orders.update((iid, oid) for oid in initial)
            for oid in initial:
                while True:
                    self.limiter.wait(emergency=True)
                    if oid not in self.orders(iid):
                        break
                    if not self.exchange.is_connected():
                        self._fault('exchange disconnected during cancellation')
                    if not self.limiter.try_acquire(emergency=True):
                        continue
                    # No intervening logging, nested update, or sleep here.
                    try:
                        response = self.exchange.delete_order(iid, order_id=oid)
                    except Exception as exc:
                        self._fault(f'unknown cancellation outcome for {iid}/{oid}: {exc}')
                    success = getattr(response, 'success', None)
                    if type(success) is not bool:
                        self._fault('cancellation acknowledgment has no valid success flag')
                    self.journal.emit('cancel', instrument=iid, order_id=oid, success=success)
                    started = self.clock()
                    while oid in self.orders(iid):
                        if not success or self.clock() - started >= self.config.get('cancel_confirmation_seconds', 0.25):
                            self._fault('cancellation not confirmed; all inserts disabled')
                        self.sleep(0.2)
                    break
            if self.orders(iid):
                self._fault(f'{iid}: new/unconfirmed resting order appeared during cancellation')

    def _band_allows(self, iid, price):
        # API lists these fields but not the combination formula.  Intersection
        # of valid bounds is conservative; zero means zero width, not disabled.
        meta = self.book_provider.instruments[iid]
        band = getattr(meta, 'price_change_limit', None)
        if band is None:
            return True
        reference = self.book_provider.last_prices.get(iid)
        if not finite(reference) or reference <= 0:
            return False
        limits = []
        for name in ('absolute_change', 'relative_change'):
            value = band.get(name) if isinstance(band, Mapping) else getattr(band, name, None)
            if value is None:
                continue
            if not finite(value) or value < 0:
                return False
            width = value * reference if name == 'relative_change' else value
            if not math.isfinite(width):
                return False
            limits.append(width)
        return bool(limits) and abs(price - reference) <= min(limits) + 1e-9

    def _idle_snapshot(self):
        before = self.audit('pre_order')
        if self._settled_positions is not None and before != self._settled_positions:
            self._fault('position changed outside a fully settled IOC')
        if self._settled_positions is None:
            self._settled_positions = before.copy()
        return before

    def _settle(self, iid, side, volume, oid, before):
        started = self.clock()
        last_observation = None
        terminal = None
        while True:
            after = self.audit('ioc_settlement')
            if any(after[other] != before[other] for other in self.symbols if other != iid):
                self._fault('another instrument changed while settling a sequential IOC')
            change = (after[iid] - before[iid]) * (1 if side == 'bid' else -1)
            reported = self.private_totals.get((iid, oid), 0)
            if not 0 <= change <= volume:
                self._fault('position delta is inconsistent with the submitted IOC')
            if any(self.orders(other) for other in self.symbols):
                self._fault('IOC left an unexpected resting order; cancellation/disconnect required')
            if self.terminal_quantity is not None:
                evidence = self.terminal_quantity(iid, oid)
                if evidence is not None:
                    if isinstance(evidence, bool) or not isinstance(evidence, int) or not 0 <= evidence <= volume:
                        self._fault('invalid IOC terminal quantity evidence')
                    if terminal is not None and terminal != evidence:
                        self._fault('IOC terminal evidence changed')
                    terminal = evidence
            if terminal is not None and (change > terminal or reported > terminal):
                self._fault('IOC terminal evidence contradicts fills or positions')
            observed = (change, reported, terminal)
            if observed != last_observation:
                if self._pending is not None:
                    self._pending.update(position_delta=change, reported_fills=reported,
                                         terminal_quantity=terminal, state='awaiting_reconciliation')
                self.journal.emit('ioc_observation', instrument=iid, order_id=oid,
                                  position_delta=change, reported_fills=reported, terminal_quantity=terminal)
                last_observation = observed
            if change == reported and (change == volume or terminal == change):
                self._settled_positions = after.copy()
                self._pending = None
                self.journal.emit('ioc_settled', instrument=iid, order_id=oid,
                                  state='filled' if change == volume else 'partial' if change else 'zero_fill',
                                  filled_volume=change, cancelled_volume=volume-change,
                                  evidence='full_quantity_reconciled' if change == volume else 'terminal_quantity_adapter')
                return change
            if self.clock() - started >= self.config.get('settlement_seconds', 0.25):
                if self._pending is not None:
                    self._pending['state'] = 'unknown'
                self.journal.emit('unresolved_ioc', instrument=iid, order_id=oid,
                                  position_delta=change, reported_fills=reported, submitted_volume=volume,
                                  state='unknown', terminal_quantity=terminal,
                                  recovery='No inserts; reconcile this order and residual positions before restart')
                self._fault('IOC terminal quantity is unproven; all inserts including liquidation disabled')
            self.sleep(0.2)

    def send(self, iid, side, requested, *, reducing=False):
        if iid not in self.symbols or side not in ('bid', 'ask') or lots(requested) <= 0:
            raise ExecutionFault('invalid order request')
        self._insert_allowed()
        if self.halted and not reducing:
            return 0
        while True:
            if not self._deadline_allows(reducing):
                return self._deadline_block(iid, reducing)
            self.limiter.wait(emergency=reducing)
            self._insert_allowed()
            if not self._deadline_allows(reducing):
                return self._deadline_block(iid, reducing)
            outstanding = {other: self.orders(other) for other in self.symbols}
            if any(outstanding.values()):
                self.cancel_all()
                if self._started:
                    self._fault('unexpected resting orders after IOC trading began')
                if not reducing:
                    self.halted = True
                    return 0
                self._settled_positions = None
                continue  # Cancellation consumed budget; refresh after another wait.
            before = self._idle_snapshot()
            q = before[iid]
            limit = self.config.get('position_limit', 100)
            room = limit - q if side == 'bid' else limit + q
            quantity = min(requested, max(0, room), self.config.get('max_outstanding_volume', 200),
                           self.config.get('max_order_lots', 20))
            if reducing:
                if not q or (side == 'bid') != (q < 0):
                    return 0
                quantity = min(quantity, abs(q))
            else:
                net, net_limit = sum(before.values()), self.config.get('max_net_lots', 30)
                quantity = min(quantity, max(0, net_limit - net if side == 'bid' else net_limit + net))
            if quantity <= 0:
                return 0
            book = self.book_provider.one(iid, reducing=reducing,
                                          reducing_side=side if reducing else None)
            slippage = self.config.get('exit_slippage_ticks', 3) if reducing else self.config.get('entry_slippage_ticks', 1)
            price, volume = ioc_plan(book, side, quantity, slippage)
            tick = book.get('tick')
            if (not finite(price) or price <= 0 or not finite(tick) or tick <= 0
                    or abs(price / tick - round(price / tick)) > 1e-6
                    or not 0 <= lots(volume) <= quantity):
                self._fault('IOC planner returned an invalid price/volume')
            reference = self.entry_reference.get(iid)
            if reference is not None and not reducing:
                original = reference['ask' if side == 'bid' else 'bid']
                if not finite(original) or original <= 0:
                    self._fault('invalid signal price reference')
                tolerance = self.config.get('entry_slippage_ticks', 1) * tick
                if ((side == 'bid' and price > original + tolerance + 1e-9)
                        or (side == 'ask' and price < original - tolerance - 1e-9)):
                    self.journal.emit('blocked_order', instrument=iid, reason='signal price moved away')
                    return 0
            if volume <= 0 or not self._band_allows(iid, price):
                self.journal.emit('blocked_order', instrument=iid, reason='depth or price band')
                return 0
            self.journal.emit('order_attempt', instrument=iid, side=side, price=price,
                              volume=volume, reducing=reducing, order_type='ioc')
            # Recheck after all potentially slow reads/logging and let admission
            # check its own fresh timestamp too.  Expiration consumes no update
            # and creates no pending IOC; known pair exposure may still reduce.
            if not self._deadline_allows(reducing):
                return self._deadline_block(iid, reducing)
            deadline = self.reduction_deadline if reducing else self.entry_deadline
            if not self.limiter.try_acquire(emergency=reducing, deadline=deadline):
                continue  # No transmission: redo snapshots after waiting.
            self._pending = dict(instrument=iid, side=side, volume=volume, before=before, order_id=None)
            self._started = True
            try:
                response = self.exchange.insert_order(iid, price=price, volume=volume,
                                                      side=side, order_type='ioc')
            except Exception as exc:
                self._fault(f'unknown insertion outcome for {iid}: {exc}')
            break

        success, oid = getattr(response, 'success', None), getattr(response, 'order_id', None)
        if type(success) is not bool:
            self._fault('insertion acknowledgment has no valid success flag')
        if not success:
            if oid is not None:
                self._fault('rejected insertion unexpectedly has an order id')
            self.journal.emit('order_response', instrument=iid, success=False, order_id=None,
                              error=str(getattr(response, 'error_reason', 'order rejected')))
            after = self.audit('rejected_ioc')
            if after != before or any(self.orders(other) for other in self.symbols):
                self._fault('position/order state changed after an explicit rejection')
            self._pending = None
            self.cooldown_until = self.clock() + 5.0
            return 0
        if (isinstance(oid, bool) or not isinstance(oid, int) or oid < 0
                or (iid, oid) in self._accepted or (iid, oid) in self.private_totals):
            self._fault('accepted insertion has a missing/reused order id')
        self._pending['order_id'] = oid
        self._accepted[(iid, oid)] = dict(side=side, price=price, volume=volume)
        try:
            self.journal.emit('order_response', instrument=iid, success=True, order_id=oid, error=None)
            return self._settle(iid, side, volume, oid, before)
        except Exception as exc:
            self._fault(f'IOC reconciliation failed: {exc}')

    def flatten(self):
        """One bounded reduction per symbol; return actual residual positions."""
        self._insert_allowed()
        self.cancel_all()
        for iid in self.symbols:
            q = self.positions()[iid]
            if q:
                try:
                    self.send(iid, 'ask' if q > 0 else 'bid', abs(q), reducing=True)
                except UnusableBook as exc:
                    self.journal.emit('liquidation_blocked', instrument=iid, reason=str(exc))
        return self.positions()

    def apply(self, targets, kind, reference_books=None):
        self._insert_allowed()
        self.entry_reference = reference_books or {}
        if (not isinstance(targets, Mapping) or set(targets) != set(self.symbols)
                or any(abs(lots(q)) > self.config.get('position_limit', 100) for q in targets.values())
                or kind not in ('pair', 'ml')):
            raise ExecutionFault('invalid policy targets or kind')
        current = self.positions()
        if self.halted or not any(targets.values()):
            return self.flatten()
        if any(current.values()):
            if any(current[i] and current[i] * targets[i] <= 0 for i in self.symbols):
                return self.flatten()
            if kind == 'pair' and sum(current.values()) != 0:
                self.halted = True
                return self.flatten()
            for iid in self.symbols:
                excess = abs(current[iid]) - abs(targets[iid])
                if excess > 0:
                    self.send(iid, 'ask' if current[iid] > 0 else 'bid', excess, reducing=True)
            return self.positions()
        if self.clock() < self.cooldown_until:
            return current
        active = [i for i in self.symbols if targets[i]]
        if kind == 'pair':
            if len(active) != 2 or sum(targets.values()) != 0:
                raise ExecutionFault('pair targets must be equal and opposite')
            candidates = []
            exit_capacities = []
            for iid in active:
                side = 'bid' if targets[iid] > 0 else 'ask'
                book = self.book_provider.one(iid)
                _, available = ioc_plan(book, side, abs(targets[iid]), self.config.get('entry_slippage_ticks', 1))
                candidates.append((available, iid, side))
                if 'pair_exit_liquidity_fraction' in self.config:
                    exit_side = 'ask' if side == 'bid' else 'bid'
                    exit_book = self.book_provider.one(iid, reducing=True, reducing_side=exit_side)
                    _, exit_available = ioc_plan(exit_book, exit_side, abs(targets[iid]),
                                                 self.config.get('exit_slippage_ticks', 3))
                    # Ask for all displayed capacity before applying the fraction;
                    # applying it to a target-capped quantity would always shrink it.
                    _, depth_available = ioc_plan(exit_book, exit_side, 100,
                                                  self.config.get('exit_slippage_ticks', 3))
                    exit_capacities.append(min(exit_available, int(depth_available *
                                               self.config['pair_exit_liquidity_fraction'])))
            candidates.sort()
            wanted = min(candidates[0][0], candidates[1][0], self.config.get('max_order_lots', 20))
            if exit_capacities:
                wanted = min(wanted, *exit_capacities)
                self.journal.emit('pair_exit_capacity', requested=abs(targets[active[0]]), admitted=wanted,
                                  capacities=dict(zip(active, exit_capacities)))
            if wanted <= 0:
                return current
            _, first, side1 = candidates[0]
            _, second, side2 = candidates[1]
            fill1 = self.send(first, side1, wanted)
            if not fill1:
                return self.positions()
            try:
                fill2 = self.send(second, side2, fill1)
            except UnusableBook:
                fill2 = 0  # No request was sent, so compensating is safe.
            # ExecutionFault deliberately propagates: a late second-leg fill
            # must never race a guessed compensating trade on the first leg.
            mismatch = fill1 - fill2
            if mismatch:
                self.cooldown_until = self.clock() + 5.0
                self.journal.emit('pair_unmatched', first=first, second=second, lots=mismatch)
                try:
                    self.send(first, 'ask' if side1 == 'bid' else 'bid', mismatch, reducing=True)
                except UnusableBook:
                    self.halted = True
            after = self.positions()
            if sum(after.values()):
                self.halted = True
            return after
        if len(active) > 1:
            raise ExecutionFault('ML strategy may own only one directional symbol')
        for iid in active:
            self.send(iid, 'bid' if targets[iid] > 0 else 'ask', abs(targets[iid]))
        return self.positions()
