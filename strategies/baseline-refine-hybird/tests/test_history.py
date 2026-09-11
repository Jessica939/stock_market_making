import csv
from datetime import datetime, timezone
import importlib
import math
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
PACKAGE = 'stock_market_making.strategies.baseline-refine-hybird'
cycle = importlib.import_module(PACKAGE + '.cycle_signal')
history = importlib.import_module(PACKAGE + '.cycle_history')
load = importlib.import_module(PACKAGE + '.run').load_quote_definitions
Position = importlib.import_module(PACKAGE + '.cycle_position').CyclePosition
A, B = history.SYMBOLS
WALL, NOW = 1_800_000_000., 1000.
TICKS = {A: .1, B: .1}


def basis(t):
    return 3*math.sin(2*math.pi*(t-WALL)/180)


def stamp(t):
    return datetime.fromtimestamp(t, timezone.utc)


def books(wall=WALL, shock=0):
    mids = {A: 100., B: round((100-basis(wall)+shock)*10)/10}
    return {s: NS(timestamp=stamp(wall), bids=[NS(price=m-.1, volume=1000)],
                  asks=[NS(price=m+.1, volume=1000)]) for s, m in mids.items()}


def rows(end=-1):
    return [(WALL+t, basis(WALL+t)) for t in range(-360, end+1)]


