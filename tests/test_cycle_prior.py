from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
import math
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from stock_market_making.strategies.baseline.cycle_signal import CycleSettings, CycleSignal


def books(epoch, basis):
    def one(mid):
        return NS(timestamp=datetime.fromtimestamp(epoch, timezone.utc),
                  bids=[NS(price=mid - .1, volume=100)],
                  asks=[NS(price=mid + .1, volume=100)])
    return {'PHILIPS_A': one(100 + basis), 'PHILIPS_B': one(100)}


class HistoricalCyclePriorTests(unittest.TestCase):
    def settings(self, **changes):
        values = dict(prior_enabled=True, prior_peak_epoch_seconds=165.95,
                      prior_center=0, prior_amplitude=3.1, prior_rmse=.9,
                      prior_fit_weight=.65, prior_phase_lock_seconds=180,
                      max_gap_seconds=10)
        values.update(changes)
        return CycleSettings(**values)

    @staticmethod
    def expected(epoch, amplitude=3.1, peak=165.95):
        return amplitude * math.cos(2 * math.pi * (epoch - peak) / 180)

    def observe(self, model, now, epoch, basis=None):
        if basis is None:
            basis = round(self.expected(epoch), 1)
        return model.observe(books(epoch, basis), {'PHILIPS_A': .1, 'PHILIPS_B': .1},
                             now, epoch)

    def test_prior_is_active_on_first_clean_snapshot(self):
        model = CycleSignal(self.settings())
        epoch = 1800000165.95
        signal = self.observe(model, 0, epoch)
        self.assertTrue(signal['active'])
        self.assertEqual(signal['samples'], 1)
        self.assertEqual(signal['quality_scope'], 'historical_epoch_phase_prior')
        self.assertAlmostEqual(signal['fitted_amplitude'], 3.1)

    def test_gap_reanchors_epoch_phase_without_new_warmup(self):
        model = CycleSignal(self.settings())
        first_epoch = 1800000165.95
        self.observe(model, 0, first_epoch)
        signal = self.observe(model, 31, first_epoch + 31)
        self.assertTrue(signal['active'])
        self.assertEqual(signal['samples'], 1)
        expected_change = (self.expected(first_epoch + 31 + 15)
                           - self.expected(first_epoch + 31))
        self.assertAlmostEqual(signal['predicted_basis_change'], expected_change, places=6)

    def test_short_history_only_fine_tunes_locked_phase(self):
        model = CycleSignal(self.settings())
        start = 1800000100.0
        signal = None
        for second in range(36):
            epoch = start + second
            signal = self.observe(model, second, epoch,
                                  round(self.expected(epoch, amplitude=2.8) + .05, 1))
        self.assertTrue(signal['active'])
        self.assertEqual(signal['quality_scope'],
                         'historical_phase_prior_online_amplitude')
        self.assertLess(abs(signal['fitted_amplitude'] - 2.8), .35)

    def test_full_cycle_bounds_online_phase_correction(self):
        model = CycleSignal(self.settings())
        start = 1800000000.0
        signal = None
        for second in range(201):
            epoch = start + second
            signal = self.observe(
                model, second, epoch,
                round(self.expected(epoch, peak=165.95 + 12), 1))
        fitted_peak = (math.atan2(model.weights[1], model.weights[2]) * 180
                       / (2 * math.pi)) % 180
        phase_delta = (fitted_peak - 165.95 + 90) % 180 - 90
        self.assertLessEqual(abs(phase_delta), 5.000001)
        self.assertEqual(signal['quality_scope'],
                         'historical_phase_prior_bounded_online_fit')

    def test_default_model_still_requires_warmup(self):
        model = CycleSignal(CycleSettings())
        signal = self.observe(model, 0, 1800000165.95)
        self.assertFalse(signal['active'])
        self.assertEqual(signal['reason'], 'warmup')


if __name__ == '__main__':
    unittest.main()
