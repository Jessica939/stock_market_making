"""Causal A-minus-B cycle estimate and bounded passive quote overlay.

No exchange calls or external dependencies. A fixed period is a hypothesis;
amplitude and phase are fitted from recent history and live observations.
Fit quality controls overlay strength, not a mandatory holdout admission gate.
"""
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import math


@dataclass(frozen=True)
class CycleSettings:
    enabled: bool = True
    period_seconds: float = 180.0
    history_seconds: float = 720.0
    min_fit_seconds: float = 30.0
    ramp_seconds: float = 180.0
    sample_seconds: float = 1.0
    refit_seconds: float = 5.0
    horizon_seconds: float = 15.0
    max_gap_seconds: float = 10.0
    max_age_seconds: float = 2.0
    max_pair_gap_seconds: float = 0.75
    max_spread_ticks: float = 20.0
    min_amplitude_ticks: float = 5.0
    max_amplitude_ticks: float = 80.0
    residual_sigma: float = 3.0
    quote_gain: float = 0.5
    max_quote_shift_ticks: float = 3.0
    adverse_size_fraction: float = 0.5

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError('enabled must be boolean')
        for name in self.__dataclass_fields__:
            if name == 'enabled':
                continue
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(name + ' must be finite and positive')
        if not self.min_fit_seconds <= self.ramp_seconds <= self.history_seconds:
            raise ValueError('require min_fit_seconds <= ramp_seconds <= history_seconds')
        if self.sample_seconds > self.period_seconds / 20:
            raise ValueError('sample interval is too long for the cycle')
        if self.horizon_seconds > self.period_seconds / 2:
            raise ValueError('forecast horizon must be at most a half cycle')
        if (self.adverse_size_fraction > 1 or self.quote_gain > 1
                or self.min_amplitude_ticks >= self.max_amplitude_ticks):
            raise ValueError('invalid cycle thresholds')


def book_time(book):
    stamp = getattr(book, 'timestamp', None)
    if not isinstance(stamp, datetime):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def usable_book(book, tick, wall, settings, *, check_spread=True):
    """Require fresh two-sided, positive, sorted, grid-aligned external depth."""
    try:
        stamp = book_time(book)
        if (not math.isfinite(tick) or tick <= 0 or not math.isfinite(wall)
                or stamp is None or not -0.5 <= wall - stamp <= settings.max_age_seconds
                or not book.bids or not book.asks):
            return False
        for side, levels in (('bid', book.bids), ('ask', book.asks)):
            previous = math.inf if side == 'bid' else -math.inf
            for level in levels:
                price, volume = level.price, level.volume
                if (isinstance(price, bool) or not math.isfinite(price) or price <= 0
                        or isinstance(volume, bool) or not isinstance(volume, int) or volume <= 0
                        or abs(price / tick - round(price / tick)) > 1e-6
                        or (price >= previous if side == 'bid' else price <= previous)):
                    return False
                previous = price
        spread = book.asks[0].price - book.bids[0].price
        return 0 < spread and (not check_spread or spread <= settings.max_spread_ticks * tick + 1e-9)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return False


def _solve(matrix, rhs):
    """Pivoted 3x3 solve for the intercept/sine/cosine normal equations."""
    aug = [list(row) + [value] for row, value in zip(matrix, rhs)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda r: abs(aug[r][col]))
        aug[col], aug[pivot] = aug[pivot], aug[col]
        if abs(aug[col][col]) < 1e-10:
            return None
        scale = aug[col][col]
        aug[col] = [v / scale for v in aug[col]]
        for row in range(3):
            if row != col:
                scale = aug[row][col]
                aug[row] = [v - scale * w for v, w in zip(aug[row], aug[col])]
    result = [row[-1] for row in aug]
    return result if all(math.isfinite(v) for v in result) else None


