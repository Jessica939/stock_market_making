"""Causal, per-instrument volatility estimation and quote adjustment.

The estimator uses only successive external-book mid prices.  Variance is
normalised by elapsed time, so changing the polling interval does not silently
change the meaning of the configured risk horizon.
"""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class VolatilitySettings:
    half_life_seconds: float = 10.0
    risk_horizon_seconds: float = 2.0
    min_interval_seconds: float = 0.05
    max_gap_seconds: float = 5.0
    min_samples: int = 5
    base_full_spread_ticks: float = 2.0
    sigma_multiplier: float = 2.0
    max_full_spread_ticks: float = 12.0
    reduce_size_sigma_ticks: float = 2.0
    halt_increasing_sigma_ticks: float = 5.0
    minimum_size_fraction: float = 0.25

    def __post_init__(self):
        for name in (
            'half_life_seconds', 'risk_horizon_seconds', 'min_interval_seconds',
            'max_gap_seconds', 'base_full_spread_ticks', 'sigma_multiplier',
            'max_full_spread_ticks', 'reduce_size_sigma_ticks',
            'halt_increasing_sigma_ticks', 'minimum_size_fraction',
        ):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0):
                raise ValueError(name + ' must be finite and positive')
        if type(self.min_samples) is not int or self.min_samples < 2:
            raise ValueError('min_samples must be an integer of at least 2')
        if self.min_interval_seconds >= self.max_gap_seconds:
            raise ValueError('min_interval_seconds must be less than max_gap_seconds')
        if self.base_full_spread_ticks > self.max_full_spread_ticks:
            raise ValueError('base spread cannot exceed maximum spread')
        if self.reduce_size_sigma_ticks >= self.halt_increasing_sigma_ticks:
            raise ValueError('size-reduction threshold must be below halt threshold')
        if self.minimum_size_fraction > 1:
            raise ValueError('minimum_size_fraction cannot exceed one')


@dataclass
class _State:
    timestamp: float
    mid: float
    tick: float
    variance_rate: float = 0.0
    samples: int = 1


class EWMAVolatility:
    """Maintain independent, timestamp-aware EWMA volatility for each symbol."""

    def __init__(self, settings=None):
        self.settings = settings or VolatilitySettings()
        self._states = {}

    def reset(self, instrument_id=None):
        if instrument_id is None:
            self._states.clear()
        else:
            self._states.pop(instrument_id, None)

    def _result(self, instrument_id, state, reason):
        sigma = math.sqrt(max(0.0, state.variance_rate)
                          * self.settings.risk_horizon_seconds)
        return dict(
            instrument=instrument_id,
            ready=state.samples >= self.settings.min_samples,
            reason=reason,
            samples=state.samples,
            sigma=sigma,
            sigma_ticks=sigma / state.tick,
            variance_rate=state.variance_rate,
            risk_horizon_seconds=self.settings.risk_horizon_seconds,
        )

    def observe(self, instrument_id, mid, timestamp, tick):
        if not isinstance(instrument_id, str) or not instrument_id:
            raise ValueError('instrument_id must be a non-empty string')
        for name, value in (('mid', mid), ('timestamp', timestamp), ('tick', tick)):
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0):
                raise ValueError(name + ' must be finite and positive')

        state = self._states.get(instrument_id)
        if state is None or state.tick != tick or timestamp < state.timestamp:
            state = _State(timestamp=float(timestamp), mid=float(mid), tick=float(tick))
            self._states[instrument_id] = state
            return self._result(instrument_id, state, 'reset')

        elapsed = timestamp - state.timestamp
        if elapsed == 0:
            if mid != state.mid:
                state = _State(timestamp=float(timestamp), mid=float(mid), tick=float(tick))
                self._states[instrument_id] = state
                return self._result(instrument_id, state, 'inconsistent_duplicate_reset')
            return self._result(instrument_id, state, 'duplicate')
        if elapsed > self.settings.max_gap_seconds:
            state = _State(timestamp=float(timestamp), mid=float(mid), tick=float(tick))
            self._states[instrument_id] = state
            return self._result(instrument_id, state, 'gap_reset')
        if elapsed < self.settings.min_interval_seconds:
            return self._result(instrument_id, state, 'interval_too_short')

        innovation_rate = (mid - state.mid) ** 2 / elapsed
        decay = math.exp(-math.log(2) * elapsed / self.settings.half_life_seconds)
        if state.samples == 1:
            state.variance_rate = innovation_rate
        else:
            state.variance_rate = (decay * state.variance_rate
                                   + (1 - decay) * innovation_rate)
        state.timestamp = float(timestamp)
        state.mid = float(mid)
        state.samples += 1
        return self._result(instrument_id, state, 'updated')


def apply_volatility_quote(quote, position, tick, volatility, settings):
    """Widen symmetrically and reduce only inventory-increasing quote sizes."""
    if quote is None:
        return None
    if (isinstance(position, bool) or not isinstance(position, int)
            or isinstance(tick, bool) or not isinstance(tick, (int, float))
            or not math.isfinite(tick) or tick <= 0):
        raise ValueError('invalid position or tick')
    sigma = volatility.get('sigma', 0.0)
    ready = volatility.get('ready') is True
    if (isinstance(sigma, bool) or not isinstance(sigma, (int, float))
            or not math.isfinite(sigma) or sigma < 0):
        raise ValueError('volatility sigma must be finite and non-negative')

    out = dict(quote)
    effective_sigma = sigma if ready else 0.0
    requested_full_spread = (settings.base_full_spread_ticks * tick
                             + settings.sigma_multiplier * effective_sigma)
    volatility_full_spread = min(
        settings.max_full_spread_ticks * tick, requested_full_spread)
    half_spread = max(out['half_spread'], volatility_full_spread / 2)
    center = out['center']
    bid = round(math.floor((center - half_spread) / tick + 1e-9) * tick, 10)
    ask = round(math.ceil((center + half_spread) / tick - 1e-9) * tick, 10)
    out['bid_price'] = min(out['bid_price'], bid)
    out['ask_price'] = max(out['ask_price'], ask)
    out['half_spread'] = half_spread

    sigma_ticks = effective_sigma / tick
    if sigma_ticks < settings.reduce_size_sigma_ticks:
        size_fraction = 1.0
    elif sigma_ticks >= settings.halt_increasing_sigma_ticks:
        size_fraction = 0.0
    else:
        span = (settings.halt_increasing_sigma_ticks
                - settings.reduce_size_sigma_ticks)
        progress = (sigma_ticks - settings.reduce_size_sigma_ticks) / span
        size_fraction = max(
            settings.minimum_size_fraction,
            1 - progress * (1 - settings.minimum_size_fraction),
        )

    original_buy, original_sell = out['buy_volume'], out['sell_volume']
    # Buying reduces a short; selling reduces a long.  Never throttle the
    # risk-reducing side merely because volatility is high.
    if position >= 0:
        out['buy_volume'] = (0 if size_fraction == 0 else
                             math.ceil(original_buy * size_fraction - 1e-9))
    if position <= 0:
        out['sell_volume'] = (0 if size_fraction == 0 else
                              math.ceil(original_sell * size_fraction - 1e-9))
    if out['bid_price'] <= 0:
        out['buy_volume'] = 0
    out['volatility'] = dict(
        **volatility,
        effective_sigma=effective_sigma,
        target_full_spread=requested_full_spread,
        applied_full_spread=2 * half_spread,
        size_fraction=size_fraction,
        halted_increasing_sides=size_fraction == 0,
    )
    return out

