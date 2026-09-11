from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hybrid_fakes import A, B, Clock, Exchange, Journal
from stock_market_making.strategies.baseline_stale.engine import CombinedEngine
from stock_market_making.strategies.baseline_stale.run import load_config, step_or_recover
from stock_market_making.strategies.baseline_refine_loader import load_baseline_refine
from stock_market_making.strategies.baseline_stale.state import StateError, StateStore


class CombinedExchange(Exchange):
    def get_positions_and_cash(self):
        return {s: {'volume': self.positions[s], 'cash': 0.0} for s in (A, B)}


class DynamicEnum:
    """Cap'n Proto-like side: string-comparable but not concatenable."""
    def __init__(self, value):
        self.value = value
    def __str__(self):
        return self.value
    def __eq__(self, other):
        return self.value == str(other)
    def __hash__(self):
        return hash(self.value)


class BaselineStaleTests(unittest.TestCase):
    def test_combined_b_allocation_prefers_stale(self):
        config, stale = load_config()
        self.assertEqual(config['baseline_b_position_limit'], 30)
        self.assertEqual(config['baseline_b_soft_limit'], 30)
        self.assertEqual(config['stale_b_position_limit'], 70)
        self.assertEqual(stale['max_order_lots'], 70)
        self.assertEqual(config['baseline_b_position_limit'] + stale['max_order_lots'],
                         config['position_limit'])

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

    def test_crash_marker_blocks_silent_inventory_reassignment(self):
        config, _stale = load_config()
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / 'state.json')
            state.acquire()
            try:
                state.invalidate(config)
                with self.assertRaises(StateError):
                    state.load(config)
            finally:
                state.close()

    def test_explicit_adoption_archives_old_state(self):
        config, _stale = load_config()
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / 'state.json')
            state.acquire()
            try:
                state.invalidate(config)
                archive = state.archive_for_adoption()
                self.assertTrue(archive.is_file())
                self.assertEqual(archive.read_text(encoding='utf-8'),
                                 state.path.read_text(encoding='utf-8'))
            finally:
                state.close()

    def test_capnp_order_and_fill_sides_are_normalized(self):
        _clock, exchange, _journal, engine = self.make_engine()
        engine.step()
        for orders in exchange.orders.values():
            for order in orders.values():
                order.side = DynamicEnum(str(order.side))
        engine.step()  # Existing LIMIT reconciliation must not concatenate the enum.

        oid = next(iter(exchange.orders[A]))
        exchange.fill(A, oid, 1)
        engine.account.audit()
        self.assertEqual(engine.account.positions['mm'][A], 1)

    def test_quote_capacity_race_cancels_symbol_and_keeps_engine_running(self):
        _clock, exchange, journal, engine = self.make_engine()
        engine.step()
        self.assertTrue(exchange.orders[A])
        manager = engine.quote_managers[A]
        manager.reconcile = Mock(side_effect=engine.order_limit_error(
            'PHILIPS_A: bid remaining volume exceeds current capacity'))

        engine.step()

        self.assertFalse(exchange.orders[A])
        recovered = [row for row in journal.rows
                     if row['kind'] == 'baseline_quote_recovered']
        self.assertEqual(recovered[-1]['error_type'], 'OrderLimitError')

    def test_healthy_step_exception_cancels_mm_and_continues_session(self):
        _clock, exchange, journal, engine = self.make_engine()
        engine.step()
        self.assertTrue(exchange.orders[A])
        engine.step = Mock(side_effect=RuntimeError('temporary calculation failure'))

        completed = step_or_recover(engine, exchange, journal)

        self.assertFalse(completed)
        self.assertFalse(exchange.orders[A])
        self.assertFalse(engine.account.halted)
        self.assertEqual(journal.rows[-1]['kind'], 'combined_step_recovered')

    def test_hard_fault_is_not_hidden_by_loop_recovery(self):
        _clock, exchange, journal, engine = self.make_engine()
        engine.account.halted = True
        engine.step = Mock(side_effect=RuntimeError('unknown order state'))

        with self.assertRaisesRegex(RuntimeError, 'unknown order state'):
            step_or_recover(engine, exchange, journal)


if __name__ == '__main__':
    unittest.main()