class CycleSignal:
    def __init__(self, settings=None):
        self.settings = settings or CycleSettings()
        self.reset()

    def reset(self):
        self.history = deque()
        self.origin = None
        self.last_now = self.last_sample = self.last_fit = -math.inf
        self.stamps = None
        self.ticks = None
        self.weights = None
        self.quality = {}
        self.reason = 'warmup'
        self.bootstrap = {}
        self.seed_pending = False
        self.seed_deadline = -math.inf

    def seed(self, rows, ticks, now, wall, *, source, max_age_seconds=180.0):
        """Fit past (UTC seconds, A-minus-B) samples in the live clock frame.

        Keep absolute elapsed time: old sessions are never shifted to now.
        Admission still checks sample coverage, amplitude and the live residual.
        A rejected seed leaves the current model untouched.
        """
        if (not all(math.isfinite(v) for v in (now, wall, max_age_seconds))
                or max_age_seconds <= 0):
            return dict(loaded=False, reason='invalid_clock', source=source)
        cfg = self.settings
        cleaned = {}
        for stamp, basis in rows:
            if (math.isfinite(stamp) and math.isfinite(basis)
                    and wall - cfg.history_seconds <= stamp <= wall):
                cleaned[stamp] = basis
        sampled = []
        for stamp, basis in sorted(cleaned.items()):
            if not sampled or stamp - sampled[-1][0] >= cfg.sample_seconds:
                sampled.append((stamp, basis))
        report = dict(source=source, samples=len(sampled), loaded=False)
        if not sampled:
            return dict(report, reason='no_recent_history')
        report.update(age_seconds=wall-sampled[-1][0],
                      span_seconds=sampled[-1][0]-sampled[0][0])
        if report['age_seconds'] > max_age_seconds:
            return dict(report, reason='stale_history')
        candidate = CycleSignal(cfg)
        candidate.history = deque((now + (stamp-wall), y) for stamp, y in sampled)
        candidate.origin = candidate.history[0][0]
        candidate.last_sample = candidate.history[-1][0]
        candidate.last_now = now
        candidate.ticks = tuple(ticks[s] for s in ('PHILIPS_A', 'PHILIPS_B'))
        if any(not math.isfinite(t) or t <= 0 for t in candidate.ticks):
            return dict(report, reason='invalid_ticks')
        candidate._refit(now, max(candidate.ticks))
        report.update(reason=candidate.reason, **candidate.quality)
        if candidate.weights is None:
            return report
        report['loaded'] = True
        candidate.bootstrap = dict(report)
        candidate.seed_pending = True
        candidate.seed_deadline = now + cfg.max_gap_seconds
        self.__dict__.update(candidate.__dict__)
        return report

    def _x(self, t):
        angle = 2 * math.pi * ((t - self.origin) % self.settings.period_seconds) / self.settings.period_seconds
        return [1.0, math.sin(angle), math.cos(angle)]

    def _predict(self, t, weights):
        return sum(x * w for x, w in zip(self._x(t), weights))

    def _fit(self, rows):
        matrix = [[0.0] * 3 for _ in range(3)]
        rhs = [0.0] * 3
        for t, y in rows:
            x = self._x(t)
            for i in range(3):
                rhs[i] += x[i] * y
                for j in range(3):
                    matrix[i][j] += x[i] * x[j]
        return _solve(matrix, rhs)

    def _refit(self, now, tick):
        cfg = self.settings
        self.last_fit = now
        self.weights = None
        rows = list(self.history)
        # Just enough observations to estimate three coefficients; no holdout
        # wait. The 30-second partial-cycle fit starts with a small weight.
        self.quality = {}
        if len(rows) < 20 or rows[-1][0] - rows[0][0] < cfg.min_fit_seconds:
            self.reason = 'warmup'
            return
        weights = self._fit(rows)
        if weights is None:
            self.reason = 'singular_fit'
            return
        errors = [(y - self._predict(t, weights)) ** 2 for t, y in rows]
        mse = sum(errors) / len(errors)
        mean = sum(y for _, y in rows) / len(rows)
        variance = sum((y - mean) ** 2 for _, y in rows) / len(rows)
        r2 = 1 - mse / variance if variance > 1e-12 else -1.0
        amplitude = math.hypot(weights[1], weights[2])
        coverage = min(1.0, (rows[-1][0] - rows[0][0]) / cfg.ramp_seconds)
        fit_weight = (coverage * max(0.0, min(1.0, r2))
                      / (1 + math.sqrt(mse) / max(amplitude, tick)))
        self.quality = dict(fit_rmse=math.sqrt(mse), fit_r2=r2,
                            fitted_amplitude=amplitude, fit_samples=len(rows),
                            fit_weight=fit_weight,
                            quality_scope='in_sample_fit_not_forecast_accuracy')
        if not cfg.min_amplitude_ticks * tick <= amplitude <= cfg.max_amplitude_ticks * tick:
            self.reason = 'amplitude_out_of_bounds'
            return
        self.weights = weights
        self.reason = 'active'

    def observe(self, books, ticks, now, wall):
        cfg = self.settings
        def result(reason, **extra):
            return dict(active=False, reason=reason, period_seconds=cfg.period_seconds,
                        horizon_seconds=cfg.horizon_seconds, bootstrap=self.bootstrap,
                        **self.quality, **extra)
        if not cfg.enabled:
            return result('disabled')
        if not math.isfinite(now) or not math.isfinite(wall):
            self.reset()
            return result('invalid_clock')
        if now < self.last_now:
            self.reset()
            return result('clock_reversal')
        self.last_now = now
        symbols = ('PHILIPS_A', 'PHILIPS_B')
        if any(not usable_book(books.get(s), ticks.get(s, 0), wall, cfg) for s in symbols):
            return result('invalid_pair_books')
        stamps = tuple(book_time(books[s]) for s in symbols)
        if abs(stamps[0] - stamps[1]) > cfg.max_pair_gap_seconds:
            return result('unsynchronized_books')
        tick_pair = tuple(ticks[s] for s in symbols)
        if self.ticks is not None and tick_pair != self.ticks:
            self.reset()
            return result('tick_change')
        if self.stamps is not None and any(a < b for a, b in zip(stamps, self.stamps)):
            self.reset()
            return result('book_time_reversal')
        if (now - self.last_sample > cfg.max_gap_seconds
                and not (self.seed_pending and now <= self.seed_deadline)):
            self.reset()
        self.last_now = now
        self.ticks = tick_pair
        if self.origin is None:
            self.origin = now
        mids = [(books[s].bids[0].price + books[s].asks[0].price) / 2 for s in symbols]
        basis = mids[0] - mids[1]
        new = self.stamps is None or all(a > b for a, b in zip(stamps, self.stamps))
        if new and now - self.last_sample >= cfg.sample_seconds:
            self.history.append((now, basis))
            self.last_sample = now
            self.stamps = stamps
            self.seed_pending = False
            while self.history and now - self.history[0][0] > cfg.history_seconds:
                self.history.popleft()
            if now - self.last_fit >= cfg.refit_seconds:
                self._refit(now, max(tick_pair))
        if self.weights is None:
            return result(self.reason, samples=len(self.history))
        fitted = self._predict(now, self.weights)
        residual = basis - fitted
        bound = max(3 * max(tick_pair), cfg.residual_sigma * self.quality['fit_rmse'])
        if abs(residual) > bound:
            return result('residual_shock', residual=residual)
        change = self._predict(now + cfg.horizon_seconds, self.weights) - fitted
        return dict(result('active'), active=True, basis=basis, predicted_basis=fitted,
                    residual=residual, predicted_basis_change=change,
                    # A-as-reference is explicit: do not invent an absolute A forecast.
                    predicted_B_change=-change, samples=len(self.history))


