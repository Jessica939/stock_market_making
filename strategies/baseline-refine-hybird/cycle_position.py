"""Large cycle entries, early profit targets and persistent full-position exits."""
import math

from .quote_protection import competitive_price


class CyclePosition:
    def __init__(self, *, target_lots=100, reference_hold_seconds=45,
                 entry_window_seconds=10, retry_seconds=1, cooldown_seconds=2,
                 take_profit_fraction=.8, take_profit_buffer_ticks=2,
                 stop_ticks=60, stop_confirmation_seconds=3,
                 exit_cross_after_seconds=2, exit_sweep_ticks=10,
                 edge_buffer_ticks=1):
        if type(target_lots) is not int or not 1 <= target_lots <= 100:
            raise ValueError('target_lots must be an integer in 1..100')
        values = (reference_hold_seconds, entry_window_seconds, retry_seconds, cooldown_seconds,
                  take_profit_fraction, take_profit_buffer_ticks, stop_ticks,
                  stop_confirmation_seconds, exit_cross_after_seconds,
                  exit_sweep_ticks, edge_buffer_ticks)
        if any(isinstance(v, bool) or not math.isfinite(v) or v <= 0 for v in values):
            raise ValueError('cycle position settings must be finite and positive')
        if take_profit_fraction >= 1:
            raise ValueError('profit fraction must be below one')
        self.target_lots = target_lots
        self.reference_hold_seconds, self.entry_window = reference_hold_seconds, entry_window_seconds
        self.retry_seconds, self.cooldown_seconds = retry_seconds, cooldown_seconds
        self.take_profit_fraction = take_profit_fraction
        self.take_profit_buffer_ticks = take_profit_buffer_ticks
        self.stop_ticks, self.stop_confirmation_seconds = stop_ticks, stop_confirmation_seconds
        self.exit_cross_after_seconds, self.exit_sweep_ticks = exit_cross_after_seconds, exit_sweep_ticks
        self.edge_buffer_ticks = edge_buffer_ticks
        self.active = None
        self.next_entry = -math.inf
        self.untracked_exit_started = None

    def _entry_plan(self, book, tick, now, signal):
        """Build the same frozen cycle plan for flat or existing MM inventory."""
        mid = (book.bids[0].price + book.asks[0].price) / 2
        prediction = signal.get('predicted_B_change', 0) * signal.get('fit_weight', 0)
        sign = 1 if prediction > 0 else -1
        profit_move = abs(prediction) * self.take_profit_fraction - self.take_profit_buffer_ticks * tick
        target = mid + sign * profit_move
        target = round((math.floor if sign > 0 else math.ceil)(target / tick + sign * 1e-9) * tick, 10)
        entry = competitive_price(book, tick, 'bid' if sign > 0 else 'ask')
        if (signal.get('active') and profit_move > 0
                and sign * (target - entry) > self.edge_buffer_ticks * tick + 1e-9):
            return dict(sign=sign, target_price=target,
                model_target_price=mid + prediction, entry_mid=mid, filled=False,
                opened_at=now, reference_at=now + self.reference_hold_seconds,
                build_until=now + self.entry_window, exit_reason=None,
                exit_started=None, adverse_since=None)
        return None

    def apply(self, quote, book, position, tick, now, *, maker_quote=None):
        """Freeze profit targets; elapsed reference time is not an exit signal.

        Entries request all remaining target shares. Exits request the entire
        remaining position; partial fills never reset the exit timer.
        """
        out = dict(quote)
        out.update(buy_volume=0, sell_volume=0, reduce_only=False)
        bid, ask = book.bids[0].price, book.asks[0].price
        mid = (bid + ask) / 2
        signal = quote.get('cycle', {})
        held = self.active
        if held and held['filled'] and position == 0:
            self.active = held = None
            self.next_entry = now + self.cooldown_seconds
        if held and not position and now >= held['build_until']:
            self.active = held = None
            self.next_entry = now + self.retry_seconds
        if not position:
            self.untracked_exit_started = None
        if held is None and not position and now >= self.next_entry:
            held = self.active = self._entry_plan(book, tick, now, signal)

        reason = 'waiting_for_cycle'
        if position and held is None:
            reason = 'untracked_inventory'
            if self.untracked_exit_started is None:
                self.untracked_exit_started = now
        elif held:
            held['filled'] |= bool(position)
            if not held['exit_reason']:
                if position and (position * held['sign'] < 0 or abs(position) > self.target_lots):
                    held['exit_reason'] = 'unexpected_inventory'
                elif position and held['sign'] * ((bid if held['sign'] > 0 else ask) - held['target_price']) >= -1e-9:
                    held['exit_reason'] = 'cycle_take_profit'
                # Require a sustained excursion, not a single mid-price spike.
                adverse = bool(position) and held['sign'] * (mid - held['entry_mid']) <= -self.stop_ticks * tick
                if adverse:
                    if held['adverse_since'] is None or now - held.get('last_observed_at', now) > 1.5:
                        held['adverse_since'] = now
                    if now - held['adverse_since'] >= self.stop_confirmation_seconds:
                        held['exit_reason'] = held['exit_reason'] or 'cycle_position_stop'
                else:
                    held['adverse_since'] = None
                if held['exit_reason']:
                    held['exit_started'] = now
            held['last_observed_at'] = now
            reason = held['exit_reason'] or 'hold_cycle_position'
            side = 'bid' if held['sign'] > 0 else 'ask'
            entry = competitive_price(book, tick, side)
            if (not held['exit_reason'] and now < held['build_until']
                    and abs(position) < self.target_lots and signal.get('active')
                    and held['sign'] * signal.get('predicted_B_change', 0) > 0
                    and held['sign'] * (held['target_price'] - entry) > self.edge_buffer_ticks * tick + 1e-9):
                key = 'buy_volume' if side == 'bid' else 'sell_volume'
                out[key] = min(quote[key], self.target_lots - abs(position))
                out[side + '_price'] = entry
                reason = 'build_cycle_position'

        exit_mode = None
        if position and (held is None or held['exit_reason']):
            started = held['exit_started'] if held else self.untracked_exit_started
            side = 'ask' if position > 0 else 'bid'
            if now - started >= self.exit_cross_after_seconds:
                # Marketable LIMIT; refresh against the latest opposing touch.
                price = bid - self.exit_sweep_ticks * tick if side == 'ask' else ask + self.exit_sweep_ticks * tick
                price = max(tick, price)
                exit_mode = 'cross_opposing_book'
            else:
                price = competitive_price(book, tick, side)
                exit_mode = 'improve_best'
            out[side + '_price'] = round(price, 10)
            out['sell_volume' if position > 0 else 'buy_volume'] = abs(position)
            out['reduce_only'] = True

        out['target_position'] = held['sign'] * self.target_lots if held and not held['exit_reason'] else 0
        out['cycle_position'] = dict(reason=reason, target_lots=self.target_lots,
            target_position=out['target_position'], target_price=held['target_price'] if held else None,
            model_target_price=held['model_target_price'] if held else None,
            entry_mid=held['entry_mid'] if held else None,
            hold_reference_at=held['reference_at'] if held else None,
            hold_reference_elapsed=bool(held and now >= held['reference_at']),
            exit_started=held['exit_started'] if held else self.untracked_exit_started,
            exit_mode=exit_mode, adverse_since=held['adverse_since'] if held else None,
            next_entry=self.next_entry if math.isfinite(self.next_entry) else None)
        return out
