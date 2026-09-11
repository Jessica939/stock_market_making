import importlib
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
PACKAGE = 'stock_market_making.strategies.baseline-refine-hybird'
load = importlib.import_module(PACKAGE + '.run').load_quote_definitions
CyclePosition = importlib.import_module(PACKAGE + '.cycle_position').CyclePosition
cycle = importlib.import_module(PACKAGE + '.cycle_signal')
execution = importlib.import_module(PACKAGE + '.order_execution')
QuoteManager = execution.QuoteManager
LimitedExchange = execution.LimitedExchange
OrderLimitError = execution.OrderLimitError
A, B = 'PHILIPS_A', 'PHILIPS_B'


def book(mid=100, spread=.2):
    return NS(bids=[NS(price=round(mid-spread/2, 10), volume=1000)],
              asks=[NS(price=round(mid+spread/2, 10), volume=1000)])


class CyclePositionTests(unittest.TestCase):
    def setUp(self):
        self.strategy = load()
        self.plan = CyclePosition(**self.strategy['B_POSITION_SETTINGS'])
        self.signal = dict(active=True, predicted_B_change=3., fit_weight=1.)

    def quote(self, now, position=0, mid=100, spread=.2):
        return self.strategy['plan_quote'](book(mid, spread), position, .1, B, self.signal, self.plan, now)

    def test_full_entry_and_partial_fill_request_all_remaining_target(self):
        first = self.quote(0)
        self.assertEqual((first['buy_volume'], first['sell_volume']), (100, 0))
        self.assertEqual(first['bid_price'], 100)
        self.assertEqual(self.quote(1, 37)['buy_volume'], 63)
        self.assertEqual(self.quote(2, 100)['buy_volume'], 0)

    def test_45_seconds_is_not_a_minimum_or_a_deadline(self):
        self.quote(0)
        self.quote(1, 100)
        for now in (45, 60, 180, 400):
            q = self.quote(now, 100)
            self.assertEqual((q['buy_volume'], q['sell_volume']), (0, 0))
            self.assertTrue(q['cycle_position']['hold_reference_elapsed'])
            self.assertEqual(q['cycle_position']['reason'], 'hold_cycle_position')

    def test_take_profit_can_exit_before_45_seconds_with_full_position(self):
        self.quote(0)
        q = self.quote(2, 100, 102.4)
        self.assertEqual(q['cycle_position']['reason'], 'cycle_take_profit')
        self.assertEqual((q['sell_volume'], q['target_position']), (100, 0))
        self.assertAlmostEqual(q['ask_price'], 102.4)
        self.assertTrue(q['reduce_only'])

    def test_profit_target_does_not_follow_refits_or_additional_fills(self):
        original = self.quote(0)['cycle_position']['target_price']
        self.signal['predicted_B_change'] = 8
        q = self.quote(3, 75)
        self.assertEqual(q['cycle_position']['target_price'], original)
        self.assertEqual(q['cycle_position']['hold_reference_at'], 45)

    def test_exit_crosses_after_two_seconds_and_timer_survives_partial_fills(self):
        self.quote(0)
        self.quote(2, 100, 102.4)
        q = self.quote(3, 73, 101)
        self.assertEqual(q['sell_volume'], 73)
        self.assertEqual(q['cycle_position']['exit_mode'], 'improve_best')
        q = self.quote(4, 73, 101)
        self.assertEqual(q['sell_volume'], 73)
        self.assertEqual(q['cycle_position']['exit_mode'], 'cross_opposing_book')
        self.assertAlmostEqual(q['ask_price'], 99.9)
        self.assertEqual(q['cycle_position']['exit_started'], 2)

    def test_short_entry_and_take_profit_are_symmetric(self):
        self.signal['predicted_B_change'] = -3
        q = self.quote(0)
        self.assertEqual((q['buy_volume'], q['sell_volume']), (0, 100))
        self.assertEqual(q['ask_price'], 100)
        q = self.quote(3, -100, 97.6)
        self.assertEqual(q['cycle_position']['reason'], 'cycle_take_profit')
        self.assertEqual(q['buy_volume'], 100)
        q = self.quote(5, -71, 98)
        self.assertEqual(q['buy_volume'], 71)
        self.assertAlmostEqual(q['bid_price'], 99.1)

    def test_single_spike_and_old_20_tick_stop_do_not_exit(self):
        self.quote(0)
        self.assertEqual(self.quote(1, 100, 97)['sell_volume'], 0)
        self.assertEqual(self.quote(2, 100, 93)['sell_volume'], 0)
        self.assertEqual(self.quote(2.5, 100, 100)['sell_volume'], 0)

    def test_wider_sustained_stop_latches_until_flat(self):
        self.quote(0)
        for t in (1, 1.5, 2, 2.5, 3, 3.5):
            self.assertEqual(self.quote(t, 100, 93)['sell_volume'], 0)
        self.assertEqual(self.quote(4, 100, 93)['cycle_position']['reason'], 'cycle_position_stop')
        self.assertEqual(self.quote(4.5, 80, 100)['sell_volume'], 80)
        self.assertEqual(self.quote(5, 0)['buy_volume'], 0)
        self.assertEqual(self.quote(7, 0)['buy_volume'], 100)

    def test_data_gap_is_not_sustained_stop_evidence(self):
        self.quote(0)
        self.quote(1, 100, 93)
        self.assertEqual(self.quote(8, 100, 93)['sell_volume'], 0)

    def test_no_fill_retries_one_second_after_window_not_45_seconds(self):
        self.quote(0)
        self.assertEqual(self.quote(10)['buy_volume'], 0)
        self.assertEqual(self.quote(10.5)['buy_volume'], 0)
        self.assertEqual(self.quote(11)['buy_volume'], 100)

    def test_build_window_and_inactive_signal_cannot_force_exit(self):
        self.quote(0)
        self.signal = dict(active=False)
        q = self.quote(2, 37)
        self.assertEqual((q['buy_volume'], q['sell_volume']), (0, 0))
        self.signal = dict(active=True, predicted_B_change=-3, fit_weight=1)
        self.assertEqual(self.quote(50, 37)['sell_volume'], 0)

    def test_untracked_inventory_closes_every_remaining_share(self):
        q = self.quote(0, -183)
        self.assertEqual((q['buy_volume'], q['sell_volume']), (183, 0))
        q = self.quote(2, -171)
        self.assertEqual(q['buy_volume'], 171)
        self.assertEqual(q['cycle_position']['exit_mode'], 'cross_opposing_book')

    def test_one_tick_spread_entry_takes_touch_without_two_sided_self_cross(self):
        q = self.quote(0, spread=.1, mid=100.05)
        self.assertEqual(q['bid_price'], 100.1)
        self.assertEqual(q['sell_volume'], 0)

    def test_wide_book_does_not_block_existing_exit(self):
        self.quote(0)
        q = self.quote(1, 100, 104, spread=3)
        self.assertEqual(q['sell_volume'], 100)

    def test_A_aggressive_sizes_and_non_crossing_prices(self):
        for spread in (.1, .2, .3, 1):
            for pos in (-200, -160, -30, 0, 30, 160, 200):
                q = self.strategy['calculate_quote'](book(100.05 if spread==.1 else 100,spread),pos,.1)
                self.assertLess(q['bid_price'],q['ask_price'])
                self.assertLessEqual(q['buy_volume']+q['sell_volume'],200)
                if pos>0:
                    self.assertEqual(q['sell_volume'],pos)
                    self.assertTrue(q['reduce_ask'])
                elif pos<0:
                    self.assertEqual(q['buy_volume'],-pos)
                    self.assertTrue(q['reduce_bid'])
                else:
                    self.assertEqual((q['buy_volume'],q['sell_volume']),(100,100))


