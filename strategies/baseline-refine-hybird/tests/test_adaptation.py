"""Deterministic, causal regime changes through the actual quote pipeline."""
from dataclasses import replace
from datetime import datetime, timezone
import importlib
import math
from pathlib import Path
import random
import sys
from types import SimpleNamespace as NS
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
PACKAGE = 'stock_market_making.strategies.baseline-refine-hybird'
cycle = importlib.import_module(PACKAGE + '.cycle_signal')
load = importlib.import_module(PACKAGE + '.run').load_quote_definitions
Position = importlib.import_module(PACKAGE + '.cycle_position').CyclePosition
A, B = 'PHILIPS_A', 'PHILIPS_B'
WALL, NOW = 1_800_000_000., 1000.
TICKS = {A: .1, B: .1}


def wave(t, period=180, amplitude=3, offset=0, phase=0):
    return offset + amplitude*math.sin(2*math.pi*t/period + phase)


def books(t, basis, width=.2):
    mids = {A: 100., B: round((100-basis)*10)/10}
    return {s: NS(timestamp=datetime.fromtimestamp(WALL+t, timezone.utc),
                  bids=[NS(price=round(mid-width/2, 10), volume=1000)],
                  asks=[NS(price=round(mid+width/2, 10), volume=1000)])
            for s, mid in mids.items()}


def seeded(**overrides):
    cfg = replace(load()['CYCLE_SETTINGS'], **overrides)
    model = cycle.CycleSignal(cfg)
    report = model.seed([(WALL+t, wave(t)) for t in range(-720, 0)],
                        TICKS, NOW, WALL, source='synthetic_180_second_cycle')
    assert report['loaded'], report
    return model


def observe(model, t, value, **kwargs):
    return model.observe(books(t, value, **kwargs), TICKS, NOW+t, WALL+t)


