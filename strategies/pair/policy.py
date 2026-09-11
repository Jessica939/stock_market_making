"""Cost-aware cycle or convergence policy; no exchange side effects.

Targets are desired *actual* inventory, not orders or assumed fills. The runner
must execute two IOC legs, reconcile their fills, and handle unpaired exposure.
"""
from collections import deque
from math import isfinite, sin, cos, pi
from statistics import median
from datetime import datetime, timezone
from types import SimpleNamespace

from stock_market_making.strategies.baseline.cycle_signal import CycleSettings, CycleSignal


class Policy:
    """Trade a temporary A-minus-B dislocation with one equal-lot pair at a time."""

    def __init__(self, config=None):
        c = dict(config or {})
        self.symbols = tuple(c.get("symbols", ("PHILIPS_A", "PHILIPS_B")))
        if len(self.symbols) != 2 or len(set(self.symbols)) != 2:
            raise ValueError("Exactly two different symbols are required")
        self.c = {
            "lot_size": 20, "warmup_seconds": 45.0, "min_samples": 60,
            "history_seconds": 90.0, "sample_interval": 0.5,
            "entry_z": 2.5, "max_entry_z": 8.0, "exit_z": 0.5,
            "max_hold_seconds": 45.0, "cooldown_seconds": 8.0,
            "intent_timeout_seconds": 2.0, "session_seconds": 1800.0,
            "entry_cutoff_seconds": 120.0, "liquidation_buffer_seconds": 60.0,
            "fee_per_lot": 0.0, "slippage_ticks": 1.0,
            "min_net_edge_ticks": 2.0, "max_spread_ticks": 8.0,
            "max_mad_ticks": 8.0, "max_drift_z": 1.5,
            "max_trend_efficiency": 0.65, "min_crossings": 2,
            "stop_sigma": 3.0, "stop_ticks": 6.0,
            "max_gap_seconds": 3.0, "liquidity_fraction": 0.35,
            "relation_mode": "empirical", "parity_confirmed": False,
        }
        self.c.update(c)
        for key, ceiling in (('cycle_path_grace_seconds', 15), ('cycle_extension_seconds', 45)):
            value = self.c.get(key, 0)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or not 0 <= value <= ceiling:
                raise ValueError(key + ' outside allowed range')
        if self.c["relation_mode"] not in ("empirical", "parity", "cycle"):
            raise ValueError("relation_mode must be empirical, parity or cycle")
        if self.c["relation_mode"] == "parity":
            if self.c["parity_confirmed"] is not True or "parity_basis" not in c:
                raise ValueError("Parity requires parity_confirmed=true and explicit parity_basis")
            if not isfinite(float(c["parity_basis"])):
                raise ValueError("parity_basis must be finite")
        integer_keys = ("lot_size", "min_samples", "min_crossings")
        for key in integer_keys:
            value = self.c[key]
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(key + " must be an integer")
            self.c[key] = int(value)
        if not 1 <= self.c["lot_size"] <= 100 or self.c["min_samples"] < 6:
            raise ValueError("lot_size must be 1..100; min_samples must be >=6")
        positive = ("history_seconds", "sample_interval", "entry_z", "max_entry_z",
                    "max_hold_seconds", "intent_timeout_seconds", "session_seconds",
                    "max_spread_ticks", "max_mad_ticks", "max_drift_z",
                    "stop_sigma", "stop_ticks", "max_gap_seconds")
        nonnegative = ("warmup_seconds", "exit_z", "cooldown_seconds", "fee_per_lot",
                       "slippage_ticks", "min_net_edge_ticks", "entry_cutoff_seconds",
                       "liquidation_buffer_seconds", "min_crossings")
        for key in positive + nonnegative:
            if isinstance(self.c[key], bool):
                raise ValueError(key + " has an invalid value")
            value = float(self.c[key])
            if not isfinite(value) or value < 0 or (key in positive and value == 0):
                raise ValueError(key + " has an invalid value")
            if key not in integer_keys:
                self.c[key] = value
        if isinstance(self.c["liquidity_fraction"], bool) or not 0 < float(self.c["liquidity_fraction"]) <= 1:
            raise ValueError("liquidity_fraction must be in (0,1]")
        if isinstance(self.c["max_trend_efficiency"], bool) or not 0 <= float(self.c["max_trend_efficiency"]) <= 1:
            raise ValueError("max_trend_efficiency must be in [0,1]")
        self.c["liquidity_fraction"] = float(self.c["liquidity_fraction"])
        self.c["max_trend_efficiency"] = float(self.c["max_trend_efficiency"])
        if not self.c["exit_z"] < self.c["entry_z"] <= self.c["max_entry_z"]:
            raise ValueError("Require exit_z < entry_z <= max_entry_z")
        if self.c["history_seconds"] < self.c["warmup_seconds"]:
            raise ValueError("history_seconds must cover warmup_seconds")
        if self.c["history_seconds"] < (self.c["min_samples"] - 1) * self.c["sample_interval"]:
            raise ValueError("history window cannot contain min_samples")
        if not 0 <= self.c["liquidation_buffer_seconds"] <= self.c["entry_cutoff_seconds"] < self.c["session_seconds"]:
            raise ValueError("Require liquidation buffer <= entry cutoff < session duration")
        self.history = deque()
        self.active = None
        self.closing_reason = None
        self.cooldown_until = float("-inf")
        self.last_now = None
        self.last_elapsed = None
        self.last_stamps = {}
        self.last_fresh = {}
        self.cycle_model = None
        if self.c['relation_mode'] == 'cycle':
            if self.c.get('cycle_extension_seconds', 0) and c.get('cycle_horizon_seconds', 45) + self.c['cycle_extension_seconds'] > self.c['max_hold_seconds']:
                raise ValueError('cycle extension must fit inside max_hold_seconds')
            self.c.setdefault('cycle_error_buffer_sigma', 1.5)
            self.c.setdefault('cycle_exit_edge_ticks', 1.0)
            for key in ('cycle_error_buffer_sigma', 'cycle_exit_edge_ticks'):
                value = self.c[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value <= 0:
                    raise ValueError(key + ' must be finite and positive')
            cfg = CycleSettings(
                period_seconds=c.get('cycle_period_seconds', 180.0),
                min_fit_seconds=c.get('cycle_min_fit_seconds', 30.0),
                ramp_seconds=c.get('cycle_ramp_seconds', 180.0),
                history_seconds=c.get('cycle_history_seconds', 720.0),
                horizon_seconds=c.get('cycle_horizon_seconds', 45.0),
                max_gap_seconds=c.get('cycle_max_gap_seconds', 10.0),
                max_age_seconds=c.get('max_book_age_seconds', 2.0),
                max_pair_gap_seconds=c.get('max_pair_time_gap_seconds', .75),
                max_spread_ticks=self.c['max_spread_ticks'],
                prior_enabled=c.get('cycle_prior_enabled', False),
                prior_peak_epoch_seconds=c.get('cycle_prior_peak_epoch_seconds', 165.95),
                prior_center=c.get('cycle_prior_center', 0.0),
                prior_amplitude=c.get('cycle_prior_amplitude', 3.1),
                prior_rmse=c.get('cycle_prior_rmse', 0.9),
                prior_fit_weight=c.get('cycle_prior_fit_weight', 0.65),
                prior_fit_r2=c.get('cycle_prior_fit_r2', 0.88),
                prior_strength=c.get('cycle_prior_strength', 20.0),
                prior_phase_lock_seconds=c.get('cycle_prior_phase_lock_seconds', 180.0),
                prior_max_phase_shift_seconds=c.get('cycle_prior_max_phase_shift_seconds', 5.0),
            )
            if cfg.horizon_seconds > self.c['max_hold_seconds']:
                raise ValueError('cycle_horizon_seconds must not exceed max_hold_seconds')
            self.cycle_model = CycleSignal(cfg)

    def _clear_learning(self):
        self.history.clear()
        if self.cycle_model is not None:
            self.cycle_model.reset()

    def _cycle_signal(self, frame, now):
        """Adapt already-cleaned books; no new connection or stream consumption.

        Real exchange timestamps and explicit ages are required in cycle mode.
        Canonical adapter names preserve the configured first-minus-second order.
        """
        books, ticks, walls = {}, {}, []
        try:
            for source_id, canonical in zip(self.symbols, ('PHILIPS_A', 'PHILIPS_B')):
                source = frame['books'][source_id]
                stamp, age = source['timestamp'], source['age_seconds']
                if (isinstance(stamp, bool) or isinstance(age, bool)
                        or not isfinite(stamp) or not isfinite(age)):
                    raise ValueError('invalid timestamp metadata')
                books[canonical] = SimpleNamespace(
                    timestamp=datetime.fromtimestamp(stamp, timezone.utc),
                    bids=[SimpleNamespace(price=p, volume=v) for p, v in source['bids']],
                    asks=[SimpleNamespace(price=p, volume=v) for p, v in source['asks']],
                )
                ticks[canonical] = source['tick']
                walls.append(stamp + age)
            return self.cycle_model.observe(books, ticks, now, max(walls))
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            self.cycle_model.reset()
            return dict(active=False, reason='invalid_cycle_metadata')

    @staticmethod
    def _frozen_cycle(held, now):
        angle = 2 * pi * ((now - held['cycle_origin']) % held['cycle_period']) / held['cycle_period']
        center, sine, cosine = held['cycle_weights']
        return center + sine * sin(angle) + cosine * cos(angle)

    def _decide_cycle(self, frame, actual, elapsed, now, ba, bb, half_spreads,
                      historical_half_spreads, diag):
        a, b = self.symbols
        pa, pb = actual[a], actual[b]
        flat = pa == pb == 0
        tick, basis = max(ba['tick'], bb['tick']), ba['mid'] - bb['mid']
        signal = self._cycle_signal(frame, now)
        diag['cycle'] = signal
        if self.active and flat and self.active['filled']:
            self.active = None
            self.cooldown_until = now + self.c['cooldown_seconds']
        if not flat:
            if self.c.get('pair_independent_holding', False):
                return self.monitor_cycle_position(frame, actual)
            held = self.active
            if held is None:
                return self._exit('untracked_inventory', diag)
            direction = 1 if pa > 0 else -1
            if direction != held['direction'] or abs(pa) > held['size']:
                return self._exit('unexpected_inventory_change', diag)
            held['filled'] = True
            held['size'] = min(held['size'], abs(pa))
            if now >= held['exit_at'] or now - held['opened_at'] >= self.c['max_hold_seconds']:
                return self._exit('cycle_horizon_exit', diag)
            if not signal['active']:
                return self._exit('cycle_model_unavailable', diag)
            stop = max(self.c['stop_ticks'] * held['tick'], self.c['stop_sigma'] * held['sigma'])
            expected_move = self._frozen_cycle(held, now) - self._frozen_cycle(held, held['opened_at'])
            path_error = basis - held['entry_basis'] - expected_move
            liquidation_spread = ba['bid'] - bb['ask'] if direction == 1 else ba['ask'] - bb['bid']
            pnl = direction * (liquidation_spread - held['entry_spread']) - 4 * self.c['fee_per_lot']
            remaining = (self.cycle_model._predict(held['exit_at'], self.cycle_model.weights)
                         - signal['predicted_basis'])
            diag.update(frozen_residual_sigma=held['sigma'], frozen_path_error=path_error,
                        planned_exit_at=held['exit_at'], estimated_liquidation_pnl_per_pair=pnl,
                        predicted_remaining_basis_change=remaining)
            if direction * (basis - held['entry_basis']) <= -stop:
                return self._exit('cycle_adverse_move_stop', diag)
            if abs(path_error) > stop:
                return self._exit('cycle_path_break', diag)
            if pnl <= -(stop + held['initial_roundtrip_cost']):
                return self._exit('liquidation_cost_stop', diag)
            if direction * remaining <= self.c['cycle_exit_edge_ticks'] * tick:
                return self._exit('cycle_forecast_exit', diag)
            return self._result('hold_cycle_pair', diag, actual)
        if self.active:
            if now - self.active['opened_at'] < self.c['intent_timeout_seconds']:
                return self._result('await_entry_reconciliation', diag)
            self.active = None
            self.cooldown_until = now + self.c['cooldown_seconds']
        if now < self.cooldown_until:
            return self._result('cooldown', diag)
        cutoff = self.c['session_seconds'] - self.c['entry_cutoff_seconds']
        horizon = self.cycle_model.settings.horizon_seconds
        if elapsed + horizon >= cutoff:
            return self._result('entry_cutoff', diag)
        if not signal['active']:
            return self._result('cycle_' + signal['reason'], diag)
        change = signal['predicted_basis_change']
        weight = signal['fit_weight']
        # Round trip = two entry half-spreads + conservative exit half-spreads
        # + four fees + per-instrument entry/exit slippage budgets.
        friction = 4 * self.c['fee_per_lot'] + 2 * self.c['slippage_ticks'] * (ba['tick'] + bb['tick'])
        cost = half_spreads + max(half_spreads, historical_half_spreads) + friction
        error_buffer = max(tick, self.c['cycle_error_buffer_sigma'] * signal['fit_rmse'])
        net_edge = abs(change) * weight - cost - error_buffer
        diag.update(predicted_basis_change=change, weighted_gross_edge=abs(change) * weight,
                    estimated_roundtrip_cost=cost, error_buffer=error_buffer, estimated_net_edge=net_edge)
        if net_edge < self.c['min_net_edge_ticks'] * tick:
            return self._result('cycle_cost_filter', diag)
        direction = 1 if change > 0 else -1
        liquidity = min(ba['ask_volume'], bb['bid_volume']) if direction == 1 else min(ba['bid_volume'], bb['ask_volume'])
        size = min(self.c['lot_size'], int(liquidity * self.c['liquidity_fraction']),
                   max(1, int(self.c['lot_size'] * weight)))
        if size < 1:
            return self._result('insufficient_top_liquidity', diag)
        entry_spread = ba['ask'] - bb['bid'] if direction == 1 else ba['bid'] - bb['ask']
        self.active = dict(direction=direction, size=size, tick=tick,
                           sigma=max(tick, signal['fit_rmse']), entry_basis=basis,
                           entry_spread=entry_spread, initial_roundtrip_cost=cost,
                           opened_at=now, exit_at=now + horizon, filled=False,
                           cycle_weights=tuple(self.cycle_model.weights),
                           cycle_origin=self.cycle_model.origin,
                           cycle_period=self.cycle_model.settings.period_seconds)
        diag.update(pair_size=size, planned_exit_at=now + horizon)
        return self._result('cycle_long_A_short_B' if direction == 1 else 'cycle_short_A_long_B',
                            diag, {a: direction * size, b: -direction * size})

    def monitor_cycle_position(self, frame, actual):
        """Use the entry-frozen cycle for held risk; never refit, enter or add lots."""
        held, now = self.active, frame['now']
        a, b = self.symbols
        diag = {'mode': 'frozen_cycle_holding'}
        if self.closing_reason:
            return self._result(self.closing_reason, diag)
        if held is None or not actual[a] or actual[a] != -actual[b]:
            return self._exit('unmatched_or_untracked_inventory', diag)
        direction = 1 if actual[a] > 0 else -1
        if direction != held['direction'] or abs(actual[a]) > held['size']:
            return self._exit('unexpected_inventory_change', diag)
        held['filled'] = True
        held['size'] = min(held['size'], abs(actual[a]))
        hard_deadline = held['opened_at'] + self.c['max_hold_seconds']
        extension = self.c.get('cycle_extension_seconds', 0)
        if now >= hard_deadline or (now >= held['exit_at'] and not extension):
            return self._exit('cycle_horizon_exit', diag)
        ba, bb = (self._read_book(frame['books'][s]) for s in self.symbols)
        basis = ba['mid'] - bb['mid']
        expected = self._frozen_cycle(held, now)-self._frozen_cycle(held, held['opened_at'])
        path_error = basis-held['entry_basis']-expected
        stop = max(self.c['stop_ticks']*held['tick'], self.c['stop_sigma']*held['sigma'])
        remaining = self._frozen_cycle(held, held['exit_at'])-self._frozen_cycle(held, now)
        spread = ba['bid']-bb['ask'] if direction == 1 else ba['ask']-bb['bid']
        pnl = direction*(spread-held['entry_spread'])-4*self.c['fee_per_lot']
        diag.update(basis=basis, held_seconds=now-held['opened_at'], frozen_path_error=path_error,
                    stop_width=stop, predicted_remaining_basis_change=remaining,
                    estimated_liquidation_pnl_per_pair=pnl, planned_exit_at=held['exit_at'])
        if direction*(basis-held['entry_basis']) <= -stop:
            return self._exit('cycle_adverse_move_stop', diag)
        if pnl <= -(stop+held['initial_roundtrip_cost']):
            return self._exit('liquidation_cost_stop', diag)
        # Only adverse deviation is a lag. A favorable overshoot is not a broken path.
        lagging = direction*path_error < -stop
        if lagging:
            held.setdefault('path_lag_started_at', now)
            lag_seconds = now-held['path_lag_started_at']
            diag['path_lag_seconds'] = lag_seconds
            if lag_seconds >= self.c.get('cycle_path_grace_seconds', 0):
                return self._exit('cycle_path_lag_timeout', diag)
        else:
            held.pop('path_lag_started_at', None)
        if now >= held['exit_at']:
            original_exit = held.setdefault('original_exit_at', held['exit_at'])
            candidate = min(original_exit+extension, hard_deadline)
            extra = direction*(self._frozen_cycle(held, candidate)-self._frozen_cycle(held, now))
            # One fixed extension; never roll the deadline or extend a lagging model.
            if held.get('extended') or lagging or candidate <= now or extra <= self.c['cycle_exit_edge_ticks']*held['tick']:
                return self._exit('cycle_horizon_exit', diag)
            held['exit_at'], held['extended'] = candidate, True
            remaining = self._frozen_cycle(held, candidate)-self._frozen_cycle(held, now)
            diag.update(extension_granted=True, planned_exit_at=candidate,
                        predicted_remaining_basis_change=remaining)
        if direction*remaining <= self.c['cycle_exit_edge_ticks']*held['tick']:
            return self._exit('cycle_forecast_exit', diag)
        return self._result('observe_cycle_path_lag' if lagging else 'hold_frozen_cycle_pair', diag, actual)

    def _result(self, reason, diagnostics=None, targets=None):
        return {"targets": targets or dict.fromkeys(self.symbols, 0),
                "reason": reason, "diagnostics": diagnostics or {}}

    def _exit(self, reason, diagnostics=None):
        self.closing_reason = reason
        return self._result(reason, diagnostics)

    @staticmethod
    def _read_book(book):
        if not isinstance(book, dict):
            raise ValueError("missing book")
        if book.get("signal_usable", True) is not True:
            raise ValueError("book is usable for execution only")
        if any(isinstance(book[k], bool) for k in ("bid", "ask", "tick")):
            raise ValueError("invalid numeric book field")
        bid, ask, tick = (float(book[k]) for k in ("bid", "ask", "tick"))
        if not all(isfinite(x) and x > 0 for x in (bid, ask, tick)) or bid >= ask:
            raise ValueError("invalid book")
        # Recompute midpoint; an upstream fair-value estimate cannot contaminate it.
        volumes = []
        for key, price in (("bids", bid), ("asks", ask)):
            levels = book.get(key, [])
            if not levels or abs(float(levels[0][0]) - price) > tick * 1e-6:
                raise ValueError("missing/inconsistent best level")
            value = levels[0][1]
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("invalid displayed size")
            volumes.append(value)
        return {"bid": bid, "ask": ask, "tick": tick, "mid": (bid + ask) / 2,
                "bid_volume": volumes[0], "ask_volume": volumes[1]}

    def _stats(self, tick):
        values = [row[1] for row in self.history]
        center = median(values)
        sigma = max(tick, 1.4826 * median(abs(x - center) for x in values))
        n = max(2, len(values) // 3)
        drift = median(values[-n:]) - median(values[:n])
        movement = sum(abs(b - a) for a, b in zip(values, values[1:]))
        efficiency = abs(values[-1] - values[0]) / movement if movement else 0.0
        last_sign, crossings = 0, 0
        for value in values:
            sign = 1 if value - center > tick * 0.25 else -1 if value - center < -tick * 0.25 else 0
            if sign:
                if last_sign and sign != last_sign:
                    crossings += 1
                last_sign = sign
        return center, sigma, drift, efficiency, crossings

    def decide(self, frame, positions, elapsed):
        """Consume one synchronized frame and return absolute inventory targets.

        Statistics use only earlier accepted frames. A pending decision freezes
        the anchor, but a position is considered filled only on actual inventory.
        """
        try:
            if isinstance(frame["now"], bool) or isinstance(elapsed, bool):
                raise ValueError("invalid time")
            now, elapsed = float(frame["now"]), float(elapsed)
            if not isfinite(now) or not isfinite(elapsed) or elapsed < 0:
                raise ValueError("invalid time")
            actual = {}
            for symbol in self.symbols:
                value = positions[symbol]
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError("invalid position")
                actual[symbol] = value
        except (KeyError, TypeError, ValueError, OverflowError):
            self._clear_learning()
            return self._exit("invalid_state")
        a, b = self.symbols
        pa, pb = actual[a], actual[b]
        flat = pa == pb == 0
        if self.closing_reason:
            if not flat:
                return self._result(self.closing_reason)
            self.active = None
            self.closing_reason = None
            self.cooldown_until = now + self.c["cooldown_seconds"]
        if ((self.last_now is not None and now < self.last_now)
                or (self.last_elapsed is not None and elapsed < self.last_elapsed)):
            self._clear_learning()
            return self._exit("clock_reversed")
        if self.last_now is not None and now - self.last_now > self.c["max_gap_seconds"]:
            self._clear_learning()
            self.last_now = now
            if not flat:
                return self._exit("data_gap_exit")
            self.active = None
            self.cooldown_until = now + self.c["cooldown_seconds"]
        self.last_now = now
        self.last_elapsed = elapsed
        if elapsed >= self.c["session_seconds"] - self.c["liquidation_buffer_seconds"]:
            return self._exit("session_liquidation")
        if (pa or pb) and (pa != -pb or abs(pa) > self.c["lot_size"]):
            return self._exit("unmatched_or_excess_inventory")
        try:
            ba, bb = (self._read_book(frame["books"][symbol]) for symbol in self.symbols)
            # Live cleaned frames carry exchange timestamps. Bare unit-test
            # frames without timestamps remain supported, but mixed metadata
            # and stale/reversed exchange time cannot masquerade as fresh data.
            stamps = {}
            for symbol in self.symbols:
                source = frame["books"][symbol]
                if "timestamp" in source:
                    if isinstance(source["timestamp"], bool):
                        raise ValueError("invalid exchange time")
                    stamps[symbol] = float(source["timestamp"])
                    if (not isfinite(stamps[symbol])
                            or stamps[symbol] < self.last_stamps.get(symbol, float("-inf"))):
                        raise ValueError("invalid or reversed exchange time")
                if "age_seconds" in source:
                    age = float(source["age_seconds"])
                    if not isfinite(age) or not -1 <= age <= self.c["max_gap_seconds"]:
                        raise ValueError("stale exchange book")
            if stamps:
                if len(stamps) != 2 or abs(stamps[a] - stamps[b]) > self.c["max_gap_seconds"]:
                    raise ValueError("unsynchronized exchange books")
                for symbol, stamp in stamps.items():
                    if stamp > self.last_stamps.get(symbol, float("-inf")):
                        self.last_fresh[symbol] = now
                    if now - self.last_fresh[symbol] > self.c["max_gap_seconds"]:
                        raise ValueError("stale repeated exchange book")
                self.last_stamps.update(stamps)
        except (KeyError, IndexError, TypeError, ValueError, OverflowError):
            self.history.clear()
            # A single rejected/wide snapshot blocks trading and exits exposure,
            # but need not discard the phase. Long gaps still reset the model.
            if (self.cycle_model is not None and now - self.cycle_model.last_sample
                    > self.cycle_model.settings.max_gap_seconds):
                self.cycle_model.reset()
            if flat:
                self.active = None
            return self._exit("invalid_book") if not flat else self._result("invalid_book")
        tick = max(ba["tick"], bb["tick"])
        basis = ba["mid"] - bb["mid"]
        half_spreads = ((ba["ask"] - ba["bid"]) + (bb["ask"] - bb["bid"])) / 2
        while self.history and now - self.history[0][0] > self.c["history_seconds"]:
            self.history.popleft()
        ready = (len(self.history) >= self.c["min_samples"] and
                 now - self.history[0][0] >= self.c["warmup_seconds"])
        stats = self._stats(tick) if ready else None
        historical_half_spreads = median(row[2] for row in self.history) if self.history else half_spreads
        # Sampling is time based; repeated calls do not multiply one observation.
        if not self.history or now - self.history[-1][0] >= self.c["sample_interval"]:
            self.history.append((now, basis, half_spreads))
        diag = {"basis": basis, "sample_count": len(self.history), "mode": self.c["relation_mode"]}
        if self.cycle_model is not None:
            return self._decide_cycle(frame, actual, elapsed, now, ba, bb, half_spreads,
                                      historical_half_spreads, diag)
        if self.active and flat and self.active["filled"]:
            self.active = None
            self.cooldown_until = now + self.c["cooldown_seconds"]
        if not flat:
            if self.active is None:
                return self._exit("untracked_inventory", diag)
            held = self.active
            direction = 1 if pa > 0 else -1
            if direction != held["direction"] or abs(pa) > held["size"]:
                return self._exit("unexpected_inventory_change", diag)
            held["filled"] = True
            # Partial fill or external reduction never triggers replenishment.
            held["size"] = min(held["size"], abs(pa))
            exit_spread = ba["bid"] - bb["ask"] if direction == 1 else ba["ask"] - bb["bid"]
            pnl_estimate = direction * (exit_spread - held["entry_spread"]) - 4 * self.c["fee_per_lot"]
            adverse = direction * (basis - held["entry_basis"])
            stop_width = max(self.c["stop_ticks"] * held["tick"], self.c["stop_sigma"] * held["sigma"])
            diag.update({"frozen_anchor": held["anchor"], "frozen_sigma": held["sigma"],
                         "held_seconds": now - held["opened_at"],
                         "estimated_liquidation_pnl_per_pair": pnl_estimate})
            if adverse <= -stop_width:
                return self._exit("frozen_basis_stop", diag)
            if pnl_estimate <= -(stop_width + held["initial_roundtrip_cost"]):
                return self._exit("liquidation_cost_stop", diag)
            if now - held["opened_at"] >= self.c["max_hold_seconds"]:
                return self._exit("holding_timeout", diag)
            if stats and self.c["relation_mode"] == "empirical":
                rolling_center = stats[0]
                if abs(rolling_center - held["anchor"]) > max(4 * held["sigma"], 8 * held["tick"]):
                    return self._exit("relationship_break", diag)
            if direction * (basis - held["anchor"]) >= -self.c["exit_z"] * held["sigma"]:
                return self._exit("convergence_exit", diag)
            return self._result("hold_pair", diag, {a: pa, b: pb})
        if self.active:
            if now - self.active["opened_at"] < self.c["intent_timeout_seconds"]:
                # Do not repeat an entry request while its fill status is unresolved.
                return self._result("await_entry_reconciliation", diag)
            self.active = None
            self.cooldown_until = now + self.c["cooldown_seconds"]
        if now < self.cooldown_until:
            return self._result("cooldown", diag)
        if elapsed >= self.c["session_seconds"] - self.c["entry_cutoff_seconds"]:
            return self._result("entry_cutoff", diag)
        if not ready:
            return self._result("warmup", diag)
        center, sigma, drift, efficiency, crossings = stats
        if self.c["relation_mode"] == "parity":
            center = float(self.c["parity_basis"])
        deviation = basis - center
        z = deviation / sigma
        diag.update({"anchor": center, "sigma": sigma, "z": z,
                     "drift": drift, "trend_efficiency": efficiency, "crossings": crossings})
        if any((book["ask"] - book["bid"]) / book["tick"] > self.c["max_spread_ticks"] + 1e-8
               for book in (ba, bb)):
            return self._result("wide_market", diag)
        if sigma / tick > self.c["max_mad_ticks"]:
            return self._result("unstable_basis", diag)
        if self.c["relation_mode"] == "empirical":
            if abs(drift) > self.c["max_drift_z"] * sigma or efficiency > self.c["max_trend_efficiency"]:
                return self._result("basis_trend_filter", diag)
            if crossings < self.c["min_crossings"]:
                return self._result("unconfirmed_reversion", diag)
        if abs(z) < self.c["entry_z"]:
            return self._result("no_dislocation", diag)
        if abs(z) > self.c["max_entry_z"]:
            return self._result("extreme_dislocation", diag)
        future_half_spreads = max(half_spreads, historical_half_spreads)
        friction = 4 * self.c["fee_per_lot"] + 2 * self.c["slippage_ticks"] * (ba["tick"] + bb["tick"])
        cost = half_spreads + future_half_spreads + friction
        net_edge = abs(deviation) - cost
        diag.update({"estimated_roundtrip_cost": cost, "estimated_net_edge": net_edge})
        if net_edge < self.c["min_net_edge_ticks"] * tick:
            return self._result("cost_filter", diag)
        direction = -1 if deviation > 0 else 1
        liquidity = min(ba["ask_volume"], bb["bid_volume"]) if direction == 1 else min(ba["bid_volume"], bb["ask_volume"])
        size = min(self.c["lot_size"], int(liquidity * self.c["liquidity_fraction"]))
        if size < 1:
            return self._result("insufficient_top_liquidity", diag)
        entry_spread = ba["ask"] - bb["bid"] if direction == 1 else ba["bid"] - bb["ask"]
        self.active = {"direction": direction, "size": size, "anchor": center,
                       "sigma": sigma, "tick": tick, "entry_basis": basis,
                       "entry_spread": entry_spread, "initial_roundtrip_cost": cost,
                       "opened_at": now, "filled": False}
        diag["pair_size"] = size
        return self._result("enter_long_A_short_B" if direction == 1 else "enter_short_A_long_B",
                            diag, {a: direction * size, b: -direction * size})
