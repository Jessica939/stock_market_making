from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from stock_market_making.strategies.common.execution import Executor, ExecutionFault
from stock_market_making.strategies.common.runner import DEFAULTS, Feed
from stock_market_making.strategies.common.simulation import ReplayExchange, SimClock
from stock_market_making.strategies.common.market import UnusableBook
from stock_market_making.strategies.pair.policy import Policy
from stock_market_making.strategies.pair.holding import HoldingGuard, validate_holding_config
from stock_market_making.strategies.pair.session import PairSession

A, B = 'PHILIPS_A', 'PHILIPS_B'


class Journal:
    failed = False
    def __init__(self):
        self.rows = []
    def emit(self, kind, **fields):
        self.rows.append(dict(type=kind, **fields))


def fixture(fraction=1, terminal=False):
    clock, journal = SimClock(), Journal()
    config = dict(DEFAULTS, settlement_seconds=.6, relation_mode='cycle',
                  max_hold_seconds=45, pair_market_grace_seconds=3,
                  pair_valuation_grace_seconds=1, pair_recovery_seconds=1)
    exchange = ReplayExchange([A, B], clock, fill_fraction=fraction)
    exchange.advance(dict(epoch=1800000000, books={s: dict(timestamp=1800000000,
        tick=.1, bids=[[99.9, 100]], asks=[[100.1, 100]]) for s in (A, B)}))
    feed = Feed(exchange, [A, B], config, clock.monotonic, lambda:1800000000+clock.now, journal)
    executor = Executor(exchange, [A, B], feed, config, journal, clock.monotonic,
                        clock.sleep, terminal_quantity=exchange.ioc_terminal_quantity if terminal else None)
    return clock, journal, config, exchange, feed, executor


class IOCTests(unittest.TestCase):
    def test_full_fill_needs_no_extra_terminal_api(self):
        _, j, _, _, _, e = fixture()
        self.assertEqual(e.send(A, 'bid', 5), 5)
        self.assertFalse(e.unresolved)
        self.assertEqual(j.rows[-1]['state'], 'filled')

    def test_zero_and_partial_with_terminal_proof(self):
        for fraction, expected in [(0, 0), (.02, 2)]:
            with self.subTest(fraction=fraction):
                _, j, _, x, _, e = fixture(fraction, terminal=True)
                self.assertEqual(e.send(A, 'bid', 5), expected)
                self.assertEqual(x.positions[A], expected)
                self.assertFalse(e.unresolved)
                self.assertEqual(j.rows[-1]['cancelled_volume'], 5-expected)

    def test_no_terminal_proof_never_treats_stable_snapshot_as_final(self):
        for fraction in [0, .02]:
            with self.subTest(fraction=fraction):
                _, _, _, x, _, e = fixture(fraction)
                with self.assertRaisesRegex(ExecutionFault, 'unproven'):
                    e.send(A, 'bid', 5)
                sent = len(x.update_times)
                with self.assertRaises(ExecutionFault):
                    e.flatten()
                self.assertEqual(len(x.update_times), sent)
                self.assertEqual(e._pending['state'], 'unknown')

    def test_opt_in_stable_ioc_observation_accepts_partial_and_zero(self):
        for fraction, expected in [(0, 0), (.02, 2)]:
            with self.subTest(fraction=fraction):
                clock, j, _, x, _, e = fixture(fraction)
                e.config.update(allow_stable_ioc_settlement=True, ioc_stability_seconds=.4)
                self.assertEqual(e.send(A, 'bid', 5), expected)
                self.assertEqual(x.positions[A], expected)
                self.assertFalse(e.unresolved)
                self.assertGreaterEqual(clock.now, .4)
                self.assertEqual(j.rows[-1]['evidence'], 'stable_synchronous_ioc_observation')

    def test_late_private_report_is_waited_for_even_with_terminal_proof(self):
        clock, _, _, x, _, e = fixture(.02, terminal=True)
        poll = x.poll_new_trades
        with patch.object(x, 'poll_new_trades', side_effect=lambda iid: [] if clock.now < .4 else poll(iid)):
            self.assertEqual(e.send(A, 'bid', 5), 2)
        self.assertGreaterEqual(clock.now, .4)
        self.assertEqual(len(x.update_times), 1)

    def test_late_fill_after_timeout_does_not_clear_fault(self):
        clock, _, _, x, _, e = fixture()
        poll = x.poll_new_trades
        with patch.object(x, 'poll_new_trades', side_effect=lambda iid: [] if clock.now < 1 else poll(iid)):
            with self.assertRaises(ExecutionFault):
                e.send(A, 'bid', 5)
            clock.now = 1
            e.audit('late_report')
            self.assertTrue(e.hard_fault)
            with self.assertRaises(ExecutionFault):
                e.send(B, 'ask', 5)
        self.assertEqual(len(x.update_times), 1)

    def test_false_terminal_evidence_and_lost_ack_fail_closed(self):
        _, _, _, x, _, e = fixture()
        e.terminal_quantity = lambda iid, oid: 0
        with self.assertRaisesRegex(ExecutionFault, 'contradicts'):
            e.send(A, 'bid', 5)
        _, _, _, x, _, e = fixture()
        with patch.object(x, 'insert_order', side_effect=TimeoutError('lost ack')):
            with self.assertRaisesRegex(ExecutionFault, 'unknown insertion'):
                e.send(A, 'bid', 5)
        self.assertTrue(e.unresolved)

    def test_second_leg_zero_or_partial_compensates_only_known_difference(self):
        for capacity, expected in [(0, 0), (2, 2)]:
            with self.subTest(capacity=capacity):
                _, _, _, x, feed, e = fixture(terminal=True)
                x.accessible[B]['bids'][99.9] = capacity
                books = feed.frame()['books']
                after = e.apply({A:5, B:-5}, 'pair', books)
                self.assertEqual(after, {A:expected, B:-expected})
                self.assertFalse(e.unresolved)

    def test_exit_depth_caps_entry_before_first_leg(self):
        _, j, _, x, feed, e = fixture(terminal=True)
        e.config['pair_exit_liquidity_fraction'] = .35
        x.books[A]['bids'][0][1] = 4
        after = e.apply({A:5, B:-5}, 'pair', feed.frame()['books'])
        self.assertEqual(after, {A:1, B:-1})
        self.assertTrue(any(r['type']=='pair_exit_capacity' and r['admitted']==1 for r in j.rows))


