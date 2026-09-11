import unittest

from stock_market_making.strategies.volatility_adaptive.volatility import (
    EWMAVolatility, VolatilitySettings, apply_volatility_quote,
)


def quote(position=0):
    return dict(center=100.0, fair_value=100.0, half_spread=0.1,
                bid_price=99.9, ask_price=100.1,
                buy_volume=5, sell_volume=5,
                inventory_adjustment=0.0)


class VolatilityAdaptiveTests(unittest.TestCase):
    def setUp(self):
        self.settings = VolatilitySettings(min_samples=3)

    def test_stable_market_keeps_base_spread(self):
        model = EWMAVolatility(self.settings)
        model.observe('A', 100.0, 1.0, 0.1)
        model.observe('A', 100.0, 2.0, 0.1)
        snapshot = model.observe('A', 100.0, 3.0, 0.1)
        out = apply_volatility_quote(quote(), 0, 0.1, snapshot, self.settings)
        self.assertTrue(snapshot['ready'])
        self.assertEqual((out['bid_price'], out['ask_price']), (99.9, 100.1))

    def test_large_moves_widen_both_sides(self):
        model = EWMAVolatility(self.settings)
        model.observe('A', 100.0, 1.0, 0.1)
        model.observe('A', 100.3, 2.0, 0.1)
        snapshot = model.observe('A', 99.7, 3.0, 0.1)
        out = apply_volatility_quote(quote(), 0, 0.1, snapshot, self.settings)
        self.assertLess(out['bid_price'], 99.9)
        self.assertGreater(out['ask_price'], 100.1)
        self.assertGreater(out['volatility']['target_full_spread'], 0.2)

    def test_duplicate_snapshot_does_not_decay_estimate(self):
        model = EWMAVolatility(self.settings)
        model.observe('A', 100.0, 1.0, 0.1)
        updated = model.observe('A', 100.2, 2.0, 0.1)
        duplicate = model.observe('A', 100.2, 2.0, 0.1)
        self.assertEqual(duplicate['reason'], 'duplicate')
        self.assertEqual(duplicate['variance_rate'], updated['variance_rate'])
        self.assertEqual(duplicate['samples'], updated['samples'])

    def test_instruments_have_independent_state(self):
        model = EWMAVolatility(self.settings)
        model.observe('A', 100.0, 1.0, 0.1)
        model.observe('B', 200.0, 1.0, 0.1)
        a = model.observe('A', 100.5, 2.0, 0.1)
        b = model.observe('B', 200.0, 2.0, 0.1)
        self.assertGreater(a['sigma'], b['sigma'])

    def test_gap_resets_warmup(self):
        model = EWMAVolatility(self.settings)
        model.observe('A', 100.0, 1.0, 0.1)
        model.observe('A', 100.4, 2.0, 0.1)
        snapshot = model.observe('A', 101.0, 8.0, 0.1)
        self.assertEqual(snapshot['reason'], 'gap_reset')
        self.assertFalse(snapshot['ready'])
        self.assertEqual(snapshot['sigma'], 0.0)

    def test_extreme_volatility_halts_only_increasing_sides(self):
        snapshot = dict(ready=True, sigma=0.6, sigma_ticks=6.0)
        flat = apply_volatility_quote(quote(), 0, 0.1, snapshot, self.settings)
        self.assertEqual((flat['buy_volume'], flat['sell_volume']), (0, 0))
        long = apply_volatility_quote(quote(), 3, 0.1, snapshot, self.settings)
        self.assertEqual(long['buy_volume'], 0)
        self.assertEqual(long['sell_volume'], 5)
        short = apply_volatility_quote(quote(), -3, 0.1, snapshot, self.settings)
        self.assertEqual(short['buy_volume'], 5)
        self.assertEqual(short['sell_volume'], 0)


if __name__ == '__main__':
    unittest.main()
