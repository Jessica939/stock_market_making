import unittest
from guard import apply, signal


class GuardTests(unittest.TestCase):
    def quote(self,q=0,b=5,a=5):
        return dict(position=q,buy_volume=b,sell_volume=a,bid_price=159.8,ask_price=160.2)

    def test_preserves_reducing_portion_crossing_flat(self):
        q=self.quote(q=-2)
        out=apply(q,-.8)
        self.assertEqual(out['buy_volume'],3)  # cover 2 shorts + 1 of 3 new longs
        self.assertEqual(out['sell_volume'],5)
        self.assertEqual(q['buy_volume'],5)

    def test_direction_prices_and_existing_blocks(self):
        q=self.quote(b=0,a=2)
        out=apply(q,.8)
        self.assertEqual(out['buy_volume'],0)
        self.assertEqual(out['sell_volume'],1)
        self.assertEqual(out['bid_price'],q['bid_price'])
        self.assertEqual(out['ask_price'],q['ask_price'])
        self.assertEqual(apply(self.quote(b=1,a=1),-.8)['buy_volume'],1)

    def test_no_signal_is_no_op(self):
        q=self.quote()
        self.assertEqual(apply(q,None),q)
        self.assertEqual(apply(q,.599),q)

    def test_threshold_distance_and_walls(self):
        bids=[(100,200),(99.9,800),(99,20000)]
        asks=[(100.2,200),(100.3,200),(101,20000)]
        self.assertEqual(signal(bids,asks,.1),(1.,'ok'))
        self.assertIsNone(signal([(100,200)],[(100.2,200)],.1)[0])
        self.assertIsNone(signal(bids,asks,3)[0])
        self.assertIsNone(signal([(100,20000)],asks,.1)[0])

    def test_inventory_and_size_properties(self):
        for position in range(-100,101):
            for size in (0,1,2,5,20,100,200):
                q=self.quote(position,min(size,100-position),min(size,100+position))
                for strength in (-1,0,1):
                    out=apply(q,strength)
                    for key,reducing in [('buy_volume',max(0,-position)),('sell_volume',max(0,position))]:
                        self.assertLessEqual(out[key],q[key])
                        self.assertGreaterEqual(out[key],min(q[key],reducing))


if __name__=='__main__': unittest.main()
