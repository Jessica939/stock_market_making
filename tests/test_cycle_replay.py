from datetime import datetime, timezone
from types import SimpleNamespace as NS
import unittest

from stock_market_making.analysis.cycle_revision_20260911.replay import (
    ROOT, SYMBOLS, environment, exchange_class, fill_metrics)


class CausalReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = environment(ROOT)

    def setUp(self):
        self.clock = self.env.sim.SimClock()
        self.exchange = exchange_class(self.env)(self.clock)

    def frame(self, observed, stamp, bid=99., ask=101.):
        self.clock.now = observed
        self.exchange.advance(dict(epoch=1800000000+observed, books={
            iid: dict(timestamp=1800000000+stamp, bids=[[bid,100]], asks=[[ask,100]])
            for iid in SYMBOLS}))

    def test_limit_requires_later_observation_and_actual_cross(self):
        self.frame(0,0)
        self.exchange.insert_order('PHILIPS_B',price=100,volume=20,side='bid',order_type='limit')
        self.assertFalse(self.exchange.fills)
        self.frame(.1,.1,bid=99,ask=100)
        self.assertFalse(self.exchange.fills)
        self.frame(.5,.5)
        self.assertFalse(self.exchange.fills)
        self.frame(1,1,bid=99,ask=100)
        self.assertEqual(self.exchange.positions['PHILIPS_B'],20)
        self.assertEqual(self.exchange.cash['PHILIPS_B'],-2000)
        self.assertEqual(self.exchange.fills[0].timestamp.timestamp(),1800000001)

    def test_repeated_book_and_cancelled_order_cannot_fill(self):
        self.frame(0,0)
        response=self.exchange.insert_order('PHILIPS_B',price=101,volume=5,side='bid',order_type='limit')
        self.frame(1,0)
        self.assertFalse(self.exchange.fills)
        self.exchange.delete_order('PHILIPS_B',order_id=response.order_id)
        self.frame(2,2)
        self.assertFalse(self.exchange.fills)

    def test_ioc_uses_only_current_price_and_depth(self):
        self.frame(0,0)
        self.exchange.insert_order('PHILIPS_B',price=101,volume=20,side='bid',order_type='ioc')
        self.assertEqual(self.exchange.positions['PHILIPS_B'],20)
        self.assertEqual(self.exchange.cash['PHILIPS_B'],-2020)
        self.assertFalse(self.exchange.full_fill_violations)
        self.frame(1,1,bid=149,ask=151)
        self.assertEqual(self.exchange.fills[0].price,101)

    def test_clock_sleep_cannot_fill_from_an_earlier_observation(self):
        self.frame(0,0)
        self.clock.sleep(2)
        self.exchange.insert_order('PHILIPS_B',price=100,volume=5,side='bid',order_type='limit')
        self.clock.sleep(2)
        self.exchange.advance(dict(epoch=1800000001,books={
            iid:dict(timestamp=1800000001,bids=[[99,100]],asks=[[100,100]]) for iid in SYMBOLS}))
        self.assertFalse(self.exchange.fills)
        self.frame(4,4,bid=99,ask=100)
        self.assertEqual(self.exchange.positions['PHILIPS_B'],5)

    def test_fifo_fees_reversal_and_open_inventory(self):
        def trade(t,side,volume,price):
            return NS(instrument_id='PHILIPS_B',side=side,volume=volume,price=price,
                      timestamp=datetime.fromtimestamp(t,timezone.utc))
        rows=[trade(0,'bid',10,100),trade(10,'bid',10,102),
              trade(30,'ask',25,105),trade(40,'bid',3,101)]
        result=fill_metrics(rows,.1)
        self.assertEqual(result['positions']['PHILIPS_B'],-2)
        self.assertAlmostEqual(result['cash']['PHILIPS_B'],297.2)
        self.assertAlmostEqual(result['realized_pnl']['PHILIPS_B'],87.4)
        self.assertEqual(result['median_fifo_hold_seconds']['PHILIPS_B'],20)
        self.assertEqual(result['closed_episodes']['PHILIPS_B'],1)
        self.assertEqual(result['peak_positions']['PHILIPS_B'],20)


if __name__ == '__main__':
    unittest.main()
