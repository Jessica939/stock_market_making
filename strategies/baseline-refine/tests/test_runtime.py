"""Run the real notebook loop with an inert exchange and deterministic clock."""
from dataclasses import asdict
import json
import logging
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from test_adaptation import A, B, WALL, NOW, books, cycle, load


class RuntimeTests(unittest.TestCase):
    def test_live_loop_keeps_observing_degraded_model_without_rebootstrapping(self):
        self.run_loop(False)

    def test_execution_error_preserves_model_and_continues_learning(self):
        self.run_loop(True)

    def run_loop(self, execution_error):
        strategy = load()
        clock = NS(now=0.)
        raw = Mock()
        raw.get_tradable_instruments.return_value = {s: NS(tick_size=.1) for s in (A, B)}
        raw.is_connected.side_effect = lambda: clock.now < 16
        raw.get_last_price_book.side_effect = lambda s: books(clock.now, 0)[s]
        raw.get_outstanding_orders.return_value = {}
        raw.get_positions.return_value = {A: 0, B: 183}
        client = ModuleType('optibook.synchronous_client')
        client.Exchange = Mock(return_value=raw)
        recorder = Mock(path=Path('/unused/events.jsonl'))
        storage = Mock(directory=Path('/unused/run'), path=Path('/unused/manifest.json'))
        model = cycle.CycleSignal(strategy['CYCLE_SETTINGS'])
        observations = []

        def bootstrap(*args, **kwargs):
            model.initialized = True
            return dict(loaded=True, source='inert_test')

        def degraded_observe(external_books, ticks, now, wall):
            observations.append(now)
            # Emulate an already initialized model losing its admissible fit.
            model.weights = None
            model.last_fit = int((now-NOW)/5)*5 + NOW
            return dict(active=False, reason='amplitude_out_of_bounds',
                        period_seconds=180, window_seconds=720,
                        adaptation=dict(state='stable', revision=0))

        model.observe = degraded_observe
        model.reset = Mock(wraps=model.reset)
        def sleep(seconds):
            clock.now += seconds

        plan_quote = Mock(return_value=None)
        if execution_error:
            # Fail once after bootstrap, then continue every later observation.
            calls = []
            def transient_failure(*args):
                calls.append(args)
                if len(calls) == 1:
                    raise RuntimeError('transient order failure')
                return None
            plan_quote.side_effect = transient_failure
        namespace = dict(strategy, logging=logging, asdict=asdict,
            time=NS(monotonic=lambda: NOW+clock.now, time=lambda: WALL+clock.now, sleep=sleep),
            RunStorage=Mock(return_value=storage), StrategyRecorder=Mock(return_value=recorder),
            PhilipsPriceRecorder=Mock(return_value=Mock(exchange=raw, directory=Path('/unused/market'))),
            LimitedExchange=lambda x, **kwargs: x, RecordedExchange=lambda x, rec: x,
            QuoteManager=Mock(), CycleSignal=Mock(return_value=model), CyclePosition=Mock(),
            bootstrap_cycle=Mock(side_effect=bootstrap), usable_book=cycle.usable_book,
            external_price_book=lambda book, orders, tick: book, plan_quote=plan_quote)
        path = Path(__file__).resolve().parents[1]/'strategy_with_logging.ipynb'
        notebook = json.loads(path.read_text())
        source = next(''.join(cell['source']) for cell in notebook['cells']
                      if ''.join(cell['source']).startswith('def main():'))
        exec(compile(source, str(path), 'exec'), namespace)
        with patch.dict(sys.modules, {'optibook': ModuleType('optibook'), 'optibook.synchronous_client': client}), patch('builtins.print'):
            namespace['main']()
        namespace['bootstrap_cycle'].assert_called_once()
        self.assertGreater(max(observations)-min(observations), 15)
        self.assertGreater(plan_quote.call_count, 50)
        self.assertTrue(any(call.args[1] == 183 for call in plan_quote.call_args_list))
        events = [call for call in recorder.event.call_args_list if call.args[0] == 'cycle_model_update']
        self.assertEqual(len(events), 4)
        errors = [call for call in recorder.event.call_args_list if call.args[0] == 'cycle_error']
        self.assertEqual(len(errors), int(execution_error))
        model.reset.assert_not_called()
        if errors:
            self.assertTrue(errors[0].kwargs['model_preserved'])
            self.assertIn('transient order failure', errors[0].kwargs['traceback'])
        raw.connect.assert_called_once()
        raw.disconnect.assert_called_once()


if __name__ == '__main__':
    unittest.main()
