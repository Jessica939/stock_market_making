"""Competitive B prices. CyclePosition owns direction, size and exits."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ProtectionSettings:
    max_increasing_volume: int = 100

    def __post_init__(self):
        if type(self.max_increasing_volume) is not int or not 1 <= self.max_increasing_volume <= 100:
            raise ValueError('max_increasing_volume must be an integer in 1..100')


def competitive_price(book, tick, side):
    """Improve the external same-side touch by one tick.

    A one-tick spread has no inside grid point: the improved limit takes the
    opposite touch. The B controller emits only one side at a time.
    """
    if side == 'bid':
        return round(min(book.asks[0].price, book.bids[0].price + tick), 10)
    return round(max(tick, book.bids[0].price, book.asks[0].price - tick), 10)


def protect_quote(quote, book, position, tick, instrument_id, settings):
    if quote is None or instrument_id != 'PHILIPS_B':
        return quote
    out = dict(quote)
    active = out.get('cycle', {}).get('active') is True
    out['reduce_only'] = not active
    out['bid_price'] = competitive_price(book, tick, 'bid')
    out['ask_price'] = competitive_price(book, tick, 'ask')
    out['buy_volume'] = abs(position) if position < 0 else settings.max_increasing_volume if active else 0
    out['sell_volume'] = position if position > 0 else settings.max_increasing_volume if active else 0
    out['protection'] = dict(mode='competitive_entry' if active else 'reduce_only')
    return out
