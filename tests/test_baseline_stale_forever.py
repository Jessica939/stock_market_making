from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hybrid_fakes import A, B, Clock, Exchange, Journal
from stock_market_making.strategies.baseline_refine_loader import load_baseline_refine
from stock_market_making.strategies.baseline_stale_forever.engine import CombinedEngine
from stock_market_making.strategies.baseline_stale_forever.run import load_config, step_or_recover


class CombinedExchange(Exchange):
    def get_positions_and_cash(self):
        return {s: {'volume': self.positions[s], 'cash': 0.0} for s in (A, B)}


class ForeverTests(unittest.TestCase):
    def make_engine(self):
        clock = Clock()
        exchange = CombinedExchange(clock)
        exchange.mid.update({A: 100.0, B: 90.0})
        journal = Journal()
        config, stale = load_config()
        config = dict(config, session_seconds=60.0, closeout_seconds=10.0)
        stale = dict(stale, session_seconds=60.0, closeout_seconds=10.0)
        engine = CombinedEngine(
            exchange, config, stale, journal, load_baseline_refine(),
            clock=clock.time, sleep=clock.sleep, wall=clock.time,
            terminal_quantity=lambda *_args: None)
        return exchange, journal, engine

    def test_capacity_race_cancels_quote_without_stopping(self):
        exchange, journal, engine = self.make_engine()
        engine.step()
        manager = engine.quote_managers[A]
        manager.reconcile = Mock(side_effect=engine.order_limit_error(
            'PHILIPS_A: bid remaining volume exceeds current capacity'))

        engine.step()

        self.assertFalse(exchange.orders[A])
        self.assertFalse(engine.account.halted)
        self.assertTrue(any(row['kind'] == 'baseline_quote_recovered'
                            for row in journal.rows))

    def test_recoverable_loop_error_cancels_mm_and_returns(self):
        exchange, journal, engine = self.make_engine()
        engine.step()
        engine.step = Mock(side_effect=RuntimeError('temporary failure'))

        self.assertFalse(step_or_recover(engine, exchange, journal))
        self.assertFalse(exchange.orders[A])
        self.assertFalse(engine.account.halted)

    def test_hard_fault_is_still_propagated(self):
        exchange, journal, engine = self.make_engine()
        engine.account.halted = True
        engine.step = Mock(side_effect=RuntimeError('unknown state'))
        with self.assertRaisesRegex(RuntimeError, 'unknown state'):
            step_or_recover(engine, exchange, journal)


if __name__ == '__main__':
    unittest.main()
