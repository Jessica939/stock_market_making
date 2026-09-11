import unittest

from replay import Replay, Scenario


def book(stamp=0.,volume=20):
    return dict(stamp=stamp, status='ok', bids=[[99.9,volume],[99.8,volume]],
                asks=[[100.1,volume],[100.2,volume]])


def engine(**kwargs):
    return Replay(dict(soft_limit=70),Scenario(depth_removal=0,depth_fraction=1.,**kwargs),0.,10.)


class ReplayTests(unittest.TestCase):
    def test_pricing_ablation_changes_only_selected_fair_input(self):
        config=dict(soft_limit=70,position_limit=100,order_volume=5,inventory_scale=35,
                    inventory_skew_ticks=2,cycle_settings={},b_protection={})
        q=dict(fair_value=100.3,cycle_shift=0.,half_spread=.1,best_bid=99.9,best_ask=100.1,
               cycle=dict(active=True,predicted_B_change=0.,fit_weight=.5))
        for mode,fair in [('depth',100.3),('clipped',100.1),('mid',100.)]:
            r=Replay(config,Scenario(fair_mode=mode),0.,10.)
            r.decision(.1,q)
            self.assertAlmostEqual(r.quotes[0]['fair'],fair)
            self.assertEqual(q['fair_value'],100.3)

    def test_entry_distance_changes_only_increasing_side(self):
        config=dict(soft_limit=70,position_limit=100,order_volume=5,inventory_scale=35,
                    inventory_skew_ticks=2,cycle_settings={},b_protection={})
        q=dict(fair_value=100.,cycle_shift=0.,half_spread=.1,best_bid=99.9,best_ask=100.1,
               cycle=dict(active=True,predicted_B_change=0.,fit_weight=.5))
        for position in (-2,0,2):
            quotes=[]
            for extra in (0,1):
                r=Replay(config,Scenario(entry_extra_ticks=extra,exit_guard=True),0.,10.)
                r.position=position
                r.decision(.1,q)
                quotes.append(r.quotes[0])
            a,b=quotes
            self.assertAlmostEqual(b['bid']-a['bid'],-.1 if position>=0 else 0.)
            self.assertAlmostEqual(b['ask']-a['ask'],.1 if position<=0 else 0.)
            self.assertEqual((a['buy'],a['sell']),(b['buy'],b['sell']))

    def test_queue_depletion_and_finite_trade_volume(self):
        r=engine(queue='displayed')
        r.orders['ask']=dict(price=100.2,volume=3,queue=5,placed=0.)
        trade=dict(price=100.2,volume=4,side='bid')
        r.trade(.1,trade)
        self.assertEqual(r.position,0)
        self.assertEqual(r.orders['ask']['queue'],1)
        r.trade(.2,dict(trade,volume=2))
        self.assertEqual(r.position,-1)
        self.assertEqual(r.orders['ask']['volume'],2)
        r.trade(.3,dict(trade,price=100.3,volume=1))
        self.assertEqual(r.position,-2)
        self.assertAlmostEqual(r.cash,200.4)

    def test_strict_through_does_not_fill_on_touch(self):
        r=engine(queue='through')
        r.orders['bid']=dict(price=99.8,volume=2,queue=0,placed=0.)
        r.trade(.1,dict(price=99.8,volume=100,side='ask'))
        self.assertEqual(r.position,0)
        r.trade(.2,dict(price=99.7,volume=1,side='ask'))
        self.assertEqual(r.position,1)
        self.assertEqual(r.fills[0]['price'],99.8)

    def test_self_aggression_and_preplacement_trade_excluded(self):
        r=engine()
        r.orders['ask']=dict(price=100.2,volume=2,queue=0,placed=.2)
        r.trade(.1,dict(price=100.3,volume=100,side='bid'))
        r.trade(.3,dict(price=100.3,volume=100,side='bid',own_aggressive=True))
        self.assertEqual(r.position,0)

    def test_repeated_book_cannot_refill_depth(self):
        r=engine()
        r.observe_book(0.,book(volume=2))
        self.assertEqual(r.sweep(.1,'bid',3,100.1,'test'),2)
        r.observe_book(.2,book(volume=2))
        self.assertEqual(r.sweep(.3,'bid',3,100.1,'test'),0)
        r.observe_book(.4,book(stamp=.4,volume=2))
        self.assertEqual(r.sweep(.5,'bid',3,100.1,'test'),2)

    def test_stale_and_reversed_books_cannot_execute(self):
        r=engine()
        r.observe_book(.5,book(stamp=.5))
        self.assertEqual(r.sweep(1.6,'bid',2,101.,'test'),0)
        r.observe_book(.7,book(stamp=.3))
        self.assertEqual(r.sweep(.8,'bid',2,101.,'test'),0)

    def test_latency_keeps_old_order_live_until_cancel(self):
        r=engine(latency=.25)
        r.orders['ask']=dict(price=100.2,volume=2,queue=0,placed=0.)
        r.schedule(.1,dict(kind='cancel'))
        r.run([(.2,'trade',dict(price=100.3,volume=1,side='bid')),
               (.4,'trade',dict(price=100.3,volume=1,side='bid'))])
        self.assertEqual(r.position,-1)
        self.assertEqual(len(r.orders),0)

    def test_partial_timeout_and_old_episode_request(self):
        r=engine()
        r.fill(0.,'bid',100.,3,'test')
        r.observe_book(.1,book(stamp=.1,volume=1))
        r.exiting=True
        episode=r.episode_id
        r.activate(.2,dict(kind='exit',episode=episode))
        self.assertEqual(r.position,1)
        self.assertTrue(r.exiting)
        r.fill(.3,'ask',100.,1,'test')
        r.fill(.4,'bid',100.,1,'test')
        r.activate(.5,dict(kind='exit',episode=episode))
        self.assertEqual(r.position,1)

    def test_zero_crossing_is_split_and_position_not_clipped(self):
        r=engine()
        r.fill(.1,'bid',100.,1,'test')
        r.fill(.2,'ask',101.,2,'test')
        self.assertEqual(r.position,-1)
        self.assertEqual(r.episodes[0]['pnl'],1.)
        r.fill(.3,'bid',100.5,1,'test')
        self.assertAlmostEqual(r.cash,1.5)
        self.assertAlmostEqual(sum(e['pnl'] for e in r.episodes),r.cash)

    def test_partial_order_retained_without_topup(self):
        r=engine()
        r.observe_book(0.,book())
        r.orders['bid']=dict(price=99.7,volume=1,queue=20,placed=0.)
        q=dict(bid_price=99.7,ask_price=100.3,buy_volume=2,sell_volume=0,reduce_only=False)
        r.activate(.1,dict(kind='quote',q=q))
        self.assertEqual(r.orders['bid']['volume'],1)
        self.assertEqual(r.orders['bid']['queue'],20)

    def test_capacity_accounts_for_soft_and_reduce_only(self):
        r=engine()
        r.position=99
        q=dict(buy_volume=5,sell_volume=200,reduce_only=False)
        self.assertEqual(r.capacity('bid',q),0)
        self.assertEqual(r.capacity('ask',dict(q,reduce_only=True)),99)
        r.position=-2
        self.assertEqual(r.capacity('bid',dict(q,reduce_only=True)),2)


if __name__=='__main__':
    unittest.main()
