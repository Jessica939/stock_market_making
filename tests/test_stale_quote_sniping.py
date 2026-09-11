import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from stock_market_making.strategies.stale_quote_sniping.engine import (
    Engine, SYMBOLS, scaled_entry_size, sized_execution, validate,
)
from stock_market_making.strategies.stale_quote_sniping.model import BasisSettings, CausalBasisModel
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
        # Behavioral tests use the original small deterministic size. Aggressive
        # live defaults are asserted separately and should not weaken invariants.
        self.config["order_lots"] = 2
        self.config["max_order_lots"] = 2
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

    def test_exit_waits_for_new_book_but_not_entry_confirmation_delay(self):
        self.enter()
        self.frame(1.0, b_bid=101.0)
        self.engine.step()
        self.assertEqual(self.exchange.positions["PHILIPS_B"], 2)
        self.frame(1.1, b_bid=101.0)
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
        for key, value in (("order_lots", 51), ("max_order_lots", 51),
                           ("hold_seconds", 0.0),
                           ("loop_seconds", 0.049),
                           ("entry_edge_ticks", 0.0), ("exit_confirmation_seconds", -0.1),
                           ("fee_per_lot", float("nan"))):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate(dict(self.config, **{key: value}))

    def test_virtual_live_defaults_keep_small_edges_and_scale_large_ones(self):
        config = json.loads((ROOT / "strategies/stale_quote_sniping/config.json").read_text())
        self.assertEqual(config["entry_edge_ticks"], 3.0)
        self.assertEqual(config["order_lots"], 2)
        self.assertEqual(config["max_order_lots"], 50)
        self.assertEqual(config["exit_confirmation_seconds"], 0.0)
        tick = 0.1
        self.assertEqual(scaled_entry_size(0.3, tick, config), 2)
        self.assertEqual(scaled_entry_size(0.5, tick, config), 12)
        self.assertEqual(scaled_entry_size(0.7, tick, config), 22)
        self.assertEqual(scaled_entry_size(1.3, tick, config), 50)

    def test_large_edge_falls_back_to_available_profitable_size(self):
        config = json.loads((ROOT / "strategies/stale_quote_sniping/config.json").read_text())
        book = {"tick": 0.1, "asks": [(100.2, 2), (100.3, 3)], "bids": [(100.0, 5)]}
        quantity, execution, edge = sized_execution(book, True, 101.0, config)
        self.assertEqual(quantity, 5)
        self.assertAlmostEqual(execution, 100.26)
        self.assertAlmostEqual(edge, 0.74)


class CausalityTests(unittest.TestCase):
    def test_epoch_phase_prior_is_active_on_first_snapshot(self):
        model = CausalBasisModel(BasisSettings(
            prior_enabled=True, prior_peak_epoch_seconds=165.95,
            prior_center=0.0, prior_amplitude=3.1,
            prior_rmse=0.9, prior_fit_r2=0.88,
        ))
        epoch = 1800000165.95
        signal = model.observe(now=0.0, a_mid=103.1, b_mid=100.0,
                               book_stamps=(epoch, epoch))
        self.assertTrue(signal["active"])
        self.assertEqual(signal["samples"], 0)
        self.assertEqual(signal["quality_scope"], "historical_epoch_phase_prior")
        self.assertAlmostEqual(signal["predicted_basis"], 3.1)
        self.assertAlmostEqual(signal["fair_B"], 100.0)

    def test_prior_reactivates_after_data_gap_without_warmup(self):
        settings = BasisSettings(prior_enabled=True)
        model = CausalBasisModel(settings)
        first = model.observe(now=0.0, a_mid=103.1, b_mid=100.0,
                              book_stamps=(1800000165.95, 1800000165.95))
        later_epoch = 1800000165.95 + settings.max_gap_seconds + 1
        later = model.observe(now=settings.max_gap_seconds + 1,
                              a_mid=102.8, b_mid=100.0,
                              book_stamps=(later_epoch, later_epoch))
        self.assertTrue(first["active"])
        self.assertTrue(later["active"])
        self.assertEqual(later["quality_scope"], "historical_epoch_phase_prior")

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
