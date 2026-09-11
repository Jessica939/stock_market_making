"""Conservative B quote protection driven by the existing cycle signal.

Defaults are experiment settings, not calibrated profitability claims.  The
policy is deliberately passive: it can suppress or widen a quote, but it never
crosses the book to force an exit.
"""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class ProtectionSettings:
    max_increasing_volume: int = 2
    max_abs_position: int = 10
    min_half_spread_ticks: float = 3.0
    min_reducing_half_spread_ticks: float = 3.0
    adverse_extra_ticks: float = 1.0
    adverse_threshold_ticks: float = 0.5
    strong_adverse_threshold_ticks: float = 1.0
    adverse_size_fraction: float = 0.5
    small_position_limit: int = 10
    adverse_min_hold_seconds: float = 2.0

    def __post_init__(self):
        if type(self.max_increasing_volume) is not int or not 1 <= self.max_increasing_volume <= 100:
            raise ValueError('max_increasing_volume must be an integer 1..100')
        for name in ('max_abs_position', 'small_position_limit'):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 100:
                raise ValueError(name + ' must be an integer 1..100')
        if self.small_position_limit > self.max_abs_position:
            raise ValueError('small_position_limit cannot exceed max_abs_position')
        for name in ('min_half_spread_ticks', 'min_reducing_half_spread_ticks',
                     'adverse_extra_ticks', 'adverse_threshold_ticks',
                     'strong_adverse_threshold_ticks', 'adverse_size_fraction',
                     'adverse_min_hold_seconds'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(name + ' must be finite and positive')
        if self.adverse_size_fraction > 1:
            raise ValueError('adverse_size_fraction must be <= 1')
        if self.strong_adverse_threshold_ticks < self.adverse_threshold_ticks:
            raise ValueError('strong adverse threshold must be at least the adverse threshold')


class PositionAgeTracker:
    """Track a B inventory episode from observed positions, without fill guesses.

    A nonzero position in the first snapshot is inherited and is therefore
    treated as mature.  A sign change starts a new episode because it crossed
    flat between observations.
    """
    def __init__(self):
        self.initialized = False
        self.opened_at = None
        self.sign = 0

    def observe(self, position, now):
        if type(position) is not int:
            raise ValueError('position must be an integer')
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
            raise ValueError('now must be finite')
        sign = 1 if position > 0 else -1 if position < 0 else 0
        if not self.initialized:
            self.initialized = True
            self.sign = sign
            self.opened_at = None
            return None
        if sign == 0:
            self.sign = 0
            self.opened_at = None
            return None
        if self.sign == 0 or self.sign != sign:
            self.sign = sign
            self.opened_at = now
            return 0.0
        if self.opened_at is None:
            return None
        if now < self.opened_at:
            self.opened_at = now
            return 0.0
        return now - self.opened_at


def _widen(out, book, side, tick, half):
    """Move one side outward from the strategy center, never inward."""
    key = side + '_price'
    old = out[key]
    if side == 'bid':
        raw = min(old, out['center'] - half, book.asks[0].price - tick)
        out[key] = round(math.floor(raw / tick + 1e-9) * tick, 10)
    else:
        raw = max(old, out['center'] + half, book.bids[0].price + tick)
        out[key] = round(math.ceil(raw / tick - 1e-9) * tick, 10)
    return out[key] != old


def protect_quote(quote, book, position, tick, instrument_id, settings, *, position_age=None):
    if quote is None or instrument_id != 'PHILIPS_B':
        return quote
    out = dict(quote)
    active = out.get('cycle', {}).get('active') is True
    out['reduce_only'] = not active
    if position_age is not None and (isinstance(position_age, bool)
            or not isinstance(position_age, (int, float))
            or not math.isfinite(position_age) or position_age < 0):
        raise ValueError('position_age must be finite and non-negative')
    diagnostics = dict(mode='active' if active else 'reduce_only',
                       position_age=position_age, widened_sides=[], reduced_sides=[],
                       blocked_sides=[], reasons={})
    shift = out.get('cycle_risk_shift', out.get('cycle_shift', 0.0))
    for side, size_key, reducing in (('bid', 'buy_volume', position < 0),
                                      ('ask', 'sell_volume', position > 0)):
        original_size = out[size_key]
        adverse = (shift <= -settings.adverse_threshold_ticks * tick if side == 'bid'
                   else shift >= settings.adverse_threshold_ticks * tick)
        strongly_adverse = (shift <= -settings.strong_adverse_threshold_ticks * tick if side == 'bid'
                            else shift >= settings.strong_adverse_threshold_ticks * tick)
        if reducing:
            # Do not let a reducing quote cross flat into fresh exposure.
            out[size_key] = min(original_size, abs(position))
            # The replay-supported guard applies only to ordinary small B
            # inventory while the cycle model is active.  Larger or inherited
            # inventory remains immediately reducible under the old risk path.
            if active and abs(position) <= settings.small_position_limit:
                half = settings.min_reducing_half_spread_ticks * tick
                if adverse:
                    half += settings.adverse_extra_ticks * tick
                if _widen(out, book, side, tick, half):
                    diagnostics['widened_sides'].append(side)
                    diagnostics['reasons'][side] = 'reducing_min_distance'
                if (strongly_adverse and position_age is not None
                        and position_age < settings.adverse_min_hold_seconds):
                    out[size_key] = 0
                    diagnostics['blocked_sides'].append(side)
                    diagnostics['reasons'][side] = 'young_inventory_against_strong_signal'
        elif not active:
            out[size_key] = 0
        else:
            # Unlike the account-wide executor limit, this is B's experimental
            # inventory cap.  QuoteManager treats the result as remaining size,
            # so outstanding same-side exposure is not replenished blindly.
            room = max(0, settings.max_abs_position - abs(position))
            out[size_key] = min(original_size, settings.max_increasing_volume, room)
            if strongly_adverse:
                out[size_key] = 0
                diagnostics['blocked_sides'].append(side)
                diagnostics['reasons'][side] = 'entry_against_strong_signal'
            elif adverse and out[size_key]:
                out[size_key] = max(1, math.floor(out[size_key] * settings.adverse_size_fraction))
            # Widen around the chosen center, never tighten the quote.
            half = max(out['half_spread'], settings.min_half_spread_ticks * tick)
            half += settings.adverse_extra_ticks * tick if adverse else 0
            if _widen(out, book, side, tick, half):
                diagnostics['widened_sides'].append(side)
        if out[size_key] != original_size:
            diagnostics['reduced_sides'].append(side)
    if out['bid_price'] <= 0:
        out['buy_volume'] = 0
    out['protection'] = diagnostics
    return out
