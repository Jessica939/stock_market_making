import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT.parent))
from stock_market_making.strategies.b_cycle.engine import Engine, Feed, SYMBOLS, validate
from stock_market_making.strategies.common.execution import ExecutionFault
from stock_market_making.strategies.common.market import UnusableBook
from stock_market_making.strategies.common.simulation import ReplayExchange, SimClock


class Journal:
    failed=False
    def __init__(self):
        self.events=[]
    def emit(self,kind,**fields):
        self.events.append((kind,fields))


class BCycleTests(unittest.TestCase):
    def setUp(self):
        self.config=json.loads((ROOT/'strategies/b_cycle/config.json').read_text())
        self.clock=SimClock();self.exchange=ReplayExchange(SYMBOLS,self.clock,fill_fraction=1)
        self.journal=Journal();self.frame(0)
        self.engine=Engine(self.exchange,self.config,self.journal,self.clock.monotonic,self.clock.sleep,
                           lambda:1800000000+self.clock.now,terminal_quantity=self.exchange.ioc_terminal_quantity)
        self.engine.startup()
        self.signal=dict(active=True,predicted_B_change=3.,fit_weight=1.)
        self.engine.model=NS(observe=lambda *args:self.signal)

    def frame(self,t,bid=100,ask=None,depth=1000,missing=False):
        ask=bid+.2 if ask is None else ask
        self.clock.now=t
        books={i:dict(timestamp=1800000000+t,tick=.1,bids=[[bid,depth]],asks=[[ask,depth]]) for i in SYMBOLS}
        if missing:
            books['PHILIPS_B']=None
        self.exchange.advance(dict(epoch=1800000000+t,books=books))

    def enter(self):
        self.engine.step();self.frame(.5);self.engine.step()
        self.assertEqual(self.exchange.positions['PHILIPS_B'],2)

    def test_only_b_one_entry_and_15_second_exit(self):
        self.enter()
        self.frame(2);self.engine.step()
        self.assertEqual(len(self.exchange.fills),1)
        self.frame(15.5,101);self.engine.step()
        self.assertEqual(self.exchange.positions['PHILIPS_B'],2)
        self.frame(16,101);self.engine.step()
        self.assertEqual(self.exchange.positions,dict.fromkeys(SYMBOLS,0))
        self.assertTrue(all(t.instrument_id=='PHILIPS_B' for t in self.exchange.fills))
        self.assertAlmostEqual(self.exchange.cash['PHILIPS_B'],1.6)

    def test_recheck_frozen_signal_at_fresh_execution_price(self):
        self.engine.step();self.frame(.5,104);self.engine.step()
        self.assertFalse(self.exchange.fills)
        self.frame(2);self.engine.step()
        self.assertIsNone(self.engine.pending)

    def test_missing_exit_keeps_inventory_and_blocks_new_entry(self):
        self.enter();self.frame(16,missing=True);self.engine.step()
        self.assertEqual(self.exchange.positions['PHILIPS_B'],2)
        self.assertEqual(self.engine.exit_reason,'hold_timeout')
        self.frame(17);self.engine.step()
        self.assertEqual(self.exchange.positions['PHILIPS_B'],0)
        self.assertEqual(len(self.exchange.fills),2)

    def test_cycle_unavailable_does_not_immediately_dump_position(self):
        self.enter();self.signal={'active':False,'reason':'invalid_pair_books'}
        self.frame(3);self.engine.step()
        self.assertEqual(self.exchange.positions['PHILIPS_B'],2)
        self.assertIsNone(self.engine.exit_reason)

    def test_account_risk_latches_and_reduces_immediately(self):
        self.engine.config['max_session_loss']=5
        self.enter();self.frame(2,90);self.engine.step()
        self.assertEqual(self.exchange.positions['PHILIPS_B'],0)
        self.assertTrue(self.engine.risk_stopped)
        self.frame(4);self.engine.step()
        self.assertEqual(len(self.exchange.fills),2)

    def test_closeout_prevents_new_entry_and_flattens(self):
        self.enter();self.engine.cutoff=2
        self.frame(2);self.engine.step()
        self.assertTrue(self.engine.stopped)
        self.assertEqual(self.exchange.positions['PHILIPS_B'],0)

    def test_inherited_inventory_is_not_adopted(self):
        self.exchange.positions['PHILIPS_A']=1
        with self.assertRaises(ExecutionFault):
            self.engine.startup()
        self.assertFalse(self.exchange.fills)

    def test_confirmed_partial_entry_is_not_topped_up(self):
        self.exchange.fill_fraction=.001
        self.engine.step();self.frame(.5);self.engine.step()
        self.assertEqual(self.exchange.positions['PHILIPS_B'],1)
        self.assertIsNotNone(self.engine.opened)
        self.frame(2);self.engine.step()
        self.assertEqual(len(self.exchange.fills),1)

    def test_unproven_partial_ioc_halts_all_further_inserts(self):
        self.exchange.fill_fraction=.001
        self.engine.executor.terminal_quantity=None
        self.engine.step();self.frame(.5)
        with self.assertRaises(ExecutionFault):
            self.engine.step()
        self.assertTrue(self.engine.executor.unresolved)
        with self.assertRaises(ExecutionFault):
            self.engine.executor.send('PHILIPS_B','ask',1,reducing=True)
        self.assertEqual(len(self.exchange.fills),1)

    def test_end_of_replay_does_not_fabricate_exit(self):
        self.enter();summary=self.engine.finish(live=False)
        self.assertFalse(summary['flat'])
        self.assertEqual(summary['positions']['PHILIPS_B'],2)
        self.assertEqual(len(self.exchange.fills),1)

    def test_invalid_size_or_unvalidated_horizon_rejected(self):
        for key,value in [('order_lots',3),('hold_seconds',5),('stop_per_share',float('nan'))]:
            with self.subTest(key=key),self.assertRaises(ValueError):
                validate(dict(self.config,**{key:value}))


if __name__=='__main__':
    unittest.main()
