"""Order limits for the synchronous strategy, independent of recording.

Use one LimitedExchange for all instruments and all order updates. It reuses the
existing connection and reads outstanding orders without polling trade streams.
"""

from collections import deque
from collections.abc import Mapping
import math
import operator
import time


class OrderLimitError(ValueError):
    """The proposed order would exceed a configured limit; nothing was sent."""


def _lots(value, *, name='volume', allow_zero=False):
    try:
        result = operator.index(value)
    except TypeError:
        raise ValueError(f'{name} must be an integer') from None
    if isinstance(value, bool) or result < (0 if allow_zero else 1):
        raise ValueError(f'{name} must be {"nonnegative" if allow_zero else "positive"}')
    return result


class UpdateRateLimiter:
    """Allow at most max_updates attempts in a rolling window, across symbols.

    The strategy is synchronous: call acquire() immediately before each update.
    Rejected requests and requests that raise still use their slot. Reads do not.
    Clock/sleep injection allows fully offline tests without real waiting.
    """

    def __init__(self, max_updates=25, window_seconds=1.0, *, clock=None, sleep=None):
        self.max_updates = _lots(max_updates, name='max_updates')
        if not math.isfinite(window_seconds) or window_seconds <= 0:
            raise ValueError('window_seconds must be finite and positive')
        self.window_seconds = window_seconds
        self._clock = clock if clock is not None else time.monotonic
        self._sleep = sleep if sleep is not None else time.sleep
        self._sent = deque()

    def acquire(self):
        while True:
            now = self._clock()
            while self._sent and now - self._sent[0] >= self.window_seconds:
                self._sent.popleft()
            if len(self._sent) < self.max_updates:
                self._sent.append(now)
                return
            # A small margin avoids landing just before the boundary due to
            # floating-point rounding or a sleep returning slightly early.
            self._sleep(self._sent[0] + self.window_seconds - now + 0.001)


class LimitedExchange:
    """Apply outstanding-volume and update-rate limits to one existing Exchange.

    Each insert counts its full requested size (also for IOC orders). Each amend
    replaces the target order's remaining size. Buy and sell volumes are added,
    never netted. Exceeding the limit raises OrderLimitError before transmission.

    Bulk cancellation is expanded into individual delete_order calls, so every
    actual cancellation is paced regardless of how the server counts batches.
    Use this adapter as the sole order sender in the single-threaded strategy.
    """

    def __init__(self, exchange, *, max_outstanding_volume=200,
                 max_updates_per_second=25, position_limit=100, limiter=None):
        self._exchange = exchange
        self.max_outstanding_volume = _lots(
            max_outstanding_volume, name='max_outstanding_volume')
        max_updates_per_second = _lots(
            max_updates_per_second, name='max_updates_per_second')
        if max_updates_per_second > 25:
            raise ValueError('max_updates_per_second cannot exceed the exchange limit of 25')
        # This is the exchange's absolute per-instrument pre-trade limit, not a
        # strategy target. Keep the final guard here so an oversized quote or a
        # future strategy configuration cannot bypass it.
        self.position_limit = _lots(position_limit, name='position_limit')
        self._limiter = limiter if limiter is not None else UpdateRateLimiter(
            max_updates=max_updates_per_second)

    def __getattr__(self, name):
        return getattr(self._exchange, name)

    def _outstanding_orders(self, instrument_id):
        orders = self._exchange.get_outstanding_orders(instrument_id)
        if not isinstance(orders, Mapping):
            raise RuntimeError(f'{instrument_id}: cannot verify outstanding orders')
        for order in orders.values():
            _lots(order.volume, name='outstanding volume', allow_zero=True)
        return orders

    def _check_total(self, instrument_id, total):
        if total > self.max_outstanding_volume:
            raise OrderLimitError(
                f'{instrument_id}: outstanding volume would be {total} lots; '
                f'limit is {self.max_outstanding_volume}')

    def _position(self, instrument_id):
        positions = self._exchange.get_positions()
        if not isinstance(positions, Mapping) or instrument_id not in positions:
            raise RuntimeError(f'{instrument_id}: cannot verify position')
        try:
            position = operator.index(positions[instrument_id])
        except TypeError:
            raise ValueError(f'{instrument_id}: position must be an integer') from None
        if isinstance(positions[instrument_id], bool):
            raise ValueError(f'{instrument_id}: position must be an integer')
        return position

    def _check_worst_position(self, instrument_id, side, orders, new_volume,
                              *, replaced_order_id=None):
        """Check position plus every same-side resting lot, without netting.

        Reading orders before position makes a concurrent same-side fill
        conservative: the filled lot can be counted in both snapshots, which
        may postpone an order but cannot permit an unsafe one.
        """
        same_side = sum(
            order.volume for order_id, order in orders.items()
            if order.side == side and order_id != replaced_order_id
        )
        position = self._position(instrument_id)
        worst = (position + same_side + new_volume if side == 'bid'
                 else position - same_side - new_volume)
        breached = (worst > self.position_limit if side == 'bid'
                    else worst < -self.position_limit)
        if breached:
            raise OrderLimitError(
                f'{instrument_id}: {side} would make worst-case position {worst}; '
                f'limit is +/-{self.position_limit}')

    def insert_order(self, instrument_id, *, price, volume, side, order_type='limit'):
        volume = _lots(volume)
        if side not in ('bid', 'ask') or order_type not in ('limit', 'ioc'):
            raise ValueError('Expected side bid/ask and order_type limit/ioc')
        orders = self._outstanding_orders(instrument_id)
        self._check_total(instrument_id, sum(order.volume for order in orders.values()) + volume)
        self._check_worst_position(instrument_id, side, orders, volume)
        self._limiter.acquire()
        return self._exchange.insert_order(
            instrument_id, price=price, volume=volume, side=side, order_type=order_type)

    def amend_order(self, instrument_id, *, order_id, volume):
        volume = _lots(volume, allow_zero=True)
        orders = self._outstanding_orders(instrument_id)
        if order_id not in orders:
            raise OrderLimitError(f'{instrument_id}: outstanding order {order_id} not found')
        total = sum(order.volume for order in orders.values()) - orders[order_id].volume + volume
        self._check_total(instrument_id, total)
        self._check_worst_position(
            instrument_id, orders[order_id].side, orders, volume,
            replaced_order_id=order_id)
        self._limiter.acquire()
        return self._exchange.amend_order(instrument_id, order_id=order_id, volume=volume)

    def delete_order(self, instrument_id, *, order_id):
        self._limiter.acquire()
        return self._exchange.delete_order(instrument_id, order_id=order_id)

    def delete_orders(self, instrument_id):
        # Snapshot IDs before deletions mutate the exchange's order dictionary.
        order_ids = tuple(self._outstanding_orders(instrument_id))
        first_error = None
        for order_id in order_ids:
            try:
                response = self.delete_order(instrument_id, order_id=order_id)
                if not response.success:
                    raise RuntimeError(
                        f'{instrument_id}: cancellation of {order_id} failed: '
                        f'{response.error_reason}')
            except Exception as error:
                # Try the remaining orders too; never report full cancellation
                # success if any request failed. The strategy handles the error.
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error


