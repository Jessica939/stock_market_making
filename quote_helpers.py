"""Pure book helpers; no connection or order submission."""

from collections import defaultdict
from collections.abc import Mapping
import math
from types import SimpleNamespace


def external_price_book(book, orders, tick_size):
    """Subtract visible own resting volume without changing the input snapshot.

    Price book and private orders are sequential reads. If an own order is not
    present in this book (or exceeds displayed volume), the pair is inconsistent:
    return None and let the strategy cancel/skip until it has a usable snapshot.
    An own-only side becomes empty, never a synthetic external quote.
    """
    if (book is None or not isinstance(orders, Mapping)
            or not math.isfinite(tick_size) or tick_size <= 0):
        return None
    own = defaultdict(int)
    for order in orders.values():
        if (order.side not in ('bid', 'ask') or not math.isfinite(order.price)
                or order.price <= 0 or order.volume < 0):
            return None
        own[order.side, round(order.price / tick_size)] += order.volume
    sides = {}
    for side, levels in (('bid', book.bids), ('ask', book.asks)):
        external = []
        for level in levels or ():
            if not math.isfinite(level.price) or level.price <= 0 or level.volume < 0:
                return None
            key = side, round(level.price / tick_size)
            remaining = level.volume - own.pop(key, 0)
            if remaining < 0:
                return None
            if remaining > 0:
                external.append(SimpleNamespace(price=level.price, volume=remaining))
        sides[side] = external
    if any(own.values()):
        return None
    return SimpleNamespace(timestamp=getattr(book, 'timestamp', None),
                           instrument_id=getattr(book, 'instrument_id', None),
                           bids=sides['bid'], asks=sides['ask'])
