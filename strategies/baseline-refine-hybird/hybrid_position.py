"""One B inventory: cycle entries/exits plus a bounded market-making band."""
from dataclasses import dataclass
import math

from .cycle_position import CyclePosition


@dataclass(frozen=True)
class MarketMakingSettings:
    order_volume: int = 20
    inventory_band_lots: int = 20

    def __post_init__(self):
        for value in (self.order_volume, self.inventory_band_lots):
            if type(value) is not int or not 1 <= value <= 100:
                raise ValueError('market-making sizes must be integers in 1..100')


class HybridPosition(CyclePosition):
    """Coordinate quotes against actual total B position, without virtual books.

    Idle MM trades around zero. A cycle retains its direction and frozen exit
    rules; MM recycles at most a band of its observed inventory. After the entry
    window, MM can replenish only as far as the largest observed cycle position.
    Startup inventory and latched exits always take precedence over MM.
    """

    def __init__(self, *, market_making=None, **position_settings):
        super().__init__(**position_settings)
        self.market_making = market_making or MarketMakingSettings()
        if not isinstance(self.market_making, MarketMakingSettings):
            raise ValueError('market_making must be MarketMakingSettings')
        self.started_flat = False

    def apply(self, quote, book, position, tick, now, *, maker_quote=None):
        if maker_quote is None:
            raise ValueError('hybrid controller requires a passive maker quote')
        cfg = self.market_making
        signal = quote.get('cycle', {})
        if not position:
            self.started_flat = True

        # Only inventory accumulated after observing a flat account can be MM.
        # Finish any prior recovery even if its residual now fits the idle band.
        idle_inventory = (self.started_flat and self.active is None
                          and self.untracked_exit_started is None
                          and abs(position) <= cfg.inventory_band_lots)
        if idle_inventory and position and now >= self.next_entry:
            self.active = self._entry_plan(book, tick, now, signal)
            if self.active and position * self.active['sign'] < 0:
                self.active.update(exit_reason='cycle_rebalance', exit_started=now)

        if idle_inventory and position and self.active is None:
            out = dict(quote, buy_volume=0, sell_volume=0, reduce_only=False,
                       cycle_position=dict(reason='market_making', target_position=0))
        else:
            out = super().apply(quote, book, position, tick, now)

        # Keep the full-position, persistent exit and cooldown unchanged.
        if out.get('reduce_only') or now < self.next_entry:
            out['market_making'] = dict(active=False, reason='exit_or_cooldown')
            return out

        held = self.active
        if held:
            peak = held['mm_inventory_peak'] = max(
                abs(position), held.get('mm_inventory_peak', 0))
            ceiling = self.target_lots if now < held['build_until'] else peak
            floor = max(1, peak - cfg.inventory_band_lots) if peak else 0
            lower, upper = ((floor, ceiling) if held['sign'] > 0
                            else (-ceiling, -floor))
            can_increase = bool(signal.get('active') and
                                held['sign'] * signal.get('predicted_B_change', 0) > 0)
        else:
            lower, upper = -cfg.inventory_band_lots, cfg.inventory_band_lots
            # An unavailable fit alone still permits ordinary MM. Shocks and
            # invalid pair data retain the baseline's protection against adding.
            can_increase = signal.get('reason') not in {
                'invalid_clock', 'clock_reversal', 'invalid_pair_books',
                'unsynchronized_books', 'tick_change', 'book_time_reversal',
                'wide_pair_spread', 'residual_shock',
            }
        can_increase = can_increase and not maker_quote.get('reduce_only', False)
        buy = min(cfg.order_volume, max(0, upper-position))
        sell = min(cfg.order_volume, max(0, position-lower))
        if not can_increase:
            buy = min(buy, max(0, -position))
            sell = min(sell, max(0, position))

        # The target is a band shared by both sides, not two independent +/-100
        # allowances. QuoteManager rechecks these bounds using fresh positions.
        out.pop('target_position', None)
        if out['cycle_position']['reason'] == 'build_cycle_position':
            out['replenish_side'] = 'bid' if held['sign'] > 0 else 'ask'
        out.update(min_position=lower, max_position=upper,
                   reduce_bid=not can_increase and position < 0,
                   reduce_ask=not can_increase and position > 0)
        for side, key, volume in (('bid', 'buy_volume', buy), ('ask', 'sell_volume', sell)):
            if out[key] == 0 and volume:
                out[side+'_price'] = maker_quote[side+'_price']
            out[key] = max(out[key], volume)

        # A one/two-tick spread can make the aggressive cycle entry meet its
        # opposing MM quote. Keep the entry and move MM back to the outer touch.
        if out['buy_volume'] and out['sell_volume'] and out['bid_price'] >= out['ask_price']:
            if held and held['sign'] > 0:
                out['ask_price'] = round(max(book.asks[0].price, out['bid_price']+tick), 10)
            elif held:
                out['bid_price'] = round(min(book.bids[0].price, out['ask_price']-tick), 10)
            else:
                out['bid_price'], out['ask_price'] = book.bids[0].price, book.asks[0].price
        if out['bid_price'] <= 0:
            out['buy_volume'] = 0
        out['market_making'] = dict(active=bool(buy or sell),
            reason='cycle_band' if held else 'idle_band',
            order_volume=cfg.order_volume, inventory_band_lots=cfg.inventory_band_lots,
            min_position=lower, max_position=upper, can_increase=can_increase,
            next_entry=self.next_entry if math.isfinite(self.next_entry) else None)
        return out