class QuoteManager:
    """Reconcile one desired quote against actual remaining exchange orders.

    Quote volumes are maximum live sizes, not instructions to replenish every
    partial fill. An order at the desired price is retained while its remaining
    volume fits the desired size and position limits. Excess volume and price
    changes require confirmed cancellation before insert. Do not use amendment
    here: the API sets a new remaining size, and a fill before processing could
    turn a planned reduction into replenishment; it has no documented atomic
    reduce-only operation.

    Use one manager over the existing LimitedExchange (or its recording
    adapter), as the sole sender. Reads and updates cannot be atomic with fills;
    fresh same-side worst-case checks supplement the exchange's position limit.
    No trade stream is polled here. The outer loop must handle exceptions and
    sleep; failed or unconfirmed updates never permit replacement that cycle.
    """

    def __init__(self, exchange, *, position_limit=100, soft_limit=100,
                 net_position_limit=200, net_symbols=('PHILIPS_A', 'PHILIPS_B')):
        self.exchange = exchange
        self.position_limit = _lots(position_limit, name='position_limit')
        self.soft_limit = _lots(soft_limit, name='soft_limit')
        if self.soft_limit > self.position_limit:
            raise ValueError('soft_limit cannot exceed position_limit')
        self.net_position_limit = _lots(net_position_limit, name='net_position_limit')
        self.net_symbols = tuple(net_symbols)
        if not self.net_symbols or len(set(self.net_symbols)) != len(self.net_symbols):
            raise ValueError('net_symbols must be nonempty and unique')

    @staticmethod
    def _price(value):
        if isinstance(value, bool):
            raise ValueError('price must be finite and positive')
        try:
            price = float(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError('price must be finite and positive') from None
        if not math.isfinite(price) or price <= 0:
            raise ValueError('price must be finite and positive')
        return price

    @staticmethod
    def _same_price(left, right):
        if left is None or right is None:
            return left is right
        # Treat a few representation ULPs as equal, without a relative price
        # tolerance that could collapse distinct tradable ticks at large prices.
        return abs(left - right) <= 4 * max(math.ulp(left), math.ulp(right))

    def _desired(self, quote):
        if quote is None or (isinstance(quote, Mapping) and not quote):
            return {'bid': (None, 0), 'ask': (None, 0)}
        if not isinstance(quote, Mapping):
            raise ValueError('quote must be a mapping or None')
        desired = {}
        reduce_only = quote.get('reduce_only', False)
        if type(reduce_only) is not bool:
            raise ValueError('reduce_only must be boolean')
        desired['reduce_only'] = reduce_only
        for side in ('bid', 'ask'):
            key = 'reduce_' + side
            value = quote.get(key, False)
            if type(value) is not bool:
                raise ValueError(key + ' must be boolean')
            desired[key] = value
        if 'target_position' in quote:
            target = quote['target_position']
            if isinstance(target, bool) or not isinstance(target, int) or abs(target) > self.position_limit:
                raise ValueError('target_position must be an integer within position limits')
            desired['target_position'] = target
        for side, key in (('bid', 'buy_volume'), ('ask', 'sell_volume')):
            volume = _lots(quote[key], name=key, allow_zero=True)
            price = self._price(quote[side + '_price']) if volume else None
            desired[side] = (price, volume)
        if (desired['bid'][1] and desired['ask'][1]
                and (desired['bid'][0] >= desired['ask'][0]
                     or self._same_price(desired['bid'][0], desired['ask'][0]))):
            raise ValueError('desired bid must be lower than desired ask')
        return desired

    def _orders(self, instrument_id):
        raw_orders = self.exchange.get_outstanding_orders(instrument_id)
        if not isinstance(raw_orders, Mapping):
            raise RuntimeError(f'{instrument_id}: cannot verify outstanding orders')
        # Copy fields, not just the mapping: the client's OrderStatus objects can
        # be updated when another synchronous request processes new messages.
        orders = {}
        for order_id, order in raw_orders.items():
            volume = _lots(order.volume, name='outstanding volume', allow_zero=True)
            if order.side not in ('bid', 'ask'):
                raise ValueError('outstanding order side must be bid or ask')
            orders[order_id] = (order.side, self._price(order.price), volume)
        return orders

    def _position(self, instrument_id):
        positions = self.exchange.get_positions()
        if not isinstance(positions, Mapping):
            raise RuntimeError(f'{instrument_id}: cannot verify positions')
        if instrument_id not in positions:
            raise RuntimeError(f'{instrument_id}: position missing from snapshot')
        value = positions[instrument_id]
        try:
            position = operator.index(value)
        except TypeError:
            raise ValueError('position must be an integer') from None
        if isinstance(value, bool):
            raise ValueError('position must be an integer')
        return position

    def _capacity(self, side, desired, position, instrument_id=None):
        net_room = self.net_position_limit
        if instrument_id is not None:
            if instrument_id not in self.net_symbols:
                raise ValueError('instrument outside the account net-limit scope')
            # Read resting exposure BEFORE positions. A fill between the reads
            # is conservatively counted twice, never omitted from both.
            other_live = sum(volume for iid in self.net_symbols if iid != instrument_id
                             for order_side, _, volume in self._orders(iid).values()
                             if order_side == side)
            positions = self.exchange.get_positions()
            if not isinstance(positions, Mapping) or any(i not in positions for i in self.net_symbols):
                raise RuntimeError('cannot verify A+B net position')
            quantities = {}
            for iid in self.net_symbols:
                value = positions[iid]
                if isinstance(value, bool):
                    raise ValueError('invalid account position')
                quantities[iid] = operator.index(value)
            position, net = quantities[instrument_id], sum(quantities.values())
            net_room = (self.net_position_limit - net if side == 'bid' else self.net_position_limit + net) - other_live
        if side == 'bid':
            room = 0 if position >= self.soft_limit else self.position_limit - position
        else:
            room = 0 if position <= -self.soft_limit else self.position_limit + position
        if desired.get('reduce_only', False) or desired.get('reduce_' + side, False):
            room = min(room, -position if side == 'bid' else position)
        if 'target_position' in desired:
            target = desired['target_position']
            room = min(room, target - position if side == 'bid' else position - target)
        return min(desired[side][1], max(0, min(room, net_room)))

    @staticmethod
    def _check_response(response, instrument_id, action, order_id=None):
        if not getattr(response, 'success', False):
            reason = getattr(response, 'error_reason', 'missing success response')
            raise RuntimeError(f'{instrument_id}: {action} {order_id} failed: {reason}')

    def _cancel(self, instrument_id, order_id, result):
        # A fill may have removed this order since the cancellation plan.
        if order_id not in self._orders(instrument_id):
            return
        response = self.exchange.delete_order(instrument_id, order_id=order_id)
        if (getattr(response, 'success', None) is False
                and getattr(response, 'error_reason', '') == 'Could not find order id to delete'
                and order_id not in self._orders(instrument_id)):
            # The order disappeared during the request. Subsequent capacity
            # checks reread positions; do not treat a fill race as a model fault.
            result['cancel_races'] = result.get('cancel_races', 0) + 1
            return
        self._check_response(response, instrument_id, 'cancel', order_id)
        result['cancelled'] += 1
        if order_id in self._orders(instrument_id):
            raise RuntimeError(f'{instrument_id}: cancellation of {order_id} not confirmed')

    def _verify(self, instrument_id, orders, desired, position):
        # Large/marketable orders can fill between separate orders/position
        # reads. Retry a stale check before treating it as a strategy fault.
        for _ in range(3):
            error = None
            for side in ('bid', 'ask'):
                side_orders = [order for order in orders.values() if order[0] == side and order[2]]
                if any(not self._same_price(order[1], desired[side][0]) for order in side_orders):
                    error = RuntimeError(f'{instrument_id}: old {side} price still outstanding')
                    break
                if sum(order[2] for order in side_orders) > self._capacity(side, desired, position, instrument_id):
                    error = OrderLimitError(f'{instrument_id}: {side} remaining volume exceeds current capacity')
                    break
            if error is None:
                return
            orders = self._orders(instrument_id)
            position = self._position(instrument_id)
        raise error

    def reconcile(self, instrument_id, quote):
        """Retain/reduce/cancel/insert as needed; return counts of these actions.

        ``quote`` supplies bid_price, ask_price, buy_volume, sell_volume. None or
        an empty mapping cancels this instrument without adding orders. Prices
        must already be on the instrument's tick grid; this manager does not
        change the caller's pricing or prevent external marketable limits.
        """
        desired = self._desired(quote)
        result = dict(retained=0, amended=0, cancelled=0, inserted=0)
        first_error = None

        # Complete removals on BOTH sides before adding either side, including
        # in a large repricing that would cross our own former opposite order.
        for order_id in tuple(self._orders(instrument_id)):
            try:
                order = self._orders(instrument_id).get(order_id)
                if order is None:
                    continue
                side, price, volume = order
                if not volume or not self._same_price(price, desired[side][0]):
                    self._cancel(instrument_id, order_id, result)
                elif self._capacity(side, desired, self._position(instrument_id), instrument_id) == 0:
                    self._cancel(instrument_id, order_id, result)
            except Exception as error:
                if first_error is None:
                    first_error = error

        # Preserve the earliest orders in the snapshot where possible. Cancel
        # from the end until total remaining fits. A surviving smaller order
        # is retained for ordinary market making. A cycle target can replace a
        # smaller order when A releases reserved capacity; cancel before refill.
        for side in ('bid', 'ask'):
            ids = [oid for oid, order in self._orders(instrument_id).items()
                   if order[0] == side]
            for order_id in reversed(ids):
                try:
                    orders = self._orders(instrument_id)
                    order = orders.get(order_id)
                    if order is None:
                        continue
                    capacity = self._capacity(side, desired, self._position(instrument_id), instrument_id)
                    total = sum(o[2] for o in orders.values() if o[0] == side)
                    expand_cycle = bool(desired.get('target_position')) and total < capacity
                    if total > capacity or expand_cycle:
                        self._cancel(instrument_id, order_id, result)
                except Exception as error:
                    if first_error is None:
                        first_error = error
        if first_error is not None:
            raise first_error

        orders = self._orders(instrument_id)
        if not desired['bid'][1] and not desired['ask'][1] and not orders:
            # Pure cancellation never needs to infer a missing position.
            return result
        self._verify(instrument_id, orders, desired, self._position(instrument_id))
        result['retained'] = sum(order[2] > 0 for order in orders.values())
        for side in ('bid', 'ask'):
            orders = self._orders(instrument_id)
            position = self._position(instrument_id)
            self._verify(instrument_id, orders, desired, position)
            if any(order[0] == side and order[2] for order in orders.values()):
                continue  # Never top up a partially filled resting order.
            volume = self._capacity(side, desired, position, instrument_id)
            if not volume:
                continue
            price = desired[side][0]
            opposite = [order[1] for order in orders.values()
                        if order[0] != side and order[2]]
            if any((price > p if side == 'bid' else price < p)
                   or self._same_price(price, p) for p in opposite):
                raise RuntimeError(f'{instrument_id}: new {side} would cross an own order')
            response = self.exchange.insert_order(
                instrument_id, price=price, volume=volume, side=side, order_type='limit')
            self._check_response(response, instrument_id, 'insert')
            result['inserted'] += 1
            self._verify(instrument_id, self._orders(instrument_id), desired,
                         self._position(instrument_id))
        return result
