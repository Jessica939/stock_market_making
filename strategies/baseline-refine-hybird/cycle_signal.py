"""Causal A-minus-B cycle learning, validation and bounded quote overlay.

The initial period is a hypothesis. Live data updates coefficients and selects
period/window changes with a trailing validation block and hysteresis.
No exchange calls or external dependencies.
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
    adaptive: bool = True
    min_period_seconds: float = 120.0
    max_period_seconds: float = 240.0
    period_step_seconds: float = 5.0
    fast_history_seconds: float = 360.0
    period_min_cycles: float = 1.25
    switch_improvement: float = 0.2
    switch_confirmations: int = 3
    switch_cooldown_seconds: float = 30.0

    def __post_init__(self):
        if type(self.enabled) is not bool or type(self.adaptive) is not bool:
            raise ValueError('enabled and adaptive must be boolean')
        for name in self.__dataclass_fields__:
            if name in ('enabled', 'adaptive'):
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
        if (not self.min_period_seconds <= self.period_seconds <= self.max_period_seconds
                or self.horizon_seconds > self.min_period_seconds / 2
                or self.sample_seconds > self.min_period_seconds / 20
                or self.period_step_seconds > self.max_period_seconds-self.min_period_seconds
                or (self.max_period_seconds-self.min_period_seconds) / self.period_step_seconds > 100
                or self.period_min_cycles < 1
                or not self.min_fit_seconds <= self.fast_history_seconds <= self.history_seconds
                or not 0 < self.switch_improvement < 1
                or type(self.switch_confirmations) is not int):
            raise ValueError('invalid adaptation settings')


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
        self.initialized = False
        self.period_seconds = self.settings.period_seconds
        self.window_seconds = self.settings.history_seconds
        self.revision = 0
        self.pending_candidate = None
        self.pending_count = 0
        self.last_switch = -math.inf
        self.adaptation = dict(state='warming_up', revision=0)
        self.validation_weight = 1.0
        self.pending_forecasts = deque()
        self.forecast_errors = deque()
        self.forecast_quality = {}

    @property
    def needs_bootstrap(self):
        # A degraded live model must learn from live data, not reload old seeds.
        return not self.initialized

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

    def _x(self, t, period=None):
        period = self.period_seconds if period is None else period
        angle = 2 * math.pi * ((t - self.origin) % period) / period
        return [1.0, math.sin(angle), math.cos(angle)]

    def _predict(self, t, weights, period=None):
        return sum(x * w for x, w in zip(self._x(t, period), weights))

    def _fit(self, rows, period=None):
        matrix = [[0.0] * 3 for _ in range(3)]
        rhs = [0.0] * 3
        for t, y in rows:
            x = self._x(t, period)
            for i in range(3):
                rhs[i] += x[i] * y
                for j in range(3):
                    matrix[i][j] += x[i] * x[j]
        return _solve(matrix, rhs)

    def _validate(self, rows, period, window, tick):
        """Fit only the prefix; score a subsequent horizon-sized block.

        This is model-selection evidence, not an independent backtest. Period
        search needs at least 1.25 cycles in the training prefix by default.
        """
        cfg = self.settings
        cutoff = rows[-1][0] - cfg.horizon_seconds
        selected = [(t, y) for t, y in rows if t >= rows[-1][0] - window]
        train = [(t, y) for t, y in selected if t <= cutoff]
        holdout = [(t, y) for t, y in selected if t > cutoff]
        if (len(train) < 20 or len(holdout) < 5
                or train[-1][0] - train[0][0] < cfg.min_fit_seconds
                or holdout[-1][0] - holdout[0][0] < cfg.horizon_seconds * .8
                or (period != self.period_seconds
                    and train[-1][0] - train[0][0] < cfg.period_min_cycles * period)):
            return None
        weights = self._fit(train, period)
        if weights is None:
            return None
        amplitude = math.hypot(weights[1], weights[2])
        if not cfg.min_amplitude_ticks * tick <= amplitude <= cfg.max_amplitude_ticks * tick:
            return None
        mse = sum((y-self._predict(t, weights, period))**2 for t, y in holdout) / len(holdout)
        baseline_mse = sum((y-train[-1][1])**2 for _, y in holdout) / len(holdout)
        return dict(period=period, window=window, mse=mse, baseline_mse=baseline_mse,
                    samples=len(holdout))

    def _adapt(self, rows, now, tick):
        cfg = self.settings
        current = self._validate(rows, self.period_seconds, self.window_seconds, tick)
        self.validation_weight = 1.0
        self.adaptation = dict(state='disabled' if not cfg.adaptive else 'insufficient_validation',
                               revision=self.revision)
        candidates = [current] if current else []
        if cfg.adaptive:
            count = int((cfg.max_period_seconds-cfg.min_period_seconds) / cfg.period_step_seconds)
            periods = sorted({self.period_seconds, cfg.max_period_seconds,
                              *(cfg.min_period_seconds + i*cfg.period_step_seconds for i in range(count+1))})
            for window in sorted({cfg.fast_history_seconds, cfg.history_seconds}):
                for period in periods:
                    if (period, window) == (self.period_seconds, self.window_seconds):
                        continue
                    candidate = self._validate(rows, period, window, tick)
                    if candidate:
                        candidates.append(candidate)
        best = min(candidates, key=lambda c: c['mse']) if candidates else None
        improvement = (current['mse'] - best['mse']) if current and best else None
        better = bool(cfg.adaptive and best and
                      (best['period'], best['window']) != (self.period_seconds, self.window_seconds)
                      and (current is None or improvement > max(tick*tick, current['mse']*cfg.switch_improvement)))
        state = 'stable' if current else 'insufficient_validation'
        if better:
            key = (best['period'], best['window'])
            if (self.pending_candidate and key[1] == self.pending_candidate[1]
                    and abs(key[0]-self.pending_candidate[0]) <= cfg.period_step_seconds):
                self.pending_count += 1
            else:
                self.pending_count = 1
            self.pending_candidate = key
            state = 'confirming_change'
            if now - self.last_switch < cfg.switch_cooldown_seconds:
                state = 'switch_cooldown'
            elif self.pending_count >= cfg.switch_confirmations:
                previous = dict(period_seconds=self.period_seconds, window_seconds=self.window_seconds)
                self.period_seconds, self.window_seconds = key
                self.revision += 1
                self.last_switch = now
                self.pending_candidate, self.pending_count = None, 0
                # Old issued forecasts describe the previous regime/model.
                self.pending_forecasts.clear()
                self.forecast_errors.clear()
                self.forecast_quality = {}
                self.adaptation.update(previous=previous, changed_at=now,
                                       reason='lower_time_ordered_validation_error')
                current = best
                state = 'changed'
        else:
            self.pending_candidate, self.pending_count = None, 0
        self.adaptation.update(state=state if cfg.adaptive else 'disabled', revision=self.revision,
                               confirmations=self.pending_count, candidate=best)
        if current:
            # A constant-spread forecast is the causal comparison baseline.
            self.validation_weight = max(0.0, min(1.0, 1-current['mse']/max(current['baseline_mse'], tick*tick)))
            self.adaptation.update(validation_rmse=math.sqrt(current['mse']),
                                   validation_baseline_rmse=math.sqrt(current['baseline_mse']),
                                   validation_weight=self.validation_weight)

    def _score_forecasts(self, now, basis, tick):
        """Verify previously issued forecasts before using this new sample."""
        cfg = self.settings
        while self.pending_forecasts and self.pending_forecasts[0][0] <= now:
            due, prediction, baseline = self.pending_forecasts.popleft()
            if now - due <= 2*cfg.sample_seconds:
                self.forecast_errors.append((now, (basis-prediction)**2, (basis-baseline)**2))
        while self.forecast_errors and now-self.forecast_errors[0][0] > cfg.ramp_seconds:
            self.forecast_errors.popleft()
        count = len(self.forecast_errors)
        self.forecast_quality = dict(forecast_samples=count, forecast_weight=1.0,
                                    forecast_scope='issued_A_minus_B_forecasts_not_absolute_B')
        if count:
            mse = sum(row[1] for row in self.forecast_errors)/count
            baseline_mse = sum(row[2] for row in self.forecast_errors)/count
            skill = 1-mse/max(baseline_mse, tick*tick)
            ready = count >= 10 and now-self.forecast_errors[0][0] >= cfg.min_fit_seconds
            self.forecast_quality.update(forecast_rmse=math.sqrt(mse),
                forecast_baseline_rmse=math.sqrt(baseline_mse), forecast_skill=skill,
                forecast_ready=ready, forecast_weight=max(0.0, min(1.0, skill)) if ready else 1.0)

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
        self._adapt(rows, now, tick)
        rows = [(t, y) for t, y in rows if t >= rows[-1][0] - self.window_seconds]
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
                            fitted_offset=weights[0],
                            fitted_phase_radians=math.atan2(weights[2], weights[1]),
                            phase_origin=self.origin,
                            quality_scope='in_sample_fit_not_forecast_accuracy')
        if not cfg.min_amplitude_ticks * tick <= amplitude <= cfg.max_amplitude_ticks * tick:
            self.reason = 'amplitude_out_of_bounds'
            return
        self.weights = weights
        self.initialized = True
        self.reason = 'active'

    def observe(self, books, ticks, now, wall):
        cfg = self.settings
        def result(reason, **extra):
            out = dict(active=False, reason=reason, period_seconds=self.period_seconds,
                        window_seconds=self.window_seconds, basis_definition='mid_A_minus_mid_B',
                        horizon_seconds=cfg.horizon_seconds, bootstrap=self.bootstrap,
                        adaptation=dict(self.adaptation), **self.quality,
                        **self.forecast_quality, **extra)
            out['in_sample_fit_weight'] = out.get('fit_weight', 0.0)
            out['fit_weight'] = (out['in_sample_fit_weight'] * self.validation_weight
                                 * out.get('forecast_weight', 1.0))
            return out
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
        if any(not usable_book(books.get(s), ticks.get(s, 0), wall, cfg, check_spread=False) for s in symbols):
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
        sampled = False
        if new and now - self.last_sample >= cfg.sample_seconds:
            sampled = True
            self._score_forecasts(now, basis, max(tick_pair))
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
        future = self._predict(now + cfg.horizon_seconds, self.weights)
        if sampled:
            self.pending_forecasts.append((now+cfg.horizon_seconds, future, basis))
        diagnostics = dict(basis=basis, predicted_basis=fitted, residual=residual,
                           predicted_basis_change=future-fitted,
                           predicted_B_change=fitted-future, samples=len(self.history))
        if any(not usable_book(books[s], ticks[s], wall, cfg) for s in symbols):
            return result('wide_pair_spread', **diagnostics)
        bound = max(3 * max(tick_pair), cfg.residual_sigma * self.quality['fit_rmse'])
        if abs(residual) > bound:
            return result('residual_shock', **diagnostics)
        if result('active')['fit_weight'] <= 0:
            return result('forecast_not_better_than_constant', **diagnostics)
        # A-as-reference is explicit: do not invent an absolute A forecast.
        return dict(result('active', **diagnostics), active=True)


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
