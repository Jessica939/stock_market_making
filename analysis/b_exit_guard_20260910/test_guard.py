import unittest
from guard import apply
from attribution import fill_impact


def quote(position=2):
    return dict(instrument='PHILIPS_B',position=position,center=160.,
        bid_price=159.8,ask_price=160.2,buy_volume=2,sell_volume=2,
        best_bid=159.9,best_ask=160.1,cycle={'active':True},reduce_only=False)


class Tests(unittest.TestCase):
    def test_improved_execution_is_not_a_quote_change(self):
        r=fill_impact('ask',160.2,160.2,160.5,2,2)
        self.assertFalse(r['repriced'])
        self.assertFalse(r['changed'])

    def test_repriced_order_can_still_allow_original_fill(self):
        for side,old,new,execution,pos in [('ask',160.2,160.3,160.5,2),('bid',159.8,159.7,159.5,-2)]:
            r=fill_impact(side,old,new,execution,pos,2)
            self.assertTrue(r['repriced'])
            self.assertFalse(r['original_execution_blocked'])
            self.assertFalse(r['changed'])

    def test_partial_reductions_excluded_from_pure_exit_cohort(self):
        r=fill_impact('ask',160.2,160.3,160.2,1,2)
        self.assertEqual(r['reducing_volume'],1)
        self.assertFalse(r['fully_reducing'])
        self.assertFalse(r['changed'])
        self.assertTrue(fill_impact('ask',160.2,160.3,160.2,2,2)['changed'])

    def test_only_reducing_price_changes(self):
        for pos,side,target in [(2,'ask',160.3),(-2,'bid',159.7)]:
            q=quote(pos); c=apply(q)
            self.assertEqual(c[side+'_price'],target)
            for key in q:
                if key!=side+'_price': self.assertEqual(c[key],q[key])
            self.assertEqual(q['ask_price'],160.2)

    def test_fallback_and_large_inventory_unchanged(self):
        for pos in (0,11,-11,100,-100):
            q=quote(pos); self.assertEqual(apply(q),q)
        for field,value in [('instrument','PHILIPS_A'),('cycle',{'active':False}),('reduce_only',True),('sell_volume',0)]:
            q=quote(); q[field]=value; self.assertEqual(apply(q),q)

    def test_never_tightens_wide_quotes(self):
        for pos in (2,-2):
            q=quote(pos); q.update(bid_price=159.,ask_price=161.)
            self.assertEqual(apply(q),q)

    def test_boundary_and_rounding(self):
        for pos in (-10,10):
            q=quote(pos); q['center']=160.037
            c=apply(q)
            self.assertGreaterEqual(c['ask_price'],q['ask_price'])
            self.assertLessEqual(c['bid_price'],q['bid_price'])
            key='ask_price' if pos>0 else 'bid_price'
            self.assertAlmostEqual(c[key]*10,round(c[key]*10))


if __name__=='__main__': unittest.main()
