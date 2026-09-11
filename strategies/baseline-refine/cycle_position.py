"""Turn a B cycle forecast into one bounded inventory plan, using actual positions."""
import math


class CyclePosition:
    def __init__(self, *, target_lots=20, order_lots=5, hold_seconds=45,
                 entry_window_seconds=10, entry_interval_seconds=45,
                 stop_ticks=20, edge_buffer_ticks=1):
        if (type(target_lots) is not int or type(order_lots) is not int
                or not 1 <= order_lots <= target_lots <= 100):
            raise ValueError('require 1 <= order_lots <= target_lots <= 100')
        for value in (hold_seconds, entry_window_seconds, entry_interval_seconds,
                      stop_ticks, edge_buffer_ticks):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError('cycle position settings must be finite and positive')
        if entry_window_seconds >= hold_seconds or entry_interval_seconds < hold_seconds:
            raise ValueError('entry window must end before holding deadline; interval must cover holding')
        self.target_lots, self.order_lots = target_lots, order_lots
        self.hold_seconds, self.entry_window = hold_seconds, entry_window_seconds
        self.entry_interval, self.stop_ticks = entry_interval_seconds, stop_ticks
        self.edge_buffer_ticks = edge_buffer_ticks
        self.active = None
        self.next_entry = -math.inf

    def apply(self, quote, book, position, tick, now):
        """Only the entry side may build inventory; exits latch until flat.

        Deadlines start with the first quote intent and never roll with refits or
        additional fills. Inherited inventory is reduced, never silently adopted.
        """
        out = dict(quote)
        out.update(buy_volume=0, sell_volume=0)
        mid = (book.bids[0].price + book.asks[0].price) / 2
        cost = book.asks[0].price - book.bids[0].price + self.edge_buffer_ticks * tick
        held = self.active
        if held and held['filled'] and position == 0:
            self.active = held = None
        if held and not position and now >= held['build_until']:
            self.active = held = None
        if held is None and not position and now >= self.next_entry:
            signal = quote.get('cycle', {})
            prediction = signal.get('predicted_B_change', 0) * signal.get('fit_weight', 0)
            if signal.get('active') and abs(prediction) > cost:
                held = self.active = dict(sign=1 if prediction > 0 else -1,
                    target_price=mid + prediction, entry_mid=mid, filled=False,
                    opened_at=now, exit_at=now + self.hold_seconds,
                    build_until=now + self.entry_window, exit_reason=None)
                self.next_entry = now + self.entry_interval
        reason = 'waiting_for_cycle'
        if position and held is None:
            reason = 'untracked_inventory'
        elif held:
            held['filled'] |= bool(position)
            if position and position * held['sign'] < 0:
                held['exit_reason'] = 'unexpected_inventory'
            if abs(position) > self.target_lots:
                held['exit_reason'] = 'unexpected_inventory'
            if position and held['sign'] * (mid - held['entry_mid']) <= -self.stop_ticks * tick:
                held['exit_reason'] = 'cycle_position_stop'
            if now >= held['exit_at']:
                held['exit_reason'] = held['exit_reason'] or 'cycle_horizon_exit'
            reason = held['exit_reason'] or 'hold_cycle_position'
            room = self.target_lots - abs(position)
            if (not held['exit_reason'] and now < held['build_until'] and room > 0
                    and held['sign'] * (held['target_price'] - mid) > cost):
                key = 'buy_volume' if held['sign'] > 0 else 'sell_volume'
                # Preserve price/size protection from the quote pipeline. A
                # temporarily inactive fit may pause building, never force exit.
                out[key] = min(quote[key], self.order_lots, room)
                reason = 'build_cycle_position'
        if position and (held is None or held['exit_reason']):
            key = 'sell_volume' if position > 0 else 'buy_volume'
            out[key] = min(quote[key], self.order_lots, abs(position))
            out['reduce_only'] = True
        out['target_position'] = held['sign'] * self.target_lots if held and not held['exit_reason'] else 0
        out['cycle_position'] = dict(reason=reason, target_lots=self.target_lots,
            target_position=out['target_position'],
            exit_at=held['exit_at'] if held else None, next_entry=self.next_entry if math.isfinite(self.next_entry) else None)
        return out
