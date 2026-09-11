import unittest
from datetime import datetime, timezone
from constrained_replay import simulate, sweep

def book(t, bid=100, ask=None, depth=1000):
    ask = bid+.2 if ask is None else ask
    return {'status':'ok','book_timestamp':datetime.fromtimestamp(t,timezone.utc).isoformat(),
            'bids':[[bid,depth]],'asks':[[ask,depth]]}

def entry(i, quantity=2, price=100):
    return {'trade_id':i,'quantity':quantity,'price':price,'sign':1}

class ReplayTests(unittest.TestCase):
    def test_inventory_cap_and_oldest_deadline(self):
        events = [(0,0,book(0)),(.1,1,entry(1)),(.2,1,entry(2)),(.3,1,entry(3)),(.4,1,entry(4)),(1.11,0,book(1.11,101))]
        r = simulate(events,1)
        self.assertEqual(r['stats']['accepted_volume'],6)
        self.assertEqual(r['stats']['rejected_inventory_cap'],1)
        self.assertEqual(r['residual_position'],0)
        self.assertAlmostEqual(r['realized_pnl'],6)
    def test_missing_depth_keeps_exit_pending(self):
        events = [(0,0,book(0)),(.1,1,entry(1)),(1.2,0,book(1.2,depth=201)),
                  (1.3,1,entry(2)),(2,0,book(2,99))]
        r = simulate(events,1)
        self.assertEqual(r['stats']['rejected_exit_pending'],1)
        self.assertEqual(r['exits'][0]['time'],2)
        self.assertAlmostEqual(r['realized_pnl'],-2)
    def test_session_stop_latches_and_can_overshoot(self):
        events = [(0,0,book(0)),(.1,1,entry(1)),(.5,0,book(.5,88)),
                  (.6,1,entry(2)),(1,0,book(1,110)),(1.1,1,entry(3))]
        r = simulate(events,15)
        self.assertTrue(r['session_stopped'])
        self.assertEqual(r['stats']['rejected_session_stop'],2)
        self.assertAlmostEqual(r['realized_pnl'],-24)
    def test_terminal_inventory_is_not_silently_flattened(self):
        r = simulate([(0,0,book(0)),(.1,1,entry(1))],15)
        self.assertEqual(r['residual_position'],2)
        self.assertIsNone(r['terminal_equity'])
        self.assertEqual(r['realized_pnl'],0)
    def test_no_future_book_at_entry(self):
        r = simulate([(.1,1,entry(1)),(.2,0,book(.2))],15)
        self.assertEqual(r['stats']['accepted_volume'] if 'accepted_volume' in r['stats'] else 0,0)
        self.assertEqual(r['stats']['rejected_no_fresh_book'],1)
    def test_sweep_accounts_for_full_position_and_boundary(self):
        b = book(0,depth=202)
        b['bids'] += [[99.5,2],[98,10000]]
        self.assertAlmostEqual(sweep(b,1,4),99.75)
        self.assertIsNone(sweep(b,1,5))

if __name__=='__main__':
    unittest.main()
