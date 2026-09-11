import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hybrid_fakes import A, B, Clock, Exchange, Journal, Hybrid, load_config
from stock_market_making.strategies.baseline.cycle_signal import CycleSettings
from stock_market_making.strategies.hybrid.state import StateStore, StateError
from stock_market_making.strategies.hybrid.account import AccountFault


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = StateStore(Path(self.temp.name) / 'hybrid.json')
        self.store.acquire()
        self.addCleanup(self.store.close)
        self.clock, self.journal = Clock(), Journal()
        self.raw = Exchange(self.clock)
        self.cfg = load_config()

    def engine(self, restored=None):
        return Hybrid(self.raw, self.cfg, self.journal, self.clock.time,
                      self.clock.sleep, self.clock.time, state_store=self.store,
                      restored=restored, mm_strategy={'CYCLE_SETTINGS': CycleSettings()})

    def owned_fill(self, engine, symbol=B, side='bid', volume=3):
        response = engine.mm_view.insert_order(symbol, price=99.9 if side == 'bid' else 100.1,
                                               volume=volume, side=side, order_type='limit')
        self.raw.fill(symbol, response.order_id)
        engine.account.audit()

    def test_normal_stop_and_restart_retains_mm_inventory_and_cash(self):
        engine = self.engine()
        self.owned_fill(engine, A, 'ask', 1)
        self.owned_fill(engine, B, 'bid', 9)
        engine.baseline, engine.peak = 0., 10.
        cash = copy.deepcopy(engine.account.cash)
        summary = engine.finish('keyboard_interrupt')
        self.assertTrue(summary['state_recoverable'])
        saved = self.store.load(self.cfg)
        resumed = self.engine(saved)
        self.assertEqual(resumed.account.positions['mm'], {A: -1, B: 9})
        self.assertEqual(resumed.account.cash, cash)
        self.assertEqual((resumed.baseline, resumed.peak), (0., 10.))
        self.assertFalse(resumed.stopping)
        self.assertEqual(resumed.mm_view.get_positions(), {A: -1, B: 9})

    def test_cancel_race_is_included_in_final_checkpoint(self):
        engine = self.engine()
        engine.mm_view.insert_order(A, price=99.9, volume=2, side='bid', order_type='limit')
        self.raw.cancel_fill = True
        engine.finish('keyboard_interrupt')
        self.assertEqual(self.store.load(self.cfg)['positions']['mm'][A], 1)

    def test_missing_state_with_inventory_does_not_guess_ownership(self):
        self.raw.positions[B] = 9
        with self.assertRaisesRegex(AccountFault, 'No saved ownership'):
            self.engine()
        self.assertFalse(self.store.path.exists())
        self.assertEqual(self.raw.sent, [])

    def test_mismatch_does_not_overwrite_saved_state_or_send_orders(self):
        self.engine()
        saved = self.store.load(self.cfg)
        original = self.store.path.read_bytes()
        self.raw.positions[A] = 1
        with self.assertRaisesRegex(AccountFault, 'does not match'):
            self.engine(saved)
        self.assertEqual(self.store.path.read_bytes(), original)
        self.assertEqual(self.raw.sent, [])

    def test_outstanding_orders_block_recovery_even_if_position_matches(self):
        engine = self.engine()
        engine.mm_view.insert_order(A, price=99.9, volume=2, side='bid', order_type='limit')
        self.store.checkpoint(engine)
        with self.assertRaisesRegex(StateError, 'unconfirmed'):
            self.store.load(self.cfg)

    def test_step_is_dirty_before_any_order_can_be_sent(self):
        engine = self.engine()
        def crash():
            with self.assertRaises(StateError):
                self.store.load(self.cfg)
            raise TimeoutError('crash during send')
        with patch.object(engine, '_step', side_effect=crash):
            with self.assertRaises(TimeoutError):
                engine.step()
        with self.assertRaises(StateError):
            self.store.load(self.cfg)

    def test_failed_state_write_prevents_execution(self):
        engine = self.engine()
        with patch.object(self.store, 'write', side_effect=OSError('disk full')), patch.object(engine, '_step') as step:
            with self.assertRaises(OSError):
                engine.step()
            step.assert_not_called()

    def test_interrupt_during_step_cannot_be_promoted_to_clean_shutdown(self):
        engine = self.engine()
        with patch.object(engine, '_step', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                engine.step()
        summary = engine.finish('keyboard_interrupt')
        self.assertFalse(summary['state_recoverable'])
        with self.assertRaises(StateError):
            self.store.load(self.cfg)

    def test_order_free_interruption_can_resume_offsetting_ownership_and_pair_timers(self):
        engine = self.engine()
        self.clock.now = 100
        engine.account.positions = {'mm': {A: -3, B: 3}, 'pair': {A: 3, B: -3}}
        engine.pair_policy.active = dict(opened_at=90., exit_at=135., cycle_origin=10.,
                                        direction=1, size=3, filled=True)
        self.store.checkpoint(engine)
        saved = self.store.load(self.cfg)
        # A restarted process has a different monotonic origin; wall time advances 20s.
        from stock_market_making.strategies.hybrid.state import restore_policy
        resumed = self.engine(saved)
        resumed.clock = lambda: 5.
        resumed.wall = lambda: 120.
        restore_policy(resumed, saved)
        self.assertEqual(resumed.account.positions, engine.account.positions)
        self.assertEqual(5 - resumed.pair_policy.active['opened_at'], 30)
        self.assertEqual(resumed.pair_policy.active['exit_at'] - 5, 15)

    def test_risk_stop_remains_latched_after_restart(self):
        engine = self.engine()
        engine.request_stop('session_loss_limit')
        engine.finish('runner_exit')
        resumed = self.engine(self.store.load(self.cfg))
        self.assertTrue(resumed.stopping)
        self.assertEqual(resumed.stop_reason, 'session_loss_limit')

    def test_corruption_config_change_and_double_start_are_rejected(self):
        self.engine()
        with self.assertRaises(StateError):
            self.store.load(dict(self.cfg, mm_position_limit=40))
        other = StateStore(self.store.path)
        with self.assertRaises(StateError):
            other.acquire()
        self.store.path.write_text('{invalid', encoding='utf-8')
        with self.assertRaises(StateError):
            self.store.load(self.cfg)

    def test_failed_replace_preserves_previous_checkpoint(self):
        self.engine()
        previous = self.store.path.read_bytes()
        with patch('os.replace', side_effect=OSError('replace failed')):
            with self.assertRaises(OSError):
                self.store.invalidate()
        self.assertEqual(self.store.path.read_bytes(), previous)


if __name__ == '__main__':
    unittest.main()