def write_prices(directory):
    path = Path(directory)/'philips_20270115_test'/'prices.csv'
    path.parent.mkdir()
    fields = ('sample_id', 'observed_at_utc', 'instrument_id', 'book_timestamp',
              'status', 'best_bid', 'best_ask')
    with path.open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for i, (wall, _) in enumerate(rows()):
            for symbol, book in books(wall).items():
                writer.writerow(dict(sample_id=i, observed_at_utc=stamp(wall).isoformat(),
                                     instrument_id=symbol, book_timestamp=stamp(wall).isoformat(),
                                     status='ok', best_bid=book.bids[0].price, best_ask=book.asks[0].price))
    return path


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.strategy = load()
        self.model = cycle.CycleSignal(self.strategy['CYCLE_SETTINGS'])

    def seed(self, data=None, now=NOW, wall=WALL):
        return self.model.seed(rows() if data is None else data, TICKS, now, wall, source='test')

    def test_first_live_observation_can_create_full_size_entry(self):
        self.assertTrue(self.seed()['loaded'])
        live = books()
        signal = self.model.observe(live, TICKS, NOW, WALL)
        self.assertTrue(signal['active'])
        self.assertAlmostEqual(signal['predicted_B_change'], -3, places=8)
        plan = Position(**self.strategy['B_POSITION_SETTINGS'])
        quote = self.strategy['plan_quote'](live[B], 0, .1, B, signal, plan, NOW)
        self.assertEqual(quote['sell_volume'], 100)
        self.assertEqual(quote['buy_volume'], 0)

    def test_absolute_phase_survives_monotonic_origin_and_restart_gap(self):
        report = self.seed(rows(-60), now=20.)
        self.assertTrue(report['loaded'])
        signal = self.model.observe(books(), TICKS, 20., WALL)
        self.assertTrue(signal['active'])
        self.assertAlmostEqual(signal['predicted_B_change'], -3, places=8)
        self.assertGreater(signal['samples'], 200)

    def test_later_observation_gap_still_resets(self):
        self.seed()
        self.model.observe(books(), TICKS, NOW, WALL)
        signal = self.model.observe(books(WALL+20), TICKS, NOW+20, WALL+20)
        self.assertFalse(signal['active'])
        self.assertEqual(signal['reason'], 'warmup')

    def test_seed_cannot_wait_indefinitely_for_first_valid_book(self):
        self.seed(rows(-60))
        signal = self.model.observe(books(WALL+20), TICKS, NOW+20, WALL+20)
        self.assertFalse(signal['active'])
        self.assertEqual(signal['samples'], 1)

    def test_stale_future_and_too_short_history_do_not_seed(self):
        for data in (rows(-181), [(WALL+i, 3.) for i in range(1, 300)], rows()[-19:]):
            with self.subTest(data=data[:1]):
                self.assertFalse(self.seed(data)['loaded'])
                self.assertEqual(len(self.model.history), 0)

    def test_future_perturbation_cannot_change_startup_prediction(self):
        self.seed()
        expected = self.model.observe(books(), TICKS, NOW, WALL)
        self.model.reset()
        self.seed(rows()+[(WALL+i, 1e6) for i in range(1, 200)])
        actual = self.model.observe(books(), TICKS, NOW, WALL)
        self.assertEqual(expected, actual)

    def test_live_shock_blocks_entry_even_with_good_history(self):
        self.seed()
        signal = self.model.observe(books(shock=4), TICKS, NOW, WALL)
        self.assertEqual(signal['reason'], 'residual_shock')
        self.assertFalse(signal['active'])

    def test_local_midpoint_history_is_preferred_without_polling(self):
        class Exchange:
            def get_trade_tick_history(self, symbol):
                raise AssertionError('valid midpoint history should be preferred')
            def poll_new_trade_ticks(self, symbol):
                raise AssertionError('recorder owns polling')
        with tempfile.TemporaryDirectory() as directory:
            path = write_prices(directory)
            report = history.bootstrap_cycle(self.model, Exchange(), directory, TICKS, NOW, WALL)
            self.assertTrue(report['loaded'])
            self.assertEqual(report['source'], str(path))
            signal = self.model.observe(books(), TICKS, NOW, WALL)
            self.assertTrue(signal['active'])
            self.assertAlmostEqual(signal['predicted_B_change'], -3, delta=.03)

    def test_sdk_history_initializes_without_consuming_public_poll(self):
        class Exchange:
            def get_trade_tick_history(self, symbol):
                return [NS(timestamp=stamp(t), price=100 if symbol == A else 100-y) for t, y in rows()]
            def poll_new_trade_ticks(self, symbol):
                raise AssertionError('recorder owns polling')
        with tempfile.TemporaryDirectory() as directory:
            report = history.bootstrap_cycle(self.model, Exchange(), directory, TICKS, NOW, WALL)
        self.assertTrue(report['loaded'])
        self.assertEqual(report['source'], 'exchange.get_trade_tick_history')
        self.assertTrue(self.model.observe(books(), TICKS, NOW, WALL)['active'])

    def test_absent_or_failed_api_falls_back_with_explanation(self):
        class Broken:
            def get_trade_tick_history(self, symbol):
                raise ConnectionError('unavailable')
        for exchange in (NS(), Broken()):
            with tempfile.TemporaryDirectory() as directory:
                report = history.bootstrap_cycle(self.model, exchange, directory, TICKS, NOW, WALL)
            self.assertFalse(report['loaded'])
            self.assertTrue(report['attempts'])
            self.assertEqual(self.model.observe(books(), TICKS, NOW, WALL)['reason'], 'warmup')
            self.model.reset()

    def test_trade_pairs_do_not_bridge_stale_or_future_prices(self):
        ticks = {A: [NS(timestamp=stamp(WALL-3), price=100), NS(timestamp=stamp(WALL+1), price=99)],
                 B: [NS(timestamp=stamp(WALL-1), price=100)]}
        self.assertEqual(history.trade_rows(ticks, self.model.settings, WALL), [])

    def test_midpoints_require_causal_and_synchronized_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_prices(directory)
            with path.open() as f:
                data = list(csv.DictReader(f))
            for row in data:
                if row['instrument_id'] == B:
                    row['book_timestamp'] = stamp(WALL+1).isoformat()
            with path.open('w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=list(data[0]))
                writer.writeheader()
                writer.writerows(data)
            self.assertEqual(history.price_rows(path, self.model.settings, TICKS, WALL), [])


if __name__ == '__main__':
    unittest.main()