class HoldingTests(unittest.TestCase):
    def setUp(self):
        self.clock, self.journal, self.config, self.x, self.feed, self.e = fixture(terminal=True)
        self.guard = HoldingGuard(self.config)
        self.held = dict(size=5, direction=1, opened_at=0, exit_at=45)

    def evaluate(self, now, **kwargs):
        params = dict(positions={A:5, B:-5}, held=self.held, reason='spread_too_wide',
                      equity=0, baseline=0, peak=0)
        params.update(kwargs)
        return self.guard.evaluate(now, **params)

    def test_short_pause_needs_stable_recovery(self):
        self.assertEqual(self.evaluate(0), 'wait')
        self.assertEqual(self.evaluate(.5, reason=None), 'wait')
        self.assertEqual(self.evaluate(1.5, reason=None), 'resume')

    def test_blinking_valid_frames_cannot_restart_total_grace(self):
        self.evaluate(0)
        self.evaluate(.5, reason=None)
        self.evaluate(1)
        self.evaluate(2, reason=None)
        self.assertEqual(self.evaluate(3), 'market_timeout')

    def test_valuation_timeout_has_shorter_bound(self):
        self.assertEqual(self.evaluate(0, equity=None), 'wait')
        self.assertEqual(self.evaluate(1, equity=None), 'valuation_timeout')

    def test_loss_mismatch_deadline_and_session_stop_never_wait(self):
        self.assertEqual(self.evaluate(0, equity=-51), 'loss_limit')
        self.assertEqual(self.evaluate(0, positions={A:5,B:0}), 'unmatched_or_untracked_inventory')
        self.assertEqual(self.evaluate(45), 'holding_deadline')
        self.assertEqual(self.evaluate(0, stopping=True), 'risk_or_session_stop')

    def session(self):
        policy = Policy(self.config)
        policy.active = dict(self.held, filled=True)
        session = PairSession(policy, 'pair', self.x, self.feed, self.config, self.journal,
                              self.clock.monotonic, self.clock.sleep,
                              terminal_quantity=self.x.ioc_terminal_quantity)
        session.initialized = session.startup_cancelled = True
        session.baseline = session.peak = 0
        self.x.positions = {A:5,B:-5}
        # Simulated established inventory with cost 1.0 at current liquidation prices.
        self.x.cash = {A:-500.5,B:499.5}
        session.executor._settled_positions = self.x.positions.copy()
        return session

    def test_session_keeps_pair_during_short_signal_gap_then_latches_exit(self):
        s = self.session()
        with patch.object(self.feed, 'frame', side_effect=UnusableBook('spread_too_wide')):
            s.step()
            self.assertEqual(self.x.positions, {A:5,B:-5})
            self.assertEqual(len(self.x.update_times), 0)
            self.clock.now = 3
            for book in self.x.books.values():
                book['timestamp'] = 1800000003
            s.step()
        self.assertEqual(self.x.positions, {A:0,B:0})
        self.assertEqual(s.pair_exit_reason, 'market_timeout')
        self.assertTrue(any(r['type']=='pair_exit_requested' for r in self.journal.rows))

    def test_stale_exit_is_latched_until_executable_books_return(self):
        s = self.session()
        with patch.object(self.feed, 'frame', side_effect=UnusableBook('stale book')):
            s.step()
            self.clock.now = 3
            s.step()
        self.assertEqual(self.x.positions, {A:5,B:-5})
        self.assertIsNotNone(s.pair_exit_reason)
        for book in self.x.books.values():
            book['timestamp'] = 1800000003
        s.step()
        self.assertEqual(self.x.positions, {A:0,B:0})

    def test_session_values_loss_even_when_entry_feed_is_blocked(self):
        s = self.session()
        with patch.object(self.feed, 'frame', side_effect=UnusableBook('jump cooldown')):
            with patch.object(self.feed, 'liquidation_equity', return_value=-55):
                s.step()
        self.assertEqual(self.x.positions, {A:0,B:0})
        self.assertTrue(s.risk_halt)

    def test_invalid_grace_config_rejected(self):
        for change in [dict(pair_market_grace_seconds=float('nan')),
                       dict(pair_valuation_grace_seconds=5), dict(pair_exit_liquidity_fraction=0)]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_holding_config(dict(self.config, **change))

    def test_lag_observation_expires_and_favorable_overshoot_does_not_stop(self):
        s = self.session()
        s.policy.c.update(cycle_path_grace_seconds=5)
        s.policy.active.update(tick=.1, sigma=.1, entry_basis=0, entry_spread=.2,
                               initial_roundtrip_cost=1, cycle_weights=(0, 3, 0),
                               cycle_origin=0, cycle_period=180)
        frame=self.feed.holding_frame()
        frame['now']=10
        self.assertEqual(s.policy.monitor_cycle_position(frame,{A:5,B:-5})['reason'], 'observe_cycle_path_lag')
        frame['now']=15
        self.assertEqual(s.policy.monitor_cycle_position(frame,{A:5,B:-5})['reason'], 'cycle_path_lag_timeout')
        s.policy.closing_reason=None
        s.policy.active.pop('path_lag_started_at',None)
        frame['now']=1
        frame['books'][A].update(bid=102.9,ask=103.1,bids=[[102.9,100]],asks=[[103.1,100]])
        self.assertEqual(s.policy.monitor_cycle_position(frame,{A:5,B:-5})['reason'], 'hold_frozen_cycle_pair')

    def test_extension_is_once_and_bounded_from_original_entry(self):
        s=self.session()
        s.policy.c.update(max_hold_seconds=60,cycle_extension_seconds=15,cycle_path_grace_seconds=5)
        s.policy.active.update(tick=.1,sigma=2,entry_basis=0,entry_spread=.2,
                               initial_roundtrip_cost=1,cycle_weights=(0,0,-3),
                               cycle_origin=0,cycle_period=180)
        frame=self.feed.holding_frame()
        frame['books'][A].update(bid=102.9,ask=103.1,bids=[[102.9,100]],asks=[[103.1,100]])
        frame['now']=45
        result=s.policy.monitor_cycle_position(frame,{A:5,B:-5})
        self.assertTrue(result['diagnostics']['extension_granted'])
        self.assertEqual(s.policy.active['exit_at'],60)
        frame['now']=60
        self.assertEqual(s.policy.monitor_cycle_position(frame,{A:5,B:-5})['reason'],'cycle_horizon_exit')

    def test_independent_holding_survives_entry_spread_filter(self):
        s = self.session()
        s.config['pair_independent_holding'] = True
        s.policy.c['pair_independent_holding'] = True
        s.policy.active.update(tick=.1, sigma=1, entry_basis=0, entry_spread=.2,
                               initial_roundtrip_cost=1, cycle_weights=(0, 3, 0),
                               cycle_origin=0, cycle_period=180)
        for book in self.x.books.values():
            book['bids'] = [[99.5,100]]
            book['asks'] = [[100.5,100]]
        for symbol in (A, B):
            self.x.accessible[symbol] = {'bids': {99.5:100}, 'asks': {100.5:100}}
        for t in (0, 1, 2, 3, 4):
            self.clock.now = t
            for book in self.x.books.values():
                book['timestamp'] = 1800000000+t
            with patch.object(self.feed, 'frame', side_effect=UnusableBook('spread_too_wide')):
                s.step()
        self.assertEqual(self.x.positions, {A:5,B:-5})
        self.assertFalse(self.x.update_times)
        self.assertTrue(any(r['type']=='pair_holding_decision' for r in self.journal.rows))
        self.clock.now = 45
        for book in self.x.books.values():
            book['timestamp'] = 1800000045
        with patch.object(self.feed, 'frame', side_effect=UnusableBook('spread_too_wide')):
            s.step()
        self.assertEqual(self.x.positions, {A:0,B:0})

    def test_frozen_model_still_exits_on_adverse_basis(self):
        s = self.session()
        s.policy.active.update(tick=.1, sigma=.1, entry_basis=0, entry_spread=.2,
                               initial_roundtrip_cost=1, cycle_weights=(0, 3, 0),
                               cycle_origin=0, cycle_period=180)
        frame = self.feed.holding_frame()
        frame['books'][A].update(bid=98.9, ask=99.1, bids=[[98.9,100]], asks=[[99.1,100]])
        result = s.policy.monitor_cycle_position(frame, {A:5,B:-5})
        self.assertEqual(result['reason'], 'cycle_adverse_move_stop')
        self.assertEqual(result['targets'], {A:0,B:0})
        frame = self.feed.holding_frame()
        result = s.policy.monitor_cycle_position(frame, {A:5,B:-5})
        self.assertEqual(result['reason'], 'cycle_adverse_move_stop')
        self.assertEqual(result['targets'], {A:0,B:0})


if __name__ == '__main__':
    unittest.main()