class Exchange:
    def __init__(self, positions=None):
        self.positions = dict(positions or {A:0,B:0})
        self.orders = {A:{},B:{}}
        self.next_id = 1
        self.before_positions = None

    def get_positions(self):
        if self.before_positions:
            callback, self.before_positions = self.before_positions, None
            callback()
        return dict(self.positions)

    def get_outstanding_orders(self, iid):
        return {oid:NS(**vars(o)) for oid,o in self.orders[iid].items()}

    def insert_order(self,iid,*,price,volume,side,order_type='limit'):
        oid,self.next_id=self.next_id,self.next_id+1
        self.orders[iid][oid]=NS(price=price,volume=volume,side=side)
        net=sum(self.positions.values())
        bids=sum(o.volume for orders in self.orders.values() for o in orders.values() if o.side=='bid')
        asks=sum(o.volume for orders in self.orders.values() for o in orders.values() if o.side=='ask')
        assert net+bids<=200 and net-asks>=-200, (net,bids,asks)
        own_bids=sum(o.volume for o in self.orders[iid].values() if o.side=='bid')
        own_asks=sum(o.volume for o in self.orders[iid].values() if o.side=='ask')
        if side == 'bid':
            assert self.positions[iid]+own_bids<=100, (iid,self.positions[iid],own_bids)
        else:
            assert self.positions[iid]-own_asks>=-100, (iid,self.positions[iid],own_asks)
        return NS(success=True,order_id=oid)

    def delete_order(self,iid,*,order_id):
        self.orders[iid].pop(order_id,None)
        return NS(success=True)

    def fill(self,iid,oid,qty):
        order=self.orders[iid][oid]
        self.positions[iid] += qty if order.side=='bid' else -qty
        order.volume -= qty
        if not order.volume:
            del self.orders[iid][oid]


