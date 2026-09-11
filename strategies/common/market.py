"""Public market-data interface shared by the active strategies."""
import math

from .depth_guard import (
    BoundaryTracker, UnusableBook, clean_book, execution_book,
    ioc_plan, timestamp_seconds, validate_market_config,
)

__all__ = ['BoundaryTracker', 'UnusableBook', 'clean_book', 'execution_book',
           'ioc_plan', 'price_band', 'timestamp_seconds', 'validate_market_config']


def price_band(instrument, last_trade):
    """Return the conservative intersection of exchange price constraints."""
    limit = getattr(instrument, 'price_change_limit', None)
    if limit is None:
        return (0.0, math.inf)
    if last_trade is None or not math.isfinite(last_trade) or last_trade <= 0:
        return None
    widths = []
    for name, scale in (('absolute_change', 1.0), ('relative_change', last_trade)):
        value = getattr(limit, name, None)
        if value is not None:
            if isinstance(value, bool) or not math.isfinite(value) or value < 0:
                return None
            widths.append(value * scale)
    if not widths:
        return None
    width = min(widths)
    return (max(0.0, last_trade - width), last_trade + width)
