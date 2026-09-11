import unittest
from types import SimpleNamespace as NS

from stock_market_making.strategies.baseline.quote_protection import (
    PositionAgeTracker, ProtectionSettings, protect_quote,
)


def quote(shift=0.0, active=True):
    return dict(center=100.0, fair_value=100.0, half_spread=.1,
                bid_price=99.9, ask_price=100.1,
                buy_volume=5, sell_volume=5,
                cycle_risk_shift=shift, cycle=dict(active=active))


BOOK = NS(bids=[NS(price=99.9)], asks=[NS(price=100.1)])
SETTINGS = ProtectionSettings()


class QuoteProtectionTests(unittest.TestCase):
    def test_small_reducing_quote_gets_minimum_distance(self):
        out = protect_quote(quote(), BOOK, 2, .1, 'PHILIPS_B', SETTINGS,
                            position_age=3.0)
        self.assertEqual(out['ask_price'], 100.3)
        self.assertEqual(out['sell_volume'], 2)
        self.assertIn('ask', out['protection']['widened_sides'])

    def test_young_reduction_against_strong_signal_is_temporarily_suppressed(self):
        out = protect_quote(quote(.11), BOOK, 2, .1, 'PHILIPS_B', SETTINGS,
                            position_age=1.0)
        self.assertEqual(out['sell_volume'], 0)
        self.assertEqual(out['ask_price'], 100.4)
        self.assertIn('ask', out['protection']['blocked_sides'])
        mature = protect_quote(quote(.11), BOOK, 2, .1, 'PHILIPS_B', SETTINGS,
                               position_age=2.0)
        self.assertEqual(mature['sell_volume'], 2)

    def test_short_inventory_uses_symmetric_reduction_guard(self):
        out = protect_quote(quote(-.11), BOOK, -2, .1, 'PHILIPS_B', SETTINGS,
                            position_age=1.0)
        self.assertEqual(out['buy_volume'], 0)
        self.assertEqual(out['bid_price'], 99.6)
        mature = protect_quote(quote(-.11), BOOK, -2, .1, 'PHILIPS_B', SETTINGS,
                               position_age=2.0)
        self.assertEqual(mature['buy_volume'], 2)

    def test_strongly_adverse_entry_is_suppressed(self):
        out = protect_quote(quote(-.11), BOOK, 0, .1, 'PHILIPS_B', SETTINGS)
        self.assertEqual(out['buy_volume'], 0)
        self.assertGreater(out['sell_volume'], 0)

    def test_b_inventory_has_separate_ten_lot_cap(self):
        near = protect_quote(quote(.05), BOOK, 9, .1, 'PHILIPS_B', SETTINGS,
                             position_age=3.0)
        self.assertEqual(near['buy_volume'], 1)
        capped = protect_quote(quote(.05), BOOK, 10, .1, 'PHILIPS_B', SETTINGS,
                               position_age=3.0)
        self.assertEqual(capped['buy_volume'], 0)
        self.assertEqual(capped['sell_volume'], 5)

    def test_fallback_and_large_inventory_keep_immediate_reduction(self):
        fallback = protect_quote(quote(active=False), BOOK, 2, .1,
                                 'PHILIPS_B', SETTINGS, position_age=0.0)
        self.assertEqual((fallback['ask_price'], fallback['sell_volume']), (100.1, 2))
        large = protect_quote(quote(.2), BOOK, 11, .1,
                              'PHILIPS_B', SETTINGS, position_age=0.0)
        self.assertEqual((large['ask_price'], large['sell_volume']), (100.1, 5))

    def test_position_age_tracker_does_not_delay_inherited_inventory(self):
        tracker = PositionAgeTracker()
        self.assertIsNone(tracker.observe(3, 10.0))
        tracker.observe(0, 11.0)
        self.assertEqual(tracker.observe(-2, 12.0), 0.0)
        self.assertEqual(tracker.observe(-1, 13.5), 1.5)
        self.assertEqual(tracker.observe(1, 14.0), 0.0)

    def test_a_is_unchanged(self):
        original = quote(.2)
        self.assertIs(protect_quote(original, BOOK, 2, .1, 'PHILIPS_A', SETTINGS), original)


if __name__ == '__main__':
    unittest.main()
