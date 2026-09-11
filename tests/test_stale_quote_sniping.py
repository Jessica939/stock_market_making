import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from stock_market_making.strategies.stale_quote_sniping.engine import Engine, SYMBOLS, validate
from stock_market_making.strategies.stale_quote_sniping.model import CausalBasisModel
from stock_market_making.strategies.common.execution import ExecutionFault
from stock_market_making.strategies.common.simulation import ReplayExchange, SimClock


class Journal:
    failed = False

    def __init__(self):
        self.events = []

    def emit(self, kind, **fields):
        self.events.append((kind, fields))


class StaleQuoteTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "strategies/stale_quote_sniping/config.json").read_text())
        self.clock = SimClock()
        self.exchange = ReplayExchange(SYMBOLS, self.clock, fill_fraction=1)
        self.journal = Journal()
        self.frame(0)
        self.engine = Engine(self.exchange, self.config, self.journal,
                             self.clock.monotonic, self.clock.sleep,
                             lambda: 1800000000 + self.clock.now,
                             terminal_quantity=self.exchange.ioc_terminal_quantity)
        self.engine.startup()
        # predicted A-B basis=-0.9, so A mid=100.1 implies B FV=101.0.
        self.signal = dict(active=True, reason="active", predicted_basis=-0.9,
                           fair_B=101.0, residual=0.9,
                           model=dict(weights=(-0.9, 0.0, 0.0), origin=0.0,
                                      period_seconds=180.0))
        self.engine.model = NS(observe=lambda **kwargs: dict(self.signal))

    def frame(self, t, a_bid=100.0, b_bid=100.0, missing_b=False, depth=1000):
        self.clock.now = t
        books = {
            "PHILIPS_A": dict(timestamp=1800000000 + t, tick=0.1,
                              bids=[[a_bid, depth]], asks=[[a_bid + 0.2, depth]]),
            "PHILIPS_B": dict(timestamp=1800000000 + t, tick=0.1,
                              bids=[[b_bid, depth]], asks=[[b_bid + 0.2, depth]]),
        }
        if missing_b:
            books["PHILIPS_B"] = None
        self.exchange.advance(dict(epoch=1800000000 + t, books=books))

    def enter(self):
        self.engine.step()
        self.assertIsNotNone(self.engine.pending)
        self.frame(0.5)
        self.engine.step()
        self.assertEqual(self.exchange.positions["PHILIPS_B"], 2)

    def test_only_b_and_fair_reached_exit(self):
        self.enter()
        self.frame(1.0, b_bid=101.0)
        self.engine.step()
        self.assertEqual(self.exchange.positions["PHILIPS_B"], 2)
        self.frame(1.5, b_bid=101.0)
        self.engine.step()
        self.assertEqual(self.exchange.positions, dict.fromkeys(SYMBOLS, 0))
        self.assertTrue(all(fill.instrument_id == "PHILIPS_B" for fill in self.exchange.fills))
        self.assertAlmostEqual(self.exchange.cash["PHILIPS_B"], 1.6)

    def test_latency_recheck_drops_disappeared_edge(self):
        self.engine.step()
        self.frame(0.5, b_bid=107.0)
        self.engine.step()
        self.assertFalse(self.exchange.fills)
        self.assertIsNone(self.engine.pending)

    def test_hold_timeout_is_delayed_then_exits(self):
        self.enter()
        self.frame(5.5)
        self.engine.step()
        self.assertEqual(self.engine.exit_reason, "hold_timeout")
        self.assertEqual(self.exchange.positions["PHILIPS_B"], 2)
        self.frame(6.0)
        self.engine.step()
        self.assertEqual(self.exchange.positions["PHILIPS_B"], 0)

    def test_missing_exit_book_keeps_confirmed_inventory(self):
        self.enter()
        self.frame(5.5, missing_b=True)
        self.engine.step()
        self.assertEqual(self.exchange.positions["PHILIPS_B"], 2)
        self.frame(6.0)
        self.engine.step()
        self.assertEqual(self.engine.exit_reason, "hold_timeout")
        self.frame(6.5)
        self.engine.step()
        self.assertEqual(self.exchange.positions["PHILIPS_B"], 0)

    def test_unexplained_b_change_after_baseline_is_rejected(self):
        self.exchange.positions["PHILIPS_B"] = 1
        with self.assertRaises(ExecutionFault):
            self.engine.step()

    def test_inherited_a_and_b_are_preserved_as_baseline(self):
        clock = SimClock()
        exchange = ReplayExchange(SYMBOLS, clock, fill_fraction=1)
        exchange.positions.update(PHILIPS_A=-18, PHILIPS_B=9)
        exchange.advance(dict(epoch=1800000000, books={
            iid: dict(timestamp=1800000000, tick=0.1,
                      bids=[[100.0, 1000]], asks=[[100.2, 1000]])
            for iid in SYMBOLS
        }))
        journal = Journal()
        engine = Engine(exchange, self.config, journal, clock.monotonic, clock.sleep,
                        lambda: 1800000000 + clock.now,
                        terminal_quantity=exchange.ioc_terminal_quantity)
        engine.startup()
        engine.model = NS(observe=lambda **kwargs: dict(self.signal))
        engine.step()
        clock.now = 0.5
        exchange.advance(dict(epoch=1800000000.5, books={
            iid: dict(timestamp=1800000000.5, tick=0.1,
                      bids=[[100.0, 1000]], asks=[[100.2, 1000]])
            for iid in SYMBOLS
        }))
        engine.step()
        self.assertEqual(exchange.positions, {"PHILIPS_A": -18, "PHILIPS_B": 11})
        self.assertEqual(engine.executor.positions()["PHILIPS_B"], 2)
        clock.now = 1.0
        exchange.advance(dict(epoch=1800000001, books={
            "PHILIPS_A": dict(timestamp=1800000001, tick=0.1,
                              bids=[[100.0, 1000]], asks=[[100.2, 1000]]),
            "PHILIPS_B": dict(timestamp=1800000001, tick=0.1,
                              bids=[[101.0, 1000]], asks=[[101.2, 1000]]),
        }))
        engine.step()
        clock.now = 1.5
        exchange.advance(dict(epoch=1800000001.5, books={
            "PHILIPS_A": dict(timestamp=1800000001.5, tick=0.1,
                              bids=[[100.0, 1000]], asks=[[100.2, 1000]]),
            "PHILIPS_B": dict(timestamp=1800000001.5, tick=0.1,
                              bids=[[101.0, 1000]], asks=[[101.2, 1000]]),
        }))
        engine.step()
        self.assertEqual(exchange.positions, {"PHILIPS_A": -18, "PHILIPS_B": 9})
        summary = engine.finish()
        self.assertTrue(summary["flat"])
        self.assertEqual(summary["baseline_B"], 9)

    def test_replay_specification_is_guarded(self):
        for key, value in (("order_lots", 21), ("hold_seconds", 0.0),
                           ("entry_edge_ticks", 0.0), ("fee_per_lot", float("nan"))):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate(dict(self.config, **{key: value}))


class CausalityTests(unittest.TestCase):
    def test_current_observation_is_not_used_by_its_own_signal(self):
        model = CausalBasisModel()
        for second in range(181):
            signal = model.observe(now=float(second), a_mid=101.0, b_mid=100.0,
                                   book_stamps=(float(second), float(second)))
        self.assertFalse(signal["active"])
        before = tuple(model.weights)
        shock = model.observe(now=181.0, a_mid=101.0, b_mid=50.0,
                              book_stamps=(181.0, 181.0))
        self.assertTrue(shock["active"])
        self.assertEqual(shock["model"]["weights"], before)
        self.assertAlmostEqual(shock["predicted_basis"], 1.0)
        self.assertAlmostEqual(shock["fair_B"], 100.0)


if __name__ == "__main__":
    unittest.main()
