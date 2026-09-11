import ast
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from stock_market_making.recording.storage import RunStorage, hybrid_state_path
from stock_market_making.strategies.common.runner import Journal, main
from stock_market_making.strategies.pair.policy import Policy


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_runs_are_unique_and_manifest_links_resolve(self):
        market = self.root / 'market' / 'sample'
        market.mkdir(parents=True)
        for _ in range(2):
            journal = Journal(self.root / 'runs', strategy='pair', config={'symbols': ['A', 'B']})
            journal.storage.link_market(market)
            journal.emit('fill', price=100)
            journal.close()
            manifest = json.loads(journal.storage.path.read_text(encoding='utf-8'))
            self.assertEqual(manifest['strategy'], 'pair')
            self.assertEqual(manifest['config']['symbols'], ['A', 'B'])
            self.assertEqual((journal.storage.directory / manifest['market_recording']['path']).resolve(), market)
            events = journal.storage.directory / manifest['event_log']
            self.assertEqual(json.loads(events.read_text())['type'], 'fill')
        self.assertEqual(len(list((self.root / 'runs').glob('*/manifest.json'))), 2)

    def test_state_namespaces_and_legacy_checkpoint_or_lock(self):
        expected = self.root / 'state/default/hybrid.json'
        self.assertEqual(hybrid_state_path(root=self.root), expected)
        legacy = self.root / 'state/hybrid.json'
        legacy.parent.mkdir()
        legacy.write_text('preserved')
        self.assertEqual(hybrid_state_path(root=self.root), legacy)
        self.assertEqual(hybrid_state_path('account2', root=self.root), self.root / 'state/account2/hybrid.json')
        legacy.unlink()
        Path(str(legacy) + '.lock').touch()
        self.assertEqual(hybrid_state_path(root=self.root), legacy)
        expected.parent.mkdir()
        expected.touch()
        with self.assertRaisesRegex(ValueError, 'Both legacy'):
            hybrid_state_path(root=self.root)
        with self.assertRaises(ValueError):
            hybrid_state_path('../escape', root=self.root)

    def test_demo_entrypoint_writes_config_and_no_market_link(self):
        folder = Path(__file__).resolve().parents[1] / 'strategies/pair'
        result = main(Policy, 'pair', folder, ['--mode', 'demo', '--duration', '121',
                      '--log-dir', str(self.root / 'runs')])
        self.assertEqual(result, 0)
        manifest = json.loads(next((self.root / 'runs').glob('*/manifest.json')).read_text())
        self.assertEqual(manifest['mode'], 'demo')
        self.assertEqual(manifest['config']['session_seconds'], 121)
        self.assertIsNone(manifest['market_recording'])
        self.assertFalse((self.root / 'market').exists())

    def test_baseline_main_links_logger_and_market_before_loop(self):
        # Execute the actual Python strategy main with inert adapters; never import a live client.
        from dataclasses import asdict, dataclass
        import logging
        from types import ModuleType, SimpleNamespace
        from unittest.mock import patch
        strategy_text = (Path(__file__).resolve().parents[1] /
                         'strategies/baseline/strategy.py').read_text(encoding='utf-8')
        tree = ast.parse(strategy_text)
        main_node = next(node for node in tree.body
                         if isinstance(node, ast.FunctionDef) and node.name == 'main')
        source = ast.get_source_segment(strategy_text, main_node)
        exchange = Mock()
        exchange.get_tradable_instruments.return_value = {'PHILIPS_A': SimpleNamespace(tick_size=.1)}
        exchange.is_connected.return_value = False
        client = ModuleType('optibook.synchronous_client')
        client.Exchange = Mock(return_value=exchange)
        @dataclass
        class Settings:
            enabled: bool = True
        def logger(raw, ids, directory, **kwargs):
            obj = Mock(path=Path(directory) / 'trading_test.jsonl')
            obj.path.touch()
            return obj
        market = self.root / 'market/sample'
        market.mkdir(parents=True)
        ns = dict(logging=logging, asdict=asdict, time=__import__('time'),
                  RunStorage=RunStorage, StrategyRecorder=logger,
                  PhilipsPriceRecorder=Mock(return_value=Mock(directory=market)),
                  LimitedExchange=lambda raw, **kw: raw, RecordedExchange=lambda raw, rec: raw,
                  QuoteManager=Mock(), CycleSignal=Mock(), CYCLE_SETTINGS=Settings(),
                  B_PROTECTION=Settings(), LOG_DIR=self.root / 'runs', PRICE_DATA_DIR=market.parent,
                  TRADE_INSTRUMENTS={'PHILIPS_A'}, MARKOUT_HORIZONS=(1, 3), STRATEGY_VERSION='test')
        for name in ('POSITION_LIMIT', 'SOFT_LIMIT', 'ORDER_VOLUME', 'VWAP_HALF_LIFE_TICKS',
                     'INVENTORY_SCALE', 'INVENTORY_SKEW_TICKS', 'SPREAD_MULTIPLIER',
                     'MIN_HALF_SPREAD_TICKS', 'MARKOUT_TIMEOUT_SECONDS', 'MAX_OUTSTANDING_VOLUME',
                     'MAX_UPDATES_PER_SECOND', 'CYCLE_PRICE_SHIFT_ENABLED'):
            ns[name] = 1
        exec(compile(source, 'baseline_main', 'exec'), ns)
        with patch.dict(sys.modules, {'optibook': ModuleType('optibook'), 'optibook.synchronous_client': client}):
            ns['main']()
        manifest = json.loads(next((self.root / 'runs').glob('*/manifest.json')).read_text())
        self.assertEqual(manifest['strategy'], 'baseline')
        self.assertEqual(manifest['config']['strategy_version'], 'test')
        self.assertEqual(manifest['event_log'], 'trading_test.jsonl')
        self.assertEqual(manifest['market_recording']['recording_id'], 'sample')
        exchange.disconnect.assert_called_once()


if __name__ == '__main__':
    unittest.main()
