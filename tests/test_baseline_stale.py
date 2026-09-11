from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hybrid_fakes import A, B, Clock, Exchange, Journal
from stock_market_making.strategies.baseline_stale.engine import CombinedEngine
from stock_market_making.strategies.baseline_stale.run import load_config
from stock_market_making.strategies.baseline_refine_loader import load_baseline_refine


class CombinedExchange(Exchange):
    def get_positions_and_cash(self):
        return {s: {'volume': self.positions[s], 'cash': 0.0} for s in (A, B)}


class BaselineStaleTests(unittest.TestCase):
    def make_engine(self):
        clock = Clock()
        exchange = CombinedExchange(clock)
        exchange.mid[A] = 100.0
        exchange.mid[B] = 90.0
        journal = Journal()
        config, stale = load_config()
        config = dict(config, session_seconds=60.0, closeout_seconds=10.0)
        stale = dict(stale, session_seconds=60.0, closeout_seconds=10.0)
        baseline = load_baseline_refine()
        engine = CombinedEngine(
            exchange, config, stale, journal, baseline,
            clock=clock.time, sleep=clock.sleep, wall=clock.time,
            terminal_quantity=lambda *_args: None,
        )
        return clock, exchange, journal, engine

    def test_stale_ioc_keeps_a_market_making_live_and_is_separately_owned(self):
        clock, exchange, _journal, engine = self.make_engine()
        engine.step()
        self.assertIsNotNone(engine.stale.pending)
        self.assertTrue(exchange.orders[A])
        self.assertFalse(exchange.orders[B])

        clock.now += 0.05
        engine.step()
        stale_b = engine.account.positions['pair'][B]
        self.assertGreater(stale_b, 0)
        self.assertEqual(exchange.positions[B], stale_b)
        self.assertEqual(engine.account.positions['mm'][B], 0)
        self.assertTrue(exchange.orders[A])
        self.assertFalse(exchange.orders[B])

    def test_startup_inventory_belongs_to_baseline_not_stale(self):
        clock = Clock()
        exchange = CombinedExchange(clock)
        exchange.positions.update({A: -7, B: 9})
        journal = Journal()
        config, stale = load_config()
        baseline = load_baseline_refine()
        engine = CombinedEngine(exchange, config, stale, journal, baseline,
                                clock=clock.time, sleep=clock.sleep, wall=clock.time)
        self.assertEqual(engine.account.positions['mm'], {A: -7, B: 9})
        self.assertEqual(engine.account.positions['pair'], {A: 0, B: 0})
        self.assertEqual(engine.stale.executor.positions()[B], 0)

    def test_pending_stale_trade_cancels_only_baseline_b(self):
        clock, exchange, _journal, engine = self.make_engine()
        engine.account.insert('mm', A, price=99.9, volume=2, side='bid', order_type='limit')
        engine.account.insert('mm', B, price=89.9, volume=2, side='bid', order_type='limit')
        engine.stale.pending = {'ready': 10.0, 'expires': 20.0}
        engine.step()
        self.assertTrue(exchange.orders[A])
        self.assertFalse(exchange.orders[B])

    def test_aggregate_actual_position_caps_stale_ioc(self):
        clock = Clock()
        exchange = CombinedExchange(clock)
        exchange.positions[B] = 80
        journal = Journal()
        config, stale = load_config()
        engine = CombinedEngine(exchange, config, stale, journal, load_baseline_refine(),
                                clock=clock.time, sleep=clock.sleep, wall=clock.time)
        response = engine.stale_view.insert_order(
            B, price=100.1, volume=50, side='bid', order_type='ioc')
        self.assertFalse(response.success)
        self.assertEqual(exchange.positions[B], 80)

    def test_stale_exit_restores_inherited_baseline_b(self):
        clock = Clock()
        exchange = CombinedExchange(clock)
        exchange.positions[B] = 9
        exchange.mid.update({A: 100.0, B: 90.0})
        journal = Journal()
        config, stale = load_config()
        config = dict(config, session_seconds=60.0, closeout_seconds=10.0)
        stale = dict(stale, session_seconds=60.0, closeout_seconds=10.0)
        engine = CombinedEngine(exchange, config, stale, journal, load_baseline_refine(),
                                clock=clock.time, sleep=clock.sleep, wall=clock.time)
        engine.step()
        clock.now += 0.05
        engine.step()
        self.assertGreater(engine.account.positions['pair'][B], 0)
        clock.now += stale['hold_seconds'] + 0.1
        engine.step()
        self.assertEqual(engine.account.positions['pair'][B], 0)
        self.assertEqual(engine.account.positions['mm'][B], 9)
        self.assertEqual(exchange.positions[B], 9)


if __name__ == '__main__':
    unittest.main()