def apply_cycle_quote(quote, book, position, tick, instrument_id, signal, settings, soft_limit,
                      *, apply_price_shift=True):
    """Keep original inventory targets; only B gets bounded cycle alpha.

    Reduce the adverse *inventory-increasing* side only, never enlarge a quote
    or suppress a reducing side. Passive clamps also apply during fallback.
    """
    if type(apply_price_shift) is not bool:
        raise ValueError('apply_price_shift must be boolean')
    if quote is None:
        return None
    out = dict(quote)
    shift = 0.0
    if instrument_id == 'PHILIPS_B' and signal.get('active'):
        prediction = signal['predicted_B_change']
        if not math.isfinite(prediction):
            raise ValueError('invalid cycle prediction')
        weight = signal.get('fit_weight', 1.0)
        if not math.isfinite(weight) or not 0 <= weight <= 1:
            raise ValueError('invalid cycle fit weight')
        cap = settings.max_quote_shift_ticks * tick
        shift = max(-cap, min(cap, settings.quote_gain * prediction))
        shift *= weight
        shift *= max(0.0, 1 - abs(position) / soft_limit)
    # Preserve the v1 risk signal and its thresholds independently of whether
    # the relative-value forecast is allowed to move absolute B quote prices.
    risk_shift = shift
    shift = risk_shift if apply_price_shift else 0.0
    out['center'] += shift
    out['fair_value'] += shift
    bid = min(out['bid_price'] + shift, book.asks[0].price - tick)
    ask = max(out['ask_price'] + shift, book.bids[0].price + tick)
    out['bid_price'] = round(math.floor(bid / tick + 1e-9) * tick, 10)
    out['ask_price'] = round(math.ceil(ask / tick - 1e-9) * tick, 10)
    if out['bid_price'] <= 0:
        out['buy_volume'] = 0
    if risk_shift <= -tick and position >= 0 and out['buy_volume'] > 0:
        out['buy_volume'] = max(1, math.floor(out['buy_volume'] * settings.adverse_size_fraction))
    if risk_shift >= tick and position <= 0 and out['sell_volume'] > 0:
        out['sell_volume'] = max(1, math.floor(out['sell_volume'] * settings.adverse_size_fraction))
    out.update(cycle_shift=shift, cycle_risk_shift=risk_shift, cycle=signal,
               cycle_price_shift_enabled=apply_price_shift)
    return out
