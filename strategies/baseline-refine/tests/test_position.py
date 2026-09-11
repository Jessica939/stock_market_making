import unittest
import importlib
from pathlib import Path
import sys
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
PACKAGE = 'stock_market_making.strategies.baseline-refine'
quote_strategy = importlib.import_module(PACKAGE + '.run').load_quote_definitions
CyclePosition = importlib.import_module(PACKAGE + '.cycle_position').CyclePosition
cycle = importlib.import_module(PACKAGE + '.cycle_signal')
CycleSettings, apply_cycle_quote = cycle.CycleSettings, cycle.apply_cycle_quote
protect_quote = importlib.import_module(PACKAGE + '.quote_protection').protect_quote
QuoteManager = importlib.import_module(PACKAGE + '.order_execution').QuoteManager


class CyclePositionTests(unittest.TestCase):
    def setUp(self):
        self.strategy = quote_strategy()
        self.plan = CyclePosition(**self.strategy['B_POSITION_SETTINGS'])
        self.signal = dict(active=True, predicted_B_change=3., fit_weight=1.)

    def quote(self, now, position=0, mid=100):
        book = NS(bids=[NS(price=mid-.1,volume=100)], asks=[NS(price=mid+.1,volume=100)])
        s = self.strategy
        quote = s['calculate_quote'](book,position,.1)
        quote = apply_cycle_quote(quote,book,position,.1,'PHILIPS_B',self.signal,
                                  s['CYCLE_SETTINGS'],s['SOFT_LIMIT'],apply_price_shift=s['CYCLE_PRICE_SHIFT_ENABLED'])
        quote = protect_quote(quote,book,position,.1,'PHILIPS_B',s['B_PROTECTION'])
        return self.plan.apply(quote,book,position,.1,now)

    def test_builds_toward_target_then_holds_until_original_deadline(self):
        q=self.quote(0)
        self.assertEqual((q['buy_volume'],q['sell_volume']),(5,0))
        self.assertEqual(self.quote(2,5)['sell_volume'],0)
        q=self.quote(5,20)
        self.assertEqual((q['buy_volume'],q['sell_volume']),(0,0))
        self.assertEqual(self.quote(44,20)['sell_volume'],0)
        q=self.quote(45,20)
        self.assertEqual((q['buy_volume'],q['sell_volume'],q['target_position']),(0,5,0))

    def test_signal_changes_do_not_reverse_held_position(self):
        self.quote(0)
        self.quote(1,5)
        for signal in ({'active':False},dict(active=True,predicted_B_change=-3,fit_weight=1)):
            self.signal=signal
            self.assertEqual(self.quote(20,5)['sell_volume'],0)

    def test_build_window_and_frozen_price_gate_prevent_chasing(self):
        self.quote(0)
        self.assertEqual(self.quote(2,5,104)['buy_volume'],0)
        self.assertEqual(self.quote(11,5)['buy_volume'],0)
        self.assertEqual(self.plan.active['exit_at'],45)

    def test_stop_is_latched_and_early_flat_does_not_reenter(self):
        self.quote(0)
        self.assertEqual(self.quote(2,5,97)['sell_volume'],5)
        self.assertEqual(self.quote(3,5)['sell_volume'],5)
        self.assertEqual(self.quote(4,0)['buy_volume'],0)
        self.assertEqual(self.quote(44,0)['buy_volume'],0)
        self.assertEqual(self.quote(45,0)['buy_volume'],5)

    def test_untracked_inventory_is_only_reduced(self):
        q=self.quote(0,-6)
        self.assertEqual((q['buy_volume'],q['sell_volume'],q['target_position']),(5,0,0))
        self.assertTrue(q['reduce_only'])

    def test_fresh_position_capacity_cannot_exceed_cycle_target(self):
        quote=self.quote(0)
        manager=QuoteManager(None)
        desired=manager._desired(quote)
        self.assertEqual(manager._capacity('bid',desired,19),1)
        self.assertEqual(manager._capacity('bid',desired,20),0)
        self.assertEqual(manager._capacity('ask',desired,20),0)

    def test_half_cycle_horizon_supported_but_not_longer(self):
        self.assertEqual(CycleSettings(horizon_seconds=90).horizon_seconds,90)
        with self.assertRaises(ValueError):CycleSettings(horizon_seconds=91)


if __name__=='__main__':
    unittest.main()
