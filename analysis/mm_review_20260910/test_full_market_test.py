import math
import unittest
from datetime import datetime,timezone
from full_market_test import signals,trade,cost,forecast_rows

def stamp(t):
    return datetime.fromtimestamp(t,timezone.utc).isoformat()

def book(t,bid=100,ask=100.2,instrument='PHILIPS_B',depth=1000):
    return dict(status='ok',instrument_id=instrument,observed_at_utc=stamp(t),
        book_timestamp=stamp(t),bids=[[bid,depth]],asks=[[ask,depth]])

def frame(t,bid=100,ask=100.2):
    return dict(now=t,b=book(t,bid,ask),decision=True,
                signal={'active':True,'predicted_B_change':3.,'fit_weight':1.})

class FullMarketTests(unittest.TestCase):
    def test_model_prefix_is_independent_of_future(self):
        rows=[]
        for i in range(800):
            t=i*.5
            basis=round(3*math.sin(t*2*math.pi/180),1)
            rows.extend([book(t,100,100.2,'PHILIPS_A'),book(t,100-basis,100.2-basis)])
        prefix=signals(rows[:1000])
        full=signals(rows)
        self.assertEqual(prefix,full[:len(prefix)])
        self.assertTrue(any(f['signal'].get('active') for f in prefix))
    def test_delayed_spread_crossing_and_timeout(self):
        r=trade([frame(0),frame(.5),frame(15.5,101.2,101.4),frame(16,101.2,101.4)])
        self.assertEqual(r['closed_trades'],1)
        self.assertAlmostEqual(r['realized_pnl'],2.)
        self.assertEqual(r['trades'][0]['exit_time'],16)
    def test_one_second_delay_does_not_expire_before_first_eligible_book(self):
        r=trade([frame(0),frame(.5),frame(1.5)],entry_delay=1)
        self.assertEqual(r['residual_position'],2)
        self.assertEqual(r['stats']['entered'],1)
    def test_future_entry_price_can_reject_frozen_forecast(self):
        r=trade([frame(0),frame(.5,104,104.2)])
        self.assertEqual(r['residual_position'],0)
        self.assertEqual(r['stats']['rejected_after_delay'],1)
    def test_missing_exit_keeps_inventory(self):
        f=frame(16);f['b']=book(16,depth=200)
        r=trade([frame(0),frame(.5),f])
        self.assertEqual(r['residual_position'],2)
        self.assertEqual(r['closed_trades'],0)
        self.assertIsNone(r['terminal_equity'])
    def test_stop_is_latched_and_delayed(self):
        r=trade([frame(0),frame(.5),frame(1,97,97.2),frame(1.5,99,99.2)],stop=2)
        self.assertEqual(r['trades'][0]['reason'],'stop')
        self.assertEqual(r['trades'][0]['exit_time'],1.5)
        self.assertAlmostEqual(r['realized_pnl'],-2.4)
    def test_two_share_depth_and_direction(self):
        b=book(0,100,101,depth=201)
        b['asks']+=[[101.5,2]]
        self.assertAlmostEqual(cost(b,True,200),101.25)
        self.assertIsNone(cost(b,False,200))

if __name__=='__main__':
    unittest.main()