class NetLimitTests(unittest.TestCase):
    def quote(self, side, volume=100, **extra):
        return dict(bid_price=99.9,ask_price=100.1,
                    buy_volume=volume if side=='bid' else 0,
                    sell_volume=volume if side=='ask' else 0,**extra)

    def quantity(self,x,iid,side):
        return sum(o.volume for o in x.orders[iid].values() if o.side==side)

    def test_combined_positions_limit_B_to_remaining_50(self):
        x=Exchange({A:100,B:50})
        QuoteManager(x).reconcile(B,self.quote('bid'))
        self.assertEqual(self.quantity(x,B,'bid'),50)

    def test_existing_A_bid_reserves_capacity_and_filling_it_does_not_release_room(self):
        x=Exchange()
        oid=x.insert_order(A,price=99.9,volume=60,side='bid').order_id
        manager=QuoteManager(x)
        manager.reconcile(B,self.quote('bid'))
        self.assertEqual(self.quantity(x,B,'bid'),100)
        x.fill(A,oid,60)
        manager.reconcile(B,self.quote('bid'))
        self.assertEqual(self.quantity(x,B,'bid'),100)

    def test_opposite_orders_never_offset_pending_exposure(self):
        x=Exchange({A:100,B:50})
        x.insert_order(A,price=100.1,volume=100,side='ask')
        QuoteManager(x).reconcile(B,self.quote('bid'))
        self.assertEqual(self.quantity(x,B,'bid'),50)

    def test_each_symbol_can_reserve_100_with_combined_limit_200(self):
        x=Exchange()
        oid=x.insert_order(A,price=99.9,volume=60,side='bid').order_id
        m=QuoteManager(x)
        q=self.quote('bid',target_position=100)
        m.reconcile(B,q)
        self.assertEqual(self.quantity(x,B,'bid'),100)
        x.delete_order(A,order_id=oid)
        m.reconcile(B,q)
        self.assertEqual(self.quantity(x,B,'bid'),100)

    def test_negative_net_limit_is_symmetric(self):
        x=Exchange({A:-100,B:-50})
        QuoteManager(x).reconcile(B,self.quote('ask'))
        self.assertEqual(self.quantity(x,B,'ask'),50)

    def test_A_and_B_can_each_reserve_100_bids(self):
        x=Exchange()
        m=QuoteManager(x)
        m.reconcile(B,self.quote('bid'))
        m.reconcile(A,self.quote('bid'))
        self.assertEqual(self.quantity(x,B,'bid'),100)
        self.assertEqual(self.quantity(x,A,'bid'),100)

    def test_full_close_has_no_five_share_cap_and_cannot_cross_flat(self):
        x=Exchange({A:0,B:183})
        m=QuoteManager(x)
        m.reconcile(B,self.quote('ask',200,reduce_only=True,target_position=0))
        self.assertEqual(self.quantity(x,B,'ask'),183)
        oid=next(iter(x.orders[B]))
        x.fill(B,oid,150)
        m.reconcile(B,self.quote('ask',200,reduce_only=True,target_position=0))
        self.assertEqual(self.quantity(x,B,'ask'),33)

    def test_A_reducing_side_is_capped_by_fresh_position(self):
        x=Exchange({A:30,B:0})
        QuoteManager(x).reconcile(A,self.quote('ask',100,reduce_ask=True))
        self.assertEqual(self.quantity(x,A,'ask'),30)

    def test_fill_between_other_orders_and_positions_is_conservative(self):
        x=Exchange()
        oid=x.insert_order(A,price=99.9,volume=60,side='bid').order_id
        m=QuoteManager(x)
        x.before_positions=lambda:x.fill(A,oid,60)
        desired=m._desired(self.quote('bid'))
        capacity=m._capacity('bid',desired,0,B)
        self.assertLessEqual(capacity,140)

    def test_missing_account_instrument_fails_closed(self):
        x=Exchange({B:0})
        with self.assertRaises(RuntimeError):
            QuoteManager(x).reconcile(B,self.quote('bid'))

    def test_fill_between_order_snapshot_and_position_check_is_not_a_fault(self):
        x=Exchange()
        oid=x.insert_order(B,price=99.9,volume=100,side='bid').order_id
        m=QuoteManager(x)
        snapshot=m._orders(B)
        x.before_positions=lambda:x.fill(B,oid,100)
        m._verify(B,snapshot,m._desired(self.quote('bid')),0)
        self.assertEqual(x.positions[B],100)

    def test_retries_never_accept_a_real_net_limit_violation(self):
        x=Exchange({A:100,B:50})
        x.orders[B][1]=NS(side='bid',price=99.9,volume=100)
        m=QuoteManager(x)
        with self.assertRaises(ValueError):
            m._verify(B,m._orders(B),m._desired(self.quote('bid')),0)

    def test_existing_short_position_reduces_new_ask_to_90(self):
        x=Exchange({A:-10,B:0})
        QuoteManager(x).reconcile(A,self.quote('ask',100))
        self.assertEqual(self.quantity(x,A,'ask'),90)

    def test_final_sender_guard_blocks_the_original_minus_110_order(self):
        x=Exchange({A:-10,B:0})
        guarded=LimitedExchange(x,position_limit=100)
        with self.assertRaises(OrderLimitError):
            guarded.insert_order(A,price=100.1,volume=100,side='ask',order_type='limit')
        self.assertEqual(self.quantity(x,A,'ask'),0)
        guarded.insert_order(A,price=100.1,volume=90,side='ask',order_type='limit')
        self.assertEqual(self.quantity(x,A,'ask'),90)


if __name__=='__main__':
    unittest.main()
