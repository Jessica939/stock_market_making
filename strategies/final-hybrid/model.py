"""Strictly causal harmonic estimate of the current A-B basis."""
from collections import deque
from dataclasses import dataclass
import math

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



@dataclass(frozen=True)
class BasisSettings:
    period_seconds: float = 180.0
    history_seconds: float = 720.0
    warmup_seconds: float = 180.0
    sample_seconds: float = 1.0
    refit_seconds: float = 5.0
    max_gap_seconds: float = 10.0
    prior_enabled: bool = False
    prior_peak_epoch_seconds: float = 165.95
    prior_center: float = 0.0
    prior_amplitude: float = 3.1
    prior_rmse: float = 0.9
    prior_fit_r2: float = 0.88

    def __post_init__(self):
        if type(self.prior_enabled) is not bool:
            raise ValueError("prior_enabled must be boolean")
        for name in self.__dataclass_fields__:
            if name in ("prior_enabled", "prior_center"):
                continue
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(name + " must be finite and positive")
        if (isinstance(self.prior_center, bool)
                or not isinstance(self.prior_center, (int, float))
                or not math.isfinite(self.prior_center)):
            raise ValueError("prior_center must be finite")
        if self.prior_fit_r2 > 1:
            raise ValueError("prior_fit_r2 must not exceed one")
        if not self.warmup_seconds <= self.history_seconds:
            raise ValueError("warmup_seconds must not exceed history_seconds")
        if self.sample_seconds > self.period_seconds / 20:
            raise ValueError("sample interval is too long for the harmonic model")


class CausalBasisModel:
    """Forecast first, then learn from the current frame.

    Calling ``observe`` on frame t never changes the parameters used for the
    signal returned for t. A fit performed after ingesting t becomes available
    only to a later frame.
    """

    def __init__(self, settings=None):
        self.settings = settings or BasisSettings()
        self.reset()

    def reset(self):
        self.rows = deque()
        self.origin = None
        self.weights = None
        self.last_sample = self.last_fit = self.last_observation = -math.inf
        self.quality = {}
        self.last_sample_stamps = None

    def _activate_prior(self):
        cfg = self.settings
        phase = 2 * math.pi * (cfg.prior_peak_epoch_seconds % cfg.period_seconds) / cfg.period_seconds
        self.weights = (cfg.prior_center,
                        cfg.prior_amplitude * math.sin(phase),
                        cfg.prior_amplitude * math.cos(phase))
        self.quality = dict(
            fit_rmse=cfg.prior_rmse,
            fit_r2=cfg.prior_fit_r2,
            fit_samples=0,
            quality_scope="historical_epoch_phase_prior",
        )

    def _x(self, t):
        angle = 2 * math.pi * ((t - self.origin) % self.settings.period_seconds) / self.settings.period_seconds
        return (1.0, math.sin(angle), math.cos(angle))

    def predict(self, t, weights=None):
        selected = self.weights if weights is None else weights
        if selected is None or self.origin is None:
            return None
        return sum(x * w for x, w in zip(self._x(t), selected))

    def snapshot(self):
        if self.weights is None:
            return None
        return dict(weights=tuple(self.weights), origin=self.origin,
                    period_seconds=self.settings.period_seconds)

    @staticmethod
    def predict_snapshot(snapshot, t):
        angle = 2 * math.pi * ((t - snapshot["origin"]) % snapshot["period_seconds"]) / snapshot["period_seconds"]
        w = snapshot["weights"]
        return w[0] + w[1] * math.sin(angle) + w[2] * math.cos(angle)

    def _fit(self):
        if len(self.rows) < 20 or self.rows[-1][0] - self.rows[0][0] < self.settings.warmup_seconds:
            return
        matrix = [[0.0] * 3 for _ in range(3)]
        rhs = [0.0] * 3
        for t, y in self.rows:
            x = self._x(t)
            for i in range(3):
                rhs[i] += x[i] * y
                for j in range(3):
                    matrix[i][j] += x[i] * x[j]
        weights = _solve(matrix, rhs)
        if weights is None:
            return
        fitted = [sum(x * w for x, w in zip(self._x(t), weights)) for t, _ in self.rows]
        values = [y for _, y in self.rows]
        mse = sum((y - yhat) ** 2 for y, yhat in zip(values, fitted)) / len(values)
        mean = sum(values) / len(values)
        variance = sum((y - mean) ** 2 for y in values) / len(values)
        self.weights = tuple(weights)
        self.quality = dict(
            fit_rmse=math.sqrt(mse),
            fit_r2=1 - mse / variance if variance > 1e-12 else -1.0,
            fit_samples=len(values),
            quality_scope="in_sample_fit_not_forecast_accuracy",
        )

    def observe(self, *, now, a_mid, b_mid, book_stamps):
        cfg = self.settings
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
               for v in (now, a_mid, b_mid, *book_stamps)):
            self.reset()
            return dict(active=False, reason="invalid_input")
        if now < self.last_observation:
            self.reset()
            return dict(active=False, reason="clock_reversal")
        if self.last_observation > -math.inf and now - self.last_observation > cfg.max_gap_seconds:
            self.reset()
        self.last_observation = now
        if self.origin is None:
            if cfg.prior_enabled:
                # Map the monotonic process clock onto the exchange epoch. A
                # restart therefore keeps the known global cycle phase.
                phase_stamp = sum(book_stamps) / len(book_stamps)
                self.origin = now - (phase_stamp % cfg.period_seconds)
                self._activate_prior()
            else:
                self.origin = now

        # Produce the decision from parameters fitted strictly before this row.
        predicted = self.predict(now)
        frozen = self.snapshot()
        signal = (dict(active=False, reason="warmup", samples=len(self.rows), **self.quality)
                  if predicted is None else
                  dict(active=True, reason="active", predicted_basis=predicted,
                       fair_B=a_mid - predicted, residual=(a_mid - b_mid) - predicted,
                       model=frozen, samples=len(self.rows), **self.quality))

        basis = a_mid - b_mid
        fresh = (self.last_sample_stamps is None
                 or all(new > old for new, old in zip(book_stamps, self.last_sample_stamps)))
        if fresh and now - self.last_sample >= cfg.sample_seconds:
            self.rows.append((now, basis))
            self.last_sample = now
            self.last_sample_stamps = tuple(book_stamps)
            while self.rows and now - self.rows[0][0] > cfg.history_seconds:
                self.rows.popleft()
            if now - self.last_fit >= cfg.refit_seconds:
                self._fit()
                self.last_fit = now
        return signal
