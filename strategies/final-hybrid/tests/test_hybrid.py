"""Exercise real A reconciliation and B execution on an offline exchange."""
from datetime import datetime, timezone
import importlib
import json
import math
from pathlib import Path
import sys
import tempfile
from types import ModuleType
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT.parent))
PACKAGE = 'stock_market_making.strategies.final-hybrid'
run = importlib.import_module(PACKAGE + '.run')
orders = importlib.import_module(PACKAGE + '.order_execution')
market = importlib.import_module(PACKAGE + '.market')
from stock_market_making.strategies.common.simulation import ReplayExchange, SimClock
from stock_market_making.order_sides import side_name
from stock_market_making.quote_helpers import external_price_book

A, B = run.A, run.B
WALL = 1800000000


class DynamicEnum:
    """Like Cap'n Proto: equals a string but cannot be concatenated to one."""
    def __init__(self, text):
        self.text = text
    def __str__(self):
        return self.text
    def __eq__(self, other):
        return self.text == str(other)
    def __hash__(self):
        return hash(self.text)


class Exchange(ReplayExchange):
    def __init__(self, clock):
        super().__init__((A, B), clock, fill_fraction=1)
        self.resting = {A: {}, B: {}}
        self.sent = []
        self.cancel_ok = True

    def get_last_price_book(self, iid):
        raw = self.books.get(iid)
        if raw is None:
            return None
        levels = {s: dict(raw[s]) for s in ('bids', 'asks')}
        for order in self.resting[iid].values():
            side = str(order.side) + 's'
            levels[side][order.price] = levels[side].get(order.price, 0) + order.volume
        return NS(timestamp=datetime.fromtimestamp(raw['timestamp'], timezone.utc),
                  **{s: [NS(price=p, volume=v) for p, v in sorted(items.items(), reverse=s=='bids') if v]
                     for s, items in levels.items()})

    def get_outstanding_orders(self, iid):
        return dict(self.resting[iid])

    def insert_order(self, iid, *, price, volume, side, order_type):
        self.sent.append((iid, order_type, side, price, volume))
        if order_type == 'ioc':
            return super().insert_order(iid, price=price, volume=volume, side=side, order_type=order_type)
        oid = self.next_id
        self.next_id += 1
        self.resting[iid][oid] = NS(order_id=oid, side=DynamicEnum(side), price=price, volume=volume)
        self.update_times.append(self.clock.now)
        return NS(success=True, order_id=oid)

    def delete_order(self, iid, order_id):
        if not self.cancel_ok:
            return NS(success=False, error_reason='test cancellation failed')
        self.resting[iid].pop(order_id, None)
        self.update_times.append(self.clock.now)
        return NS(success=True)

    def poll_new_trades(self, iid):
        result = super().poll_new_trades(iid)
        return [NS(**dict(vars(t), side=DynamicEnum(str(t.side)))) for t in result]


class HybridTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT/'strategies/final-hybrid/config.json').read_text())
        self.config['max_order_lots'] = 2
        self.config['b_cycle']['enabled'] = False
        self.clock = SimClock()
        self.exchange = Exchange(self.clock)
        self.events = []
        self.frame(0)
        self.strategy, self.fills = run.build(self.exchange, self.config, self.event,
            clock=self.clock.monotonic, sleep=self.clock.sleep, epoch=lambda: WALL+self.clock.now,
            terminal_quantity=self.exchange.ioc_terminal_quantity)
        self.signal = dict(active=True, fair_B=101., model=dict(
            weights=(-.9, 0., 0.), origin=0., period_seconds=180.))
        self.strategy.model.observe = lambda **kwargs: dict(self.signal)

    def event(self, name, **fields):
        self.events.append((name, fields))

    def frame(self, t, a=100., b=100., depth=1000):
        self.clock.now = t
        self.exchange.advance(dict(epoch=WALL+t, books={iid: dict(timestamp=WALL+t, tick=.1,
            bids=[[price, depth]], asks=[[round(price+.2, 10), depth]]) for iid, price in ((A,a),(B,b))}))

    def enter(self):
        self.strategy.step()
        self.assertIsNotNone(self.strategy.pending)
        self.frame(.05)
        self.strategy.step()
        self.assertEqual(self.strategy.cycle_position, 2)

    def test_only_a_limits_b_ioc_and_retains_enum_orders(self):
        self.enter()
        for t in (.1, .15, .2):
            self.frame(t)
            self.strategy.step()
        self.assertTrue(any(iid == A for iid, *_ in self.exchange.sent))
        self.assertTrue(any(iid == B for iid, *_ in self.exchange.sent))
        self.assertTrue(all(kind == ('limit' if iid == A else 'ioc')
                            for iid, kind, *_ in self.exchange.sent))
        self.assertEqual(sum(iid == A for iid, *_ in self.exchange.sent), 2)
        self.assertFalse(self.exchange.get_outstanding_orders(B))

    def test_fair_exit_rechecks_new_book_and_cancels_vanished_target(self):
        self.enter()
        self.frame(1, b=101)
        self.strategy.step()
        self.assertEqual(self.strategy.cycle_position, 2)
        self.frame(1.1, b=100)
        self.strategy.step()
        self.assertIsNone(self.strategy.exit_intent)
        self.assertEqual(self.strategy.cycle_position, 2)
        self.frame(2, b=101)
        self.strategy.step()
        self.frame(2.1, b=101)
        self.strategy.step()
        self.assertEqual(self.strategy.cycle_position, 0)

    def test_entry_edge_disappears(self):
        self.strategy.step()
        self.frame(.05, b=104)
        self.strategy.step()
        self.assertEqual(self.strategy.cycle_position, 0)
        self.assertFalse(any(iid == B for iid, *_ in self.exchange.sent))

    def test_short_fair_113_8_drops_sell_when_price_falls_to_112_2(self):
        self.signal = dict(active=True, fair_B=113.8, model=dict(
            weights=(-13.7, 0., 0.), origin=0., period_seconds=180.))
        self.frame(0., b=114.5)
        # advance() intentionally ignores duplicate timestamps.
        self.frame(.01, b=114.5)
        self.strategy.step()
        self.assertEqual(self.strategy.pending['sign'], -1)
        self.frame(.1, b=112.2)
        self.strategy.step()
        self.assertIsNone(self.strategy.entry)
        self.assertEqual(self.strategy.cycle_position, 0)
        self.assertFalse(any(iid == B for iid, *_ in self.exchange.sent))

    def test_timeout_closes_even_without_valid_a(self):
        self.enter()
        self.frame(5.1)
        self.exchange.books[A] = None
        self.strategy.step()
        self.assertEqual(self.strategy.cycle_position, 0)
        self.assertFalse(self.exchange.resting[A])

    def test_b_baseline_preserved_and_account_cap_clips_entry(self):
        self.exchange.positions[B] = 99
        self.strategy.baseline_b = 99
        self.strategy.step()
        self.frame(.05)
        self.strategy.step()
        self.assertEqual(self.exchange.positions[B], 99)
        self.assertEqual(self.strategy.cycle_position, 0)

    def test_inherited_b_position_restored(self):
        self.exchange.positions[B] = -9
        self.strategy.baseline_b = -9
        self.enter()
        self.frame(5.1)
        self.strategy.step()
        self.assertEqual(self.exchange.positions[B], -9)

    def test_unexplained_b_change_rebases_to_exchange(self):
        self.exchange.positions[B] = 1
        self.strategy.step()
        self.assertEqual(self.strategy.baseline_b, 1)
        self.assertEqual(self.strategy.cycle_position, 0)
        self.assertFalse(self.strategy.executor.halted)
        self.assertTrue(any(name == 'b_runtime_rebased' for name, _ in self.events))

    def test_live_partial_settles_from_account_and_keeps_running(self):
        self.strategy.executor.terminal_quantity = None
        self.strategy.step()
        self.frame(.05)
        self.exchange.accessible[B]['asks'][100.2] = 1
        self.strategy.step()
        self.assertEqual(self.strategy.cycle_position, 1)
        self.assertFalse(self.strategy.executor.halted)
        self.assertIsNone(self.strategy.executor.pending)
        self.assertTrue(any(name == 'ioc_settled_from_account' for name, _ in self.events))

    def test_terminal_partial_accounting_and_retry_remaining_exit(self):
        self.strategy.step()
        self.frame(.05)
        self.exchange.accessible[B]['asks'][100.2] = 1
        self.strategy.step()
        self.assertEqual(self.strategy.cycle_position, 1)
        self.frame(5.1)
        self.strategy.step()
        self.assertEqual(self.strategy.cycle_position, 0)

    def test_shared_rate_budget_and_post_wait_position_guard(self):
        limiter = orders.UpdateRateLimiter(1, clock=self.clock.monotonic, sleep=self.clock.sleep)
        sender = run.execution.HybridExchange(self.exchange, limiter=limiter)
        sender.insert_order(A, price=100, volume=100, side='bid')
        original_sleep = limiter._sleep
        def fill_while_waiting(seconds):
            original_sleep(seconds)
            self.exchange.positions[B] = 100
        limiter._sleep = fill_while_waiting
        with self.assertRaises(orders.OrderLimitError):
            sender.insert_ioc(lambda actual: dict(price=100.2, volume=2, side='bid'))
        self.assertFalse(any(iid == B for iid, *_ in self.exchange.sent))
        self.assertGreaterEqual(self.clock.now, 1.)

    def test_cancel_failure_prevents_replacement(self):
        self.strategy.step()
        self.strategy.pending = None
        self.signal['active'] = False
        self.exchange.cancel_ok = False
        count = len(self.exchange.sent)
        self.frame(.3, a=102)
        with self.assertRaises(RuntimeError):
            self.strategy.step()
        self.assertEqual(len(self.exchange.sent), count)

    def test_fill_stream_normalizes_and_deduplicates(self):
        self.enter()
        trades = self.fills.poll_new_trades(B)
        self.assertEqual(trades[0].side, 'bid')
        self.exchange.private[B].extend(trades)
        self.assertEqual(self.fills.poll_new_trades(B), [])
        self.assertEqual(self.fills.confirmed_volume(B, trades[0].order_id), 2)

    def test_a_quotes_equal_original_baseline(self):
        baseline = importlib.import_module('stock_market_making.strategies.baseline-refine.run').load_quote_definitions()
        maker = importlib.import_module(PACKAGE+'.maker')
        for position in (-100,-37,0,37,100):
            book = self.exchange.get_last_price_book(A)
            self.assertEqual(maker.calculate_quote(book, position, .1),
                             baseline['calculate_quote'](book, position, .1))

    def test_capnp_enum_regression_final_manager(self):
        with self.assertRaises(TypeError):
            'reduce_' + DynamicEnum('bid')
        self.exchange.resting = {A: {}, B: {}}
        self.exchange.insert_order(A, price=100., volume=20, side='bid', order_type='limit')
        manager = orders.QuoteManager(self.exchange)
        report = manager.reconcile(A, dict(bid_price=100., ask_price=100.2,
                                           buy_volume=20, sell_volume=0))
        self.assertEqual(report['retained'], 1)
        self.assertIsNotNone(external_price_book(self.exchange.get_last_price_book(A),
                                                 self.exchange.get_outstanding_orders(A), .1))

    def test_stopping_cancels_a_and_no_b_maker(self):
        self.enter()
        self.strategy.stop()
        self.frame(.1)
        self.strategy.step()
        self.assertFalse(self.exchange.resting[A])
        self.assertEqual(self.strategy.cycle_position, 0)

    def test_invalid_side_rejected(self):
        with self.assertRaises(ValueError):
            side_name(DynamicEnum('unknown'))

    def test_interrupt_during_ioc_latches_unknown_outcome(self):
        self.strategy.step()
        self.frame(.05)
        original = self.exchange.insert_order
        def interrupted(iid, **kwargs):
            if iid == B:
                raise KeyboardInterrupt
            return original(iid, **kwargs)
        self.exchange.insert_order = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self.strategy.step()
        self.assertTrue(self.strategy.executor.halted)

    def test_real_runner_start_trade_close_and_state(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            config = dict(self.config, session_seconds=3., closeout_seconds=1., hold_seconds=.2)
            config_path = folder/'config.json'
            config_path.write_text(json.dumps(config))
            self.exchange.connect = lambda: None
            self.exchange.start_recording = lambda: None
            self.exchange.sample_market_data = lambda: None
            self.exchange.recorder = NS(directory=folder/'market')
            self.exchange.recorder.directory.mkdir()
            client = ModuleType('optibook.synchronous_client')
            client.Exchange = lambda **kwargs: self.exchange
            original_build = run.build
            def build(raw, cfg, event):
                strategy, fills = original_build(raw, cfg, event,
                    clock=self.clock.monotonic, sleep=self.clock.sleep,
                    epoch=lambda: WALL+self.clock.now)
                strategy.model.observe = lambda **kwargs: dict(self.signal)
                return strategy, fills
            def sleep(seconds):
                self.frame(self.clock.now+max(.001, seconds))
            with patch.dict(sys.modules, {'optibook.synchronous_client': client}), \
                 patch.object(run, 'RecordingExchange', side_effect=lambda raw, *a, **kw: raw), \
                 patch.object(run, 'build', side_effect=build), \
                 patch.object(run.time, 'monotonic', side_effect=self.clock.monotonic), \
                 patch.object(run.time, 'sleep', side_effect=sleep), patch('builtins.print'):
                code = run.main(['--live', '--config', str(config_path),
                    '--log-dir', str(folder/'logs'), '--state-file', str(folder/'state.json')])
            self.assertEqual(code, 0)
            saved = json.loads((folder/'state.json').read_text())
            self.assertTrue(saved['safe_to_start'])
            self.assertEqual(saved['owned_B'], 0)
            self.assertFalse(self.exchange.resting[A])
            self.assertFalse(self.exchange.connected)
            self.assertTrue(any(iid == B for iid, *_ in self.exchange.sent))
            events = [json.loads(line) for path in (folder/'logs').rglob('events.jsonl')
                      for line in path.read_text().splitlines()]
            self.assertTrue(any(row['type'] == 'fill' for row in events))
            self.assertFalse(any(row['type'] == 'fatal_error' for row in events))

    def test_fast_polling_keeps_model_learning_with_enum_a_orders(self):
        module = importlib.import_module(PACKAGE+'.model')
        model = module.CausalBasisModel(module.BasisSettings(prior_enabled=True))
        self.strategy.model = model
        self.strategy.config['entry_edge_ticks'] = 100000
        for index in range(441):
            t = index*.5
            self.frame(t, b=round(100-3*math.sin(t*2*math.pi/180), 1))
            self.strategy.step()
        self.assertEqual(len(model.rows), 221)
        self.assertGreater(model.quality['fit_samples'], 180)
        self.assertEqual(model.quality['quality_scope'], 'in_sample_fit_not_forecast_accuracy')
        self.assertFalse(any(iid == B for iid, *_ in self.exchange.sent))

    def test_repeated_books_do_not_manufacture_training_samples(self):
        module = importlib.import_module(PACKAGE+'.model')
        model = module.CausalBasisModel(module.BasisSettings(prior_enabled=True))
        for t in (0., .05, .1, 1.):
            model.observe(now=t, a_mid=100., b_mid=100., book_stamps=(WALL, WALL))
        self.assertEqual(len(model.rows), 1)

    def test_budget_wait_does_not_send_expired_a_quote(self):
        budget = self.strategy.exchange.request_budget
        while len(budget.sent) < budget.capacity-20:
            budget.call('get_positions', self.exchange.get_positions)
        quote = dict(bid_price=100., ask_price=100.2, buy_volume=1, sell_volume=1,
                     valid_until=.1)
        with self.assertRaises(market.UnusableBook):
            self.strategy.executor.reconcile(A, quote)
        self.assertFalse(self.exchange.sent)
        self.assertGreater(self.clock.now, .1)

    def test_budget_wait_rechecks_b_entry_expiry(self):
        self.strategy.step()
        self.assertIsNotNone(self.strategy.pending)
        budget = self.strategy.exchange.request_budget
        while len(budget.sent) < budget.capacity-10:
            budget.call('get_positions', self.exchange.get_positions)
        self.frame(.05)
        self.strategy.step()
        self.assertFalse(any(iid == B for iid, *_ in self.exchange.sent))
        self.assertIsNone(self.strategy.entry)
        self.assertGreater(self.clock.now, 1.)


if __name__ == '__main__':
    unittest.main()