class AdaptationTests(unittest.TestCase):
    def test_stable_cycle_tracks_coefficients_without_parameter_churn(self):
        model = seeded()
        for t in range(181):
            signal = observe(model, t, wave(t))
        self.assertEqual(model.revision, 0)
        self.assertEqual(signal['period_seconds'], 180)
        self.assertGreater(signal['forecast_samples'], 100)
        self.assertLess(signal['forecast_rmse'], .05)
        self.assertGreater(signal['fit_weight'], .99)
        self.assertTrue(signal['active'])
        self.assertLessEqual(len(model.history), 721)
        self.assertLessEqual(len(model.pending_forecasts), 45)
        self.assertLessEqual(len(model.forecast_errors), 181)

    def test_period_can_shorten_and_lengthen_after_confirmed_change(self):
        for period in (140, 230):
            with self.subTest(period=period):
                model = seeded()
                changes = []
                revision = 0
                for t in range(721):
                    signal = observe(model, t, wave(t, period=period))
                    if model.revision != revision:
                        changes.append(t)
                        revision = model.revision
                self.assertAlmostEqual(signal['period_seconds'], period, delta=5)
                self.assertEqual(signal['window_seconds'], 360)
                self.assertTrue(signal['active'])
                self.assertGreater(signal['fit_weight'], .95)
                self.assertLess(signal['forecast_rmse'], .1)
                self.assertTrue(changes)
                self.assertGreaterEqual(changes[0], 15)
                self.assertTrue(all(b-a >= 30 for a, b in zip(changes, changes[1:])))

    def test_offset_amplitude_and_phase_relearn_and_recover(self):
        model = seeded()
        for t in range(601):
            signal = observe(model, t, wave(t, amplitude=2, offset=2, phase=.7))
        self.assertAlmostEqual(signal['fitted_offset'], 2, delta=.05)
        self.assertAlmostEqual(signal['fitted_amplitude'], 2, delta=.05)
        self.assertAlmostEqual(signal['predicted_B_change'],
            wave(600, amplitude=2, offset=2, phase=.7)-wave(645, amplitude=2, offset=2, phase=.7), delta=.05)
        self.assertTrue(signal['active'])
        self.assertGreater(signal['fit_weight'], .95)

    def test_single_outlier_does_not_switch_period_or_window(self):
        model = seeded()
        for t in range(91):
            signal = observe(model, t, wave(t)+(4 if t == 20 else 0))
            if t == 20:
                self.assertEqual(signal['reason'], 'residual_shock')
        self.assertEqual(model.revision, 0)
        self.assertTrue(signal['active'])

    def test_disappearing_cycle_stops_new_entries_and_keeps_learning(self):
        model = seeded()
        strategy = load()
        plan = Position(**strategy['B_POSITION_SETTINGS'])
        rng = random.Random(812)
        for t in range(721):
            value = rng.uniform(-.2, .2)
            signal = observe(model, t, value)
        self.assertFalse(signal['active'])
        self.assertFalse(model.needs_bootstrap)
        self.assertEqual(model.last_sample, NOW+720)
        quote = strategy['plan_quote'](books(720, value)[B], 0, .1, B, signal, plan, NOW+720)
        self.assertEqual((quote['buy_volume'], quote['sell_volume']), (0, 0))
        for t in range(721, 1261):
            signal = observe(model, t, wave(t))
        self.assertTrue(signal['active'])
        self.assertAlmostEqual(signal['period_seconds'], 180, delta=5)

    def test_wide_books_still_teach_model_and_do_not_block_existing_exit(self):
        model = seeded()
        strategy = load()
        plan = Position(**strategy['B_POSITION_SETTINGS'])
        strategy['plan_quote'](books(0, 0)[B], 0, .1, B,
            dict(active=True, predicted_B_change=3, fit_weight=1), plan, NOW)
        for t in range(61):
            signal = observe(model, t, wave(t), width=3)
        self.assertEqual(signal['reason'], 'wide_pair_spread')
        self.assertEqual(model.last_sample, NOW+60)
        self.assertGreater(signal['forecast_samples'], 0)
        quote = strategy['plan_quote'](books(60, -5, width=3)[B], 183, .1, B, signal, plan, NOW+60)
        self.assertEqual(quote['sell_volume'], 183)
        self.assertTrue(quote['reduce_only'])

    def test_unmatured_forecasts_and_duplicate_books_do_not_inflate_evidence(self):
        model = seeded()
        for t in range(45):
            observe(model, t, wave(t))
        self.assertEqual(model.forecast_quality['forecast_samples'], 0)
        signal = observe(model, 45, wave(45))
        self.assertEqual(signal['forecast_samples'], 1)
        expected = (len(model.history), len(model.pending_forecasts), signal)
        for _ in range(5):
            signal = observe(model, 45, wave(45))
        self.assertEqual((len(model.history), len(model.pending_forecasts), signal), expected)

    def test_validation_never_fits_its_holdout(self):
        model = seeded()
        rows = list(model.history)
        original_fit = model._fit
        fitted_times = []
        def recording_fit(data, period=None):
            fitted_times.extend(t for t, _ in data)
            return original_fit(data, period)
        model._fit = recording_fit
        first = model._validate(rows, 180, 720, .1)
        cutoff = rows[-1][0]-45
        changed = [(t, y if t <= cutoff else y+10) for t, y in rows]
        second = model._validate(changed, 180, 720, .1)
        self.assertTrue(all(t <= cutoff for t in fitted_times))
        self.assertLess(first['mse'], 1e-20)
        self.assertGreater(second['mse'], 90)

    def test_realized_error_uses_the_original_issued_forecast(self):
        # Isolate coefficient refits; a period/window switch intentionally
        # discards the old model's forecast queue.
        model = seeded(adaptive=False)
        observe(model, 0, 0)
        issued_prediction = model.pending_forecasts[0][1]
        for t in range(1, 46):
            signal = observe(model, t, wave(t)+2)
        self.assertEqual(signal['forecast_samples'], 1)
        self.assertAlmostEqual(signal['forecast_rmse'], abs(5-issued_prediction))

    def test_invalid_or_unsynchronized_books_do_not_supply_learning_evidence(self):
        model = seeded()
        observe(model, 0, 0)
        samples = len(model.history)
        invalid = books(1, wave(1))
        invalid[B].timestamp = datetime.fromtimestamp(WALL, timezone.utc)
        signal = model.observe(invalid, TICKS, NOW+1, WALL+1)
        self.assertEqual(signal['reason'], 'unsynchronized_books')
        self.assertEqual(len(model.history), samples)
        signal = model.observe({A: None, B: None}, TICKS, NOW+2, WALL+2)
        self.assertEqual(signal['reason'], 'invalid_pair_books')
        self.assertEqual(len(model.history), samples)

    def test_short_history_uses_initial_period_until_search_has_coverage(self):
        model = cycle.CycleSignal(load()['CYCLE_SETTINGS'])
        for t in range(91):
            signal = observe(model, t, wave(t, period=140))
        self.assertEqual(signal['period_seconds'], 180)
        self.assertEqual(model.revision, 0)

    def test_gap_and_clock_reversal_clear_learning_and_prediction_evidence(self):
        model = seeded()
        for t in range(51):
            observe(model, t, wave(t))
        signal = observe(model, 70, wave(70))
        self.assertFalse(signal['active'])
        self.assertEqual(signal['samples'], 1)
        self.assertEqual(signal['forecast_samples'], 0)
        self.assertTrue(model.needs_bootstrap)
        self.assertEqual(observe(model, 69, wave(69))['reason'], 'clock_reversal')
        self.assertEqual(len(model.history), 0)

    def test_adaptation_can_be_disabled(self):
        model = seeded(adaptive=False)
        for t in range(361):
            signal = observe(model, t, wave(t, period=140))
        self.assertEqual(signal['period_seconds'], 180)
        self.assertEqual(signal['window_seconds'], 720)
        self.assertEqual(signal['adaptation']['state'], 'disabled')

    def test_invalid_adaptation_configuration_is_rejected(self):
        for settings in (dict(adaptive=1), dict(min_period_seconds=200),
                         dict(max_period_seconds=170), dict(period_step_seconds=.01),
                         dict(fast_history_seconds=900), dict(switch_improvement=1),
                         dict(switch_confirmations=1.5), dict(switch_confirmations=0),
                         dict(period_min_cycles=.5)):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                replace(load()['CYCLE_SETTINGS'], **settings)


if __name__ == '__main__':
    unittest.main()
