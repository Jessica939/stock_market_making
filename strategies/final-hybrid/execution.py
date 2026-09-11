"""One sender for passive quotes and fully reconciled B IOC attempts."""
import time

from .order_execution import LimitedExchange, QuoteManager
from .market import UnusableBook

B = 'PHILIPS_B'


class ExecutionFault(RuntimeError):
    """An uncertain order outcome forbids subsequent inserts."""


class HybridExchange(LimitedExchange):
    def insert_ioc(self, planner):
        # Wait on the SAME limiter used for all MM inserts and cancellations.
        # Refresh the decision, inventory and depth only after that wait.
        self._limiter.acquire()
        if self._outstanding_orders(B):
            raise ExecutionFault('B orders remain before IOC')
        before = self._position(B)
        order = planner(before)
        if order is None:
            return None
        price, volume, side = order['price'], order['volume'], order['side']
        if type(volume) is not int or not 0 < volume <= 100 or side not in ('bid', 'ask'):
            raise ValueError('invalid IOC plan')
        QuoteManager._price(price)
        orders = self._outstanding_orders(B)
        if orders or self._position(B) != before:
            raise ExecutionFault('B changed after cancellation; IOC not sent')
        self._check_total(B, volume)
        self._check_worst_position(B, side, orders, volume)
        try:
            response = self._exchange.insert_order(B, price=price, volume=volume,
                                                   side=side, order_type='ioc')
        except Exception as exc:
            raise ExecutionFault(f'unknown IOC insertion outcome: {exc}') from exc
        return response, before, order


class HybridExecutor(QuoteManager):
    """A limits and B IOC share one limiter; B has no resting strategy orders.

    The live API provides no terminal partial-fill count. Follow the sniper's
    rule: full private fills + matching positions prove completion; a timeout,
    zero fill or a stable partial snapshot does not. Only tests/replay may
    inject authoritative terminal_quantity evidence.
    """

    def __init__(self, exchange, fill_stream, *, clock=time.monotonic,
                 sleep=time.sleep, settlement_seconds=.6, terminal_quantity=None,
                 event=lambda *args, **kwargs: None):
        super().__init__(exchange, fill_stream=fill_stream)
        self.clock, self.sleep, self.event = clock, sleep, event
        self.settlement_seconds = settlement_seconds
        self.terminal_quantity = terminal_quantity
        self.halted = False
        self.pending = None
        self.deadline = None

    def reconcile(self, instrument_id, quote):
        if self.halted and quote:
            raise ExecutionFault('unresolved IOC: all inserts disabled')
        return super().reconcile(instrument_id, quote)

    def send_ioc(self, planner):
        if self.halted or self.pending is not None:
            raise ExecutionFault('previous IOC unresolved')
        self.reconcile(B, None)
        self.fill_stream.refresh(B)
        def checked_plan(actual):
            if self.deadline is not None and self.clock() >= self.deadline:
                raise UnusableBook('IOC execution deadline elapsed')
            return planner(actual)
        try:
            result = self.exchange.insert_ioc(checked_plan)
            if result is None:
                return None
            response, before, order = result
            if getattr(response, 'success', None) is not True:
                # An explicit clean rejection is known to have created no order.
                if (getattr(response, 'success', None) is False
                        and getattr(response, 'order_id', None) is None
                        and self._position(B) == before and not self._orders(B)):
                    self.event('ioc_rejected', reason=getattr(response, 'error_reason', ''), **order)
                    return None
                raise ExecutionFault('IOC acknowledgment is invalid or ambiguous')
            oid = getattr(response, 'order_id', None)
            if type(oid) is not int or oid < 0:
                raise ExecutionFault('accepted IOC has no valid order id')
            self.pending = dict(order_id=oid, before=before, **order)
            self.event('ioc_submitted', **self.pending)
            started, terminal = self.clock(), None
            while True:
                self.fill_stream.refresh(B)
                self.fill_stream.validate_order(B, oid, order['side'], order['price'])
                filled = self.fill_stream.confirmed_volume(B, oid)
                position = self._position(B)
                delta = (position-before) * (1 if order['side'] == 'bid' else -1)
                if not 0 <= delta <= order['volume'] or filled > order['volume'] or self._orders(B):
                    raise ExecutionFault('IOC fills, inventory or resting orders are inconsistent')
                if self.terminal_quantity is not None:
                    evidence = self.terminal_quantity(B, oid)
                    if evidence is not None:
                        if (type(evidence) is not int or not 0 <= evidence <= order['volume']
                                or (terminal is not None and terminal != evidence)):
                            raise ExecutionFault('invalid or changing terminal fill evidence')
                        terminal = evidence
                if terminal is not None and max(delta, filled) > terminal:
                    raise ExecutionFault('terminal evidence contradicts observed fills')
                if delta == filled and (filled == order['volume'] or terminal == filled):
                    report = dict(self.pending, filled=filled, position=position,
                                  notional=self.fill_stream.confirmed_notional(B, oid))
                    self.pending = None
                    self.event('ioc_confirmed', **report)
                    return report
                if self.clock()-started >= self.settlement_seconds:
                    raise ExecutionFault('IOC terminal quantity unproven; all inserts disabled')
                self.sleep(.05)
        except UnusableBook:
            raise  # The planner declined before any transport call.
        except BaseException:
            # Ctrl+C during insertion/settlement is also an uncertain outcome.
            self.halted = True
            raise
