"""Cycle targets through the real shared IOC executor, without live trading."""
import importlib
import math
import unittest

import test_hybrid as hybrid
from test_hybrid import A, B, WALL, run


class CycleTargetTests(unittest.TestCase):
    frame = hybrid.HybridTests.frame
    event = hybrid.HybridTests.event

    def setUp(self):
        hybrid.HybridTests.setUp(self)
        self.strategy.target_cycle.enabled = True
        self.signal['active'] = False
        self.forecast = dict(active=True, predicted_B_change=4., fit_weight=1.)
        cycle = self.strategy.target_cycle
        def observe(raw, *args):
            cycle.signal = dict(self.forecast, active=bool(raw) and self.forecast['active'])
        cycle.observe = observe

    def enter(self, **frame):
        self.strategy.step()
        self.assertEqual(self.strategy.pending['kind'], 'cycle_target')
        self.frame(.05, **frame)
        self.strategy.step()
        self.assertEqual(self.strategy.entry['kind'], 'cycle_target')

    def test_cycle_long_uses_full_target_and_freezes_price(self):
        self.enter()
        self.assertEqual(self.strategy.cycle_position, 100)
        self.assertEqual(self.strategy.entry['target_price'], 103.1)
        self.forecast['predicted_B_change'] = 10
        self.frame(46)
        self.strategy.step()
        self.assertEqual(self.strategy.cycle_position, 100)
        self.assertEqual(self.strategy.entry['target_price'], 103.1)
        self.assertTrue([fields for name, fields in self.events if name == 'cycle_position'][-1]['hold_reference_elapsed'])
        self.assertTrue(all(kind == 'ioc' for iid, kind, *_ in self.exchange.sent if iid == B))

    def test_short_target_rounds_toward_entry_and_exits(self):
        self.forecast['predicted_B_change'] = -4
        self.enter()
        self.assertEqual(self.strategy.cycle_position, -100)
        self.assertEqual(self.strategy.entry['target_price'], 97.1)
        self.frame(1, b=96.9)
        self.strategy.step()
        self.assertEqual(self.strategy.cycle_position, 0)

    def test_baseline_target_formula_equivalence_both_directions(self):
        baseline = importlib.import_module('stock_market_making.strategies.baseline-refine.cycle_position')
        for change in (-4.37, 4.37):
            with self.subTest(change=change):
                raw = self.exchange.get_last_price_book(B)
                signal = dict(active=True, predicted_B_change=change, fit_weight=.73)
                expected = baseline.CyclePosition().apply(dict(cycle=signal, buy_volume=100,
                    sell_volume=100), raw, 0, .1, 0)['cycle_position']
                cycle = run.strategy_module.TargetCycle(self.config)
                cycle.signal = signal
                actual = cycle.quote(raw, 0, .1, 0)['cycle_position']
                self.assertEqual(actual, expected)

    def test_sniper_has_priority_and_keeps_five_second_timeout(self):
        self.signal['active'] = True
        self.strategy.step()
        self.assertNotIn('kind', self.strategy.pending)
        self.assertIsNone(self.strategy.target_cycle.position.active)
        self.frame(.05)
        self.strategy.step()
        self.frame(5.1)
        self.strategy.step()
        self.assertEqual(self.strategy.cycle_position, 0)

    def test_partial_entry_adds_only_remainder_with_same_target(self):
        self.enter(depth=20)
        self.assertEqual(self.strategy.cycle_position, 20)
        self.frame(.1)
        self.strategy.step()
        self.frame(.2)
        self.strategy.step()
        self.assertEqual(self.strategy.cycle_position, 100)
        self.assertEqual(self.strategy.entry['target_price'], 103.1)
        self.assertEqual([row[-1] for row in self.exchange.sent if row[0] == B], [20, 80])

    def test_expired_build_window_keeps_partial_position(self):
        self.enter(depth=20)
        self.frame(11)
        self.strategy.step()
        self.assertEqual(self.strategy.cycle_position, 20)
        self.assertIsNone(self.strategy.pending)

    def test_take_profit_latches_across_partial_exit_and_price_reversal(self):
        self.enter()
        self.frame(1, b=103.1, depth=20)
        self.strategy.step()
        self.assertEqual(self.strategy.cycle_position, 80)
        self.assertEqual(self.strategy.exit_intent['reason'], 'cycle_take_profit')
        self.frame(1.1, b=100)
        self.strategy.step()
        self.assertEqual(self.strategy.cycle_position, 0)
        self.assertIsNone(self.strategy.target_cycle.position.active)

    def test_stop_requires_continuous_adverse_observations_and_no_valid_a(self):
        self.enter()
        for t in (1., 2., 3.):
            self.frame(t, b=94)
            self.exchange.books[A] = None
            self.strategy.step()
            self.assertEqual(self.strategy.cycle_position, 100)
        self.frame(4, b=94)
        self.exchange.books[A] = None
        self.strategy.step()
        self.assertEqual(self.strategy.cycle_position, 0)

    def test_entry_recheck_rejects_price_that_consumes_target_edge(self):
        self.strategy.step()
        target = self.strategy.target_cycle.position.active['target_price']
        self.frame(.05, b=103)
        self.strategy.step()
        self.assertEqual(self.strategy.cycle_position, 0)
        self.assertFalse(any(row[0] == B for row in self.exchange.sent))
        self.assertEqual(self.strategy.target_cycle.position.active['target_price'], target)

    def test_budget_wait_cannot_send_expired_cycle_entry(self):
        self.strategy.step()
        budget = self.strategy.exchange.request_budget
        while len(budget.sent) < budget.capacity-10:
            budget.call('get_positions', self.exchange.get_positions)
        self.frame(.05)
        self.strategy.step()
        self.assertFalse(any(row[0] == B for row in self.exchange.sent))
        self.assertEqual(self.strategy.cycle_position, 0)

    def test_unproven_cycle_partial_fill_settles_from_account(self):
        self.strategy.executor.terminal_quantity = None
        self.strategy.step()
        self.frame(.05)
        self.exchange.accessible[B]['asks'][100.2] = 20
        self.strategy.step()
        self.assertEqual(self.strategy.cycle_position, 20)
        self.assertFalse(self.strategy.executor.halted)
        self.assertIsNone(self.strategy.executor.pending)

    def test_stop_observation_gap_restarts_confirmation(self):
        self.enter()
        for t in (1., 2., 4., 5., 6.):
            self.frame(t, b=94)
            self.strategy.step()
            self.assertEqual(self.strategy.cycle_position, 100)
        self.frame(7, b=94)
        self.strategy.step()
        self.assertEqual(self.strategy.cycle_position, 0)

    def test_baseline_capacity_and_shutdown_restore_inherited_inventory(self):
        self.exchange.positions[B] = 90
        self.strategy.baseline_b = 90
        self.enter()
        self.assertEqual(self.strategy.cycle_position, 10)
        self.assertEqual(self.exchange.positions[B], 100)
        self.strategy.stop()
        self.frame(.1)
        self.strategy.step()
        self.assertEqual(self.exchange.positions[B], 90)
        self.assertEqual(self.strategy.cycle_position, 0)

    def test_real_forecast_history_seed_and_continued_learning(self):
        cycle = run.strategy_module.TargetCycle(dict(self.config, b_cycle={'enabled': True}))
        rows = [(WALL+t, 3*math.sin(2*math.pi*t/180)) for t in range(-360, 0)]
        report = cycle.model.seed(rows, self.strategy.ticks, 0, WALL, source='test history')
        self.assertTrue(report['loaded'])
        self.frame(.01)
        raw = {iid:self.exchange.get_last_price_book(iid) for iid in (A,B)}
        cycle.observe(raw, self.exchange, self.strategy.ticks, .01, WALL+.01, self.event)
        self.assertTrue(cycle.signal['active'])
        self.assertEqual(cycle.signal['horizon_seconds'], 45)
        self.assertLess(cycle.signal['predicted_B_change'], 0)
        self.assertEqual(len(cycle.model.history), 361)

    def test_invalid_cycle_config_rejected(self):
        for options in ({'enabled':1}, {'position':{'target_lots':101}},
                        {'position':{'take_profit_fraction':1}}, {'unknown':True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                run.strategy_module.validate(dict(self.config, b_cycle=options))


if __name__ == '__main__':
    unittest.main()
