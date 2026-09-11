"""Conservative B quote protection; offline, independent of the cycle fit.

Defaults are experiment settings, not calibrated profitability claims.
Reducing quotes keep their prices and sizes; only new inventory is throttled.
"""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class ProtectionSettings:
    max_increasing_volume: int = 2
    min_half_spread_ticks: float = 3.0
    adverse_extra_ticks: float = 1.0
    adverse_threshold_ticks: float = 0.5
    adverse_size_fraction: float = 0.5

    def __post_init__(self):
        if type(self.max_increasing_volume) is not int or not 1 <= self.max_increasing_volume <= 100:
            raise ValueError('max_increasing_volume must be an integer 1..100')
        for name in ('min_half_spread_ticks', 'adverse_extra_ticks',
                     'adverse_threshold_ticks', 'adverse_size_fraction'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(name + ' must be finite and positive')
        if self.adverse_size_fraction > 1:
            raise ValueError('adverse_size_fraction must be <= 1')


def protect_quote(quote, book, position, tick, instrument_id, settings):
    if quote is None or instrument_id != 'PHILIPS_B':
        return quote
    out = dict(quote)
    active = out.get('cycle', {}).get('active') is True
    out['reduce_only'] = not active
    diagnostics = dict(mode='active' if active else 'reduce_only',
                       widened_sides=[], reduced_sides=[])
    for side, size_key, reducing in (('bid', 'buy_volume', position < 0),
                                      ('ask', 'sell_volume', position > 0)):
        original_size = out[size_key]
        if reducing:
            # Do not let a reducing quote cross flat into fresh exposure.
            out[size_key] = min(original_size, abs(position))
        elif not active:
            out[size_key] = 0
        else:
            out[size_key] = min(original_size, settings.max_increasing_volume)
            shift = out.get('cycle_risk_shift', out.get('cycle_shift', 0.0))
            adverse = (shift <= -settings.adverse_threshold_ticks * tick if side == 'bid'
                       else shift >= settings.adverse_threshold_ticks * tick)
            if adverse and out[size_key]:
                out[size_key] = max(1, math.floor(out[size_key] * settings.adverse_size_fraction))
            # Widen around the chosen center, never tighten the quote.
            half = max(out['half_spread'], settings.min_half_spread_ticks * tick)
            half += settings.adverse_extra_ticks * tick if adverse else 0
            key = side + '_price'
            old = out[key]
            if side == 'bid':
                raw = min(old, out['center'] - half, book.asks[0].price - tick)
                out[key] = round(math.floor(raw / tick + 1e-9) * tick, 10)
            else:
                raw = max(old, out['center'] + half, book.bids[0].price + tick)
                out[key] = round(math.ceil(raw / tick - 1e-9) * tick, 10)
            if out[key] != old:
                diagnostics['widened_sides'].append(side)
        if out[size_key] != original_size:
            diagnostics['reduced_sides'].append(side)
    if out['bid_price'] <= 0:
        out['buy_volume'] = 0
    out['protection'] = diagnostics
    return out
