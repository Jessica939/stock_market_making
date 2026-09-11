"""Hybrid inventory, actual sender limits, order routing, and private fills."""
from datetime import datetime, timezone
import importlib
import json
import logging
from dataclasses import asdict
from pathlib import Path
import random
import sys
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from test_position import A, B, PACKAGE, Exchange, book, load, execution

hybrid = importlib.import_module(PACKAGE + '.hybrid_position')
FillStream = importlib.import_module(PACKAGE + '.fill_stream').FillStream
cycle = importlib.import_module(PACKAGE + '.cycle_signal')


class HybridTests(unittest.TestCase):
    def setUp(self):
        self.strategy = load()
        self.controller = hybrid.HybridPosition(
            market_making=self.strategy['B_MM_SETTINGS'], **self.strategy['B_POSITION_SETTINGS'])
        self.signal = dict(active=False, reason='warmup')

    def quote(self, now=0, position=0, mid=100, spread=.4):
        return self.strategy['plan_quote'](book(mid, spread), position, .1, B,
                                           self.signal, self.controller, now)

    def start_cycle(self, sign=1):
        self.signal = dict(active=True, predicted_B_change=3.*sign, fit_weight=1.)
        return self.quote()

    def test_idle_mm_and_filled_inventory_share_twenty_share_band(self):
        q = self.quote()
        self.assertEqual((q['buy_volume'], q['sell_volume']), (20, 20))
        self.assertEqual((q['min_position'], q['max_position']), (-20, 20))
        q = self.quote(1, 20)
        self.assertEqual((q['buy_volume'], q['sell_volume']), (0, 20))
        self.assertFalse(q['reduce_only'])
        self.assertNotIn('target_position', q)
        self.assertEqual(q['execution_policy']['ask'], 'maker_limit')

    def test_cycle_full_entry_then_mm_recycles_up_to_twenty_shares(self):
        for sign in (1, -1):
            with self.subTest(sign=sign):
                self.setUp()
                q = self.start_cycle(sign)
                entry_key, close_key = ('buy_volume', 'sell_volume') if sign > 0 else ('sell_volume', 'buy_volume')
                self.assertEqual((q[entry_key], q[close_key]), (100, 0))
                q = self.quote(1, sign*100)
                self.assertEqual((q[entry_key], q[close_key]), (0, 20))
                self.assertEqual((q['min_position'], q['max_position']),
                                 (80, 100) if sign > 0 else (-100, -80))
                q = self.quote(12, sign*90)
                self.assertEqual((q[entry_key], q[close_key]), (10, 10))
                self.assertEqual(q['cycle_position']['reason'], 'hold_cycle_position')

    def test_partial_build_does_not_expand_after_entry_window(self):
        self.start_cycle()
        self.quote(1, 37)
        q = self.quote(11, 37)
        self.assertEqual((q['min_position'], q['max_position']), (17, 37))
        self.assertEqual((q['buy_volume'], q['sell_volume']), (0, 20))
        q = self.quote(12, 20)
        self.assertEqual((q['buy_volume'], q['sell_volume']), (17, 3))

    def test_take_profit_suspends_mm_and_keeps_full_exit_latched(self):
        self.start_cycle()
        self.quote(1, 100)
        q = self.quote(2, 90, 103)
        self.assertEqual((q['buy_volume'], q['sell_volume']), (0, 90))
        self.assertEqual(q['cycle_position']['reason'], 'cycle_take_profit')
        self.assertFalse(q['market_making']['active'])
        self.assertEqual(q['execution_policy'], {'ask': 'passive_exit_limit'})
        q = self.quote(4, 37, 100)
        self.assertEqual(q['cycle_position']['exit_started'], 2)
        self.assertEqual(q['sell_volume'], 37)
        self.assertEqual(q['execution_policy'], {'ask': 'aggressive_exit_limit'})
        self.assertLess(q['ask_price'], book().bids[0].price)
        self.assertEqual(q['order_type'], 'limit')
        q = self.quote(5, 0)
        self.assertEqual((q['buy_volume'], q['sell_volume']), (0, 0))

    def test_stop_still_confirms_three_seconds_and_exits_every_share(self):
        self.start_cycle()
        self.quote(1, 100)
        for now in (2, 2.5, 3, 3.5, 4, 4.5):
            q = self.quote(now, 95, 93)
            self.assertFalse(q['reduce_only'])
        q = self.quote(5, 95, 93)
        self.assertEqual(q['cycle_position']['reason'], 'cycle_position_stop')
        self.assertEqual((q['buy_volume'], q['sell_volume']), (0, 95))

    def test_existing_mm_same_direction_is_adopted_without_a_fake_position(self):
        self.quote()
        self.signal = dict(active=True, predicted_B_change=3., fit_weight=1.)
        q = self.quote(1, 12)
        self.assertEqual(q['buy_volume'], 88)
        self.assertTrue(self.controller.active['filled'])
        self.assertEqual(q['cycle_position']['reason'], 'build_cycle_position')

    def test_existing_mm_opposite_direction_closes_before_cycle_entry(self):
        self.quote()
        self.signal = dict(active=True, predicted_B_change=-3., fit_weight=1.)
        q = self.quote(1, 12)
        self.assertEqual(q['cycle_position']['reason'], 'cycle_rebalance')
        self.assertEqual((q['buy_volume'], q['sell_volume']), (0, 12))
        self.assertTrue(q['reduce_only'])

    def test_restart_inventory_is_not_relabelled_as_mm(self):
        q = self.quote(0, 15)
        self.assertEqual(q['cycle_position']['reason'], 'untracked_inventory')
        self.assertFalse(q['market_making']['active'])
        q = self.quote(2, 5)
        self.assertEqual(q['execution_policy'], {'ask': 'aggressive_exit_limit'})
        self.assertEqual(q['sell_volume'], 5)

    def test_model_failure_and_sign_reversal_do_not_rebuild_cycle(self):
        for signal in (dict(active=False, reason='residual_shock'),
                       dict(active=True, predicted_B_change=-3., fit_weight=1.)):
            self.setUp()
            self.start_cycle()
            self.quote(1, 100)
            self.signal = signal
            q = self.quote(12, 90)
            self.assertEqual(q['buy_volume'], 0)
            self.assertEqual(q['sell_volume'], 10)

    def test_invalid_pair_shock_or_wide_spread_blocks_idle_increase(self):
        for reason in ('invalid_pair_books', 'unsynchronized_books', 'residual_shock'):
            self.setUp()
            self.quote()
            self.signal = dict(active=False, reason=reason)
            q = self.quote(1, 10)
            self.assertEqual((q['buy_volume'], q['sell_volume']), (0, 10))
        self.setUp()
        q = self.quote(spread=3)
        self.assertEqual((q['buy_volume'], q['sell_volume']), (0, 0))

    def test_passive_mm_and_cycle_mm_combination_never_self_cross(self):
        for spread in (.1, .2, .3, 1):
            for sign in (-1, 0, 1):
                self.setUp()
                if sign:
                    self.start_cycle(sign)
                for p in (0, 10, 20):
                    q = self.quote(1, p*sign, mid=100.05 if spread == .1 else 100, spread=spread)
                    if q['buy_volume'] and q['sell_volume']:
                        self.assertLess(q['bid_price'], q['ask_price'])
                    for side, key in (('bid', 'buy_volume'), ('ask', 'sell_volume')):
                        if q[key] and q['execution_policy'][side] == 'maker_limit':
                            b = book(100.05 if spread == .1 else 100, spread)
                            if side == 'bid':
                                self.assertLess(q['bid_price'], b.asks[0].price)
                            else:
                                self.assertGreater(q['ask_price'], b.bids[0].price)

    def test_fresh_position_rechecks_shared_band_not_just_hard_limit(self):
        q = self.quote()
        x = Exchange({A: 0, B: 18})  # Quote was planned before an 18-share fill.
        execution.QuoteManager(x).reconcile(B, q)
        bids = sum(o.volume for o in x.orders[B].values() if o.side == 'bid')
        self.assertEqual(bids, 2)

    def test_repricing_cancels_all_old_sides_before_new_cycle_quote(self):
        x = Exchange()
        manager = execution.QuoteManager(x)
        manager.reconcile(B, self.quote())
        old_ids = set(x.orders[B])
        q = self.start_cycle()
        manager.reconcile(B, q)
        self.assertTrue(old_ids.isdisjoint(x.orders[B]))
        self.assertEqual(sum(o.volume for o in x.orders[B].values()), 100)

    def test_cancel_failure_never_adds_cycle_order(self):
        x = Exchange()
        manager = execution.QuoteManager(x)
        manager.reconcile(B, self.quote())
        before = x.next_id
        x.delete_order = Mock(return_value=NS(success=False, error_reason='rejected'))
        with self.assertRaises(RuntimeError):
            manager.reconcile(B, self.start_cycle())
        self.assertEqual(x.next_id, before)

    def test_late_fill_during_cancellation_reduces_new_cycle_capacity(self):
        x = Exchange()
        manager = execution.QuoteManager(x)
        manager.reconcile(B, self.quote())
        cancel = x.delete_order
        def fill_then_cancel(iid, *, order_id):
            if x.orders[iid][order_id].side == 'bid':
                x.fill(iid, order_id, x.orders[iid][order_id].volume)
                return NS(success=False, error_reason='Could not find order id to delete')
            return cancel(iid, order_id=order_id)
        x.delete_order = fill_then_cancel
        manager.reconcile(B, self.start_cycle())
        self.assertEqual(x.positions[B], 20)
        self.assertEqual(sum(o.volume for o in x.orders[B].values()), 80)

    def test_seeded_random_fills_never_breach_either_instrument_limit(self):
        rng = random.Random(43112)
        x = Exchange()
        limiter = execution.UpdateRateLimiter(max_updates=100000)
        guarded = execution.LimitedExchange(x, limiter=limiter)
        manager = execution.QuoteManager(guarded)
        for step in range(1000):
            for iid in (A, B):
                if x.orders[iid] and rng.random() < .8:
                    oid = rng.choice(list(x.orders[iid]))
                    x.fill(iid, oid, rng.randint(1, x.orders[iid][oid].volume))
            self.signal = (dict(active=False, reason='warmup') if step % 97 < 25 else
                           dict(active=True, predicted_B_change=3.*(1 if step % 194 < 97 else -1), fit_weight=1.))
            manager.reconcile(B, self.quote(step*.5, x.positions[B], mid=100+(step//31)%4))
            manager.reconcile(A, self.strategy['calculate_quote'](book(), x.positions[A], .1))
            for iid in (A, B):
                bid = sum(o.volume for o in x.orders[iid].values() if o.side == 'bid')
                ask = sum(o.volume for o in x.orders[iid].values() if o.side == 'ask')
                self.assertLessEqual(x.positions[iid]+bid, 100)
                self.assertGreaterEqual(x.positions[iid]-ask, -100)
                self.assertLessEqual(bid+ask, 200)

    def test_configuration_cannot_raise_exchange_hard_cap(self):
        for cls in (execution.QuoteManager, execution.LimitedExchange):
            with self.assertRaises(ValueError):
                cls(None, position_limit=101)
        for value in (0, -1, True, 1.5, 101):
            with self.assertRaises(ValueError):
                hybrid.MarketMakingSettings(order_volume=value)

    def test_invalid_bounds_or_unimplemented_order_type_fail_before_sending(self):
        q = self.quote()
        for changes in (dict(min_position=21, max_position=20), dict(max_position=101),
                        dict(min_position=False), dict(target_position=0), dict(order_type='ioc')):
            x = Exchange()
            with self.assertRaises(ValueError):
                execution.QuoteManager(x).reconcile(B, dict(q, **changes))
            self.assertFalse(x.orders[B])


class FillStreamTests(unittest.TestCase):
    def trade(self, qty=7, trade_id=1):
        return NS(order_id=12, trade_id=trade_id, side='bid', price=100., volume=qty,
                  timestamp=datetime(2026, 9, 11, tzinfo=timezone.utc))

    def test_execution_refresh_does_not_steal_trades_from_logger(self):
        trade = self.trade()
        raw = Mock()
        raw.poll_new_trades.side_effect = [[trade], [], [], [self.trade(3, 2)], []]
        stream = FillStream(raw)
        self.assertEqual(stream.refresh(B), 7)
        self.assertEqual(stream.confirmed_volume(B, 12), 7)
        self.assertEqual(stream.poll_new_trades(B), [trade])
        self.assertEqual(stream.poll_new_trades(B), [])
        self.assertEqual(stream.refresh(B), 3)
        self.assertEqual(stream.confirmed_volume(B, 12), 10)
        self.assertEqual(len(stream.poll_new_trades(B)), 1)

    def test_empty_poll_does_not_erase_partial_fill_and_duplicates_are_deduplicated(self):
        raw = Mock()
        raw.poll_new_trades.side_effect = [[self.trade()], [], [self.trade()], [self.trade(3, 2)]]
        stream = FillStream(raw)
        for expected in (7, 7, 7, 10):
            stream.refresh(B)
            self.assertEqual(stream.confirmed_volume(B, 12), expected)

    def test_mutated_trade_id_fails_closed(self):
        raw = Mock()
        raw.poll_new_trades.side_effect = [[self.trade()], [self.trade(8)]]
        stream = FillStream(raw)
        stream.refresh(B)
        with self.assertRaises(RuntimeError):
            stream.refresh(B)

    def test_actual_sender_records_partial_fill_without_claiming_full_execution(self):
        x = Exchange()
        pending = []
        insert = x.insert_order
        def partial(iid, **kwargs):
            response = insert(iid, **kwargs)
            x.fill(iid, response.order_id, 7)
            t = self.trade()
            t.order_id = response.order_id
            pending.append(t)
            return response
        def poll(iid):
            result = list(pending)
            pending.clear()
            return result
        x.insert_order, x.poll_new_trades = partial, poll
        stream = FillStream(x)
        manager = execution.QuoteManager(stream, fill_stream=stream)
        q = dict(bid_price=99.9, ask_price=100.1, buy_volume=20, sell_volume=0)
        result = manager.reconcile(B, q)
        report = result['execution_reports'][0]
        self.assertEqual(report['confirmed_fill_volume'], 7)
        self.assertEqual(report['not_yet_confirmed_volume'], 13)
        self.assertEqual(len(stream.poll_new_trades(B)), 1)


class HybridRuntimeTests(unittest.TestCase):
    def test_real_loop_uses_hybrid_quotes_and_shared_fill_stream(self):
        strategy = load()
        clock = NS(now=0.)
        x = Exchange()
        x.connect, x.disconnect = Mock(), Mock()
        x.is_connected = lambda: clock.now < 2
        x.get_tradable_instruments = lambda: {iid: NS(tick_size=.1) for iid in (A, B)}
        x.poll_new_trades = Mock(return_value=[])
        submitted = []
        insert = x.insert_order
        def record_insert(iid, **kwargs):
            submitted.append((iid, kwargs))
            return insert(iid, **kwargs)
        x.insert_order = record_insert
        def market(iid):
            b = book(spread=.4)
            for side, levels in (('bid', b.bids), ('ask', b.asks)):
                levels.extend(NS(price=o.price, volume=o.volume) for o in x.orders[iid].values() if o.side == side)
                levels.sort(key=lambda level: level.price, reverse=side == 'bid')
            b.timestamp = datetime.fromtimestamp(1_800_000_000+clock.now, timezone.utc)
            return b
        x.get_last_price_book = market
        client = ModuleType('optibook.synchronous_client')
        client.Exchange = Mock(return_value=x)
        recorder = Mock(path=Path('/unused/events.jsonl'))
        storage = Mock(directory=Path('/unused/run'), path=Path('/unused/manifest.json'))
        model = Mock(last_fit=None, needs_bootstrap=False)
        model.observe.return_value = dict(active=False, reason='warmup')
        def sleep(seconds):
            clock.now += seconds
        namespace = dict(strategy, logging=logging, asdict=asdict,
            time=NS(monotonic=lambda: 1000+clock.now, time=lambda: 1_800_000_000+clock.now, sleep=sleep),
            RunStorage=Mock(return_value=storage), StrategyRecorder=Mock(return_value=recorder),
            PhilipsPriceRecorder=Mock(return_value=Mock(exchange=x, directory=Path('/unused/market'))),
            LimitedExchange=execution.LimitedExchange, RecordedExchange=lambda exchange, rec: exchange,
            QuoteManager=execution.QuoteManager, CycleSignal=Mock(return_value=model),
            bootstrap_cycle=Mock(return_value=dict(loaded=False)), usable_book=cycle.usable_book,
            external_price_book=importlib.import_module('stock_market_making.quote_helpers').external_price_book)
        path = Path(__file__).resolve().parents[1]/'strategy_with_logging.ipynb'
        n = json.loads(path.read_text())
        source = next(''.join(c['source']) for c in n['cells'] if ''.join(c['source']).startswith('def main():'))
        exec(compile(source, str(path), 'exec'), namespace)
        with patch.dict(sys.modules, {'optibook': ModuleType('optibook'), 'optibook.synchronous_client': client}), patch('builtins.print'):
            namespace['main']()
        self.assertFalse(any(c.args[0] == 'cycle_error' for c in recorder.event.call_args_list))
        self.assertEqual({iid for iid, _ in submitted}, {A, B})
        self.assertEqual([q['volume'] for iid, q in submitted if iid == B], [20, 20])
        self.assertTrue(all(q['order_type'] == 'limit' for _, q in submitted))
        self.assertTrue(x.poll_new_trades.called)
        x.connect.assert_called_once()
        x.disconnect.assert_called_once()


if __name__ == '__main__':
    unittest.main()
