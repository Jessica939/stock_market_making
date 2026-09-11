"""Request-rate and restart regressions with no real exchange connection."""
from collections import Counter
import json
import logging
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import patch

from test_hybrid import A, B, Exchange, SimClock, run

Budget = run.budget_module.RequestBudget
Budgeted = run.budget_module.BudgetedExchange


class RequestTests(unittest.TestCase):
    def test_mixed_reads_updates_cancels_and_failed_calls_share_200(self):
        clock = SimClock()
        timestamps = []
        class Raw:
            def __getattr__(self, name):
                def call(*args, **kwargs):
                    timestamps.append((clock.now, name))
                    # Server double: count every public API call, not just inserts.
                    current = [t for t, _ in timestamps if clock.now-t < 1.]
                    if len(current) > 200:
                        raise AssertionError('Max requests per second exceeded')
                    if name == 'amend_order':
                        raise ValueError('simulated rejected request')
                    return {}
                return call
        budget = Budget(200, clock=clock.monotonic, sleep=clock.sleep)
        exchange = Budgeted(Raw(), budget)
        calls = ('get_positions', 'get_outstanding_orders', 'get_last_price_book',
                 'get_positions_and_cash', 'poll_new_trades', 'poll_new_trade_ticks',
                 'insert_order', 'delete_order', 'amend_order')
        for index in range(900):
            name = calls[index % len(calls)]
            try:
                getattr(exchange, name)(A if index % 2 else B)
            except ValueError:
                self.assertEqual(name, 'amend_order')
        self.assertEqual(sum(budget.counts.values()), 900)
        self.assertEqual(budget.counts, Counter(name for _, name in timestamps))
        self.assertGreaterEqual(clock.now, 4.2)

    def test_records_invocation_time_after_preflight_and_wait(self):
        clock = SimClock()
        budget = Budget(200, clock=clock.monotonic, sleep=clock.sleep)
        for _ in range(budget.capacity):
            budget.call('get_positions', lambda: None)
        before = sum(budget.counts.values())
        budget.acquire()
        self.assertEqual(sum(budget.counts.values()), before)
        self.assertGreater(clock.now, 1.)
        for _ in range(3):
            budget.call('get_positions', lambda: clock.sleep(.02))
        submitted = []
        budget.call('insert_order', lambda: submitted.append(clock.now))
        self.assertEqual(budget.sent[-1], submitted[0])

    def test_invalid_budget_and_reversed_clock_fail(self):
        for cap in (0, 201, True, 200.):
            with self.assertRaises(ValueError):
                Budget(cap)
        clock = SimClock()
        budget = Budget(clock=clock.monotonic, sleep=clock.sleep)
        budget.call('get_positions', lambda: None)
        clock.now = -1
        with self.assertRaises(RuntimeError):
            budget.call('insert_order', lambda: self.fail('must not submit'))

    def test_server_disconnect_reason_is_captured(self):
        capture = run.budget_module.DisconnectCapture()
        capture.emit(logging.LogRecord('client', logging.ERROR, '', 0,
            'Forced disconnect: Max requests per second exceeded.', (), None))
        self.assertIn('Max requests per second exceeded', capture.reason)


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.state = self.folder/'state.json'
        self.logs = self.folder/'runs'
        self.run_id = 'run_previous'
        directory = self.logs/self.run_id
        directory.mkdir(parents=True)
        (directory/'events.jsonl').write_text(json.dumps(dict(
            type='inventory_baseline', baseline_B=9))+'\n')
        self.prior = dict(strategy='final_hybrid_v1', safe_to_start=False,
                          run_id=self.run_id, error='Cannot call function until connected')
        self.state.write_text(json.dumps(self.prior))
        self.exchange = Exchange(SimClock())
        self.exchange.positions[B] = 9
        self.exchange.connect = lambda: None
        self.client = ModuleType('optibook.synchronous_client')
        self.client.Exchange = lambda **kwargs: self.exchange
        self.args = NS(state_file=self.state, log_dir=self.logs, cancel_orders=False)

    def reconcile(self):
        with patch.dict(sys.modules, {'optibook.synchronous_client': self.client}), patch('builtins.print'):
            return run.reconcile_run(self.args, dict(max_requests_per_second=200))

    def test_v1_missing_baseline_recovers_exact_run_and_unlocks_without_orders(self):
        self.assertEqual(self.reconcile(), 0)
        saved = json.loads(self.state.read_text())
        self.assertTrue(saved['safe_to_start'])
        self.assertEqual(saved['baseline_B'], 9)
        self.assertEqual(saved['strategy'], run.VERSION)
        self.assertFalse(self.exchange.sent)
        self.assertFalse(self.exchange.connected)

    def test_current_nonzero_b_becomes_new_baseline(self):
        self.exchange.positions[B] = 11
        self.assertEqual(self.reconcile(), 0)
        saved = json.loads(self.state.read_text())
        self.assertEqual(saved['baseline_B'], 11)
        self.assertEqual(saved['previous_baseline_B'], 9)
        self.assertEqual(saved['baseline_change_B'], 2)
        self.assertFalse(self.exchange.sent)

    def test_flat_b_is_automatically_adopted_as_new_baseline(self):
        self.exchange.positions[B] = 0
        self.assertEqual(self.reconcile(), 0)
        saved = json.loads(self.state.read_text())
        self.assertTrue(saved['safe_to_start'])
        self.assertEqual(saved['baseline_B'], 0)
        self.assertEqual(saved['previous_baseline_B'], 9)
        self.assertEqual(saved['reconciliation'], 'adopted_current_B_as_new_baseline')

    def test_reconcile_automatically_cancels_a_and_b_orders(self):
        for iid, order_id in ((A, 1), (B, 2)):
            self.exchange.resting[iid][order_id] = NS(
                order_id=order_id, side='bid', price=100., volume=1)
        self.assertEqual(self.reconcile(), 0)
        saved = json.loads(self.state.read_text())
        self.assertTrue(saved['safe_to_start'])
        self.assertFalse(self.exchange.resting[A])
        self.assertFalse(self.exchange.resting[B])
        self.assertEqual(
            {(row['instrument'], row['order_id']) for row in saved['cancelled_orders']},
            {(A, 1), (B, 2)},
        )

    def test_explicit_cancel_orders_clears_a_and_b_then_unlocks(self):
        for iid, order_id in ((A, 11), (B, 12)):
            self.exchange.resting[iid][order_id] = NS(
                order_id=order_id, side='bid', price=100., volume=1)
        self.args.cancel_orders = True
        self.assertEqual(self.reconcile(), 0)
        saved = json.loads(self.state.read_text())
        self.assertTrue(saved['safe_to_start'])
        self.assertEqual(
            {(row['instrument'], row['order_id']) for row in saved['cancelled_orders']},
            {(A, 11), (B, 12)},
        )
        self.assertFalse(self.exchange.resting[A])
        self.assertFalse(self.exchange.resting[B])
        self.assertFalse(any(item[1] == 'limit' for item in self.exchange.sent))

    def test_cancel_then_current_b_is_adopted(self):
        self.exchange.resting[A][11] = NS(
            order_id=11, side='bid', price=100., volume=1)
        self.exchange.positions[B] = 10
        self.args.cancel_orders = True
        self.assertEqual(self.reconcile(), 0)
        self.assertFalse(self.exchange.resting[A])
        saved = json.loads(self.state.read_text())
        self.assertTrue(saved['safe_to_start'])
        self.assertEqual(saved['baseline_B'], 10)

    def test_cancel_failure_keeps_state_locked(self):
        self.exchange.resting[A][11] = NS(
            order_id=11, side='bid', price=100., volume=1)
        self.exchange.cancel_ok = False
        self.args.cancel_orders = True
        self.assertEqual(self.reconcile(), 2)
        self.assertIn(11, self.exchange.resting[A])
        self.assertFalse(json.loads(self.state.read_text())['safe_to_start'])

    def test_missing_original_log_never_uses_another_run(self):
        (self.logs/self.run_id/'events.jsonl').unlink()
        other = self.logs/'run_other'
        other.mkdir()
        (other/'events.jsonl').write_text(json.dumps(dict(type='inventory_baseline', baseline_B=9))+'\n')
        self.assertEqual(self.reconcile(), 2)
        self.assertEqual(json.loads(self.state.read_text()), self.prior)

    def test_risk_stop_is_not_cleared(self):
        self.prior.update(baseline_B=9, risk_stopped=True)
        self.state.write_text(json.dumps(self.prior))
        self.assertEqual(self.reconcile(), 2)
        self.assertEqual(json.loads(self.state.read_text()), self.prior)

    def test_risk_stop_missing_from_v1_state_is_recovered_from_log(self):
        with (self.logs/self.run_id/'events.jsonl').open('a') as stream:
            stream.write(json.dumps(dict(type='stale_risk_stop', equity=-1500))+'\n')
        self.assertEqual(self.reconcile(), 2)
        self.assertEqual(json.loads(self.state.read_text()), self.prior)

    def test_failed_live_restart_preserves_original_state_and_suggests_reconcile(self):
        before = self.state.read_text()
        with patch('builtins.print') as output:
            result = run.main(['--live', '--state-file', str(self.state), '--log-dir', str(self.logs)])
        self.assertEqual(result, 2)
        self.assertEqual(self.state.read_text(), before)
        self.assertIn('--reconcile', str(output.call_args))

    def test_disconnect_preserves_baseline_and_server_reason(self):
        self.state.unlink()
        raw = self.exchange
        raw.recorder = NS(directory=self.folder/'market')
        raw.recorder.directory.mkdir()
        raw.start_recording = lambda: None
        raw.sample_market_data = lambda: None
        original_build = run.build
        def build(*args, **kwargs):
            strategy, fills = original_build(*args, **kwargs)
            def fail():
                logging.getLogger('client').error('Forced disconnect: Max requests per second exceeded.')
                raw.connected = False
                raise RuntimeError('Cannot call function until connected. Call connect() first')
            strategy.step = fail
            return strategy, fills
        # Instrument discovery happens before the forced disconnect.
        raw.instruments = {A: NS(tick_size=.1), B: NS(tick_size=.1)}
        with patch.dict(sys.modules, {'optibook.synchronous_client': self.client}), \
             patch.object(run, 'RecordingExchange', side_effect=lambda exchange, *a, **kw: exchange), \
             patch.object(run, 'build', side_effect=build), patch('builtins.print'):
            result = run.main(['--live', '--state-file', str(self.state), '--log-dir', str(self.logs)])
        self.assertEqual(result, 2)
        saved = json.loads(self.state.read_text())
        self.assertFalse(saved['safe_to_start'])
        self.assertEqual(saved['baseline_B'], 9)
        self.assertEqual(saved['owned_B'], 0)
        self.assertFalse(saved['positions_confirmed_at_finish'])
        self.assertIn('Max requests per second exceeded', saved['error'])


if __name__ == '__main__':
    unittest.main()
