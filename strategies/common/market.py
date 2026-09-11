"""Public market-data interface shared by both active strategies."""
from collections.abc import Mapping
import math

from .depth_guard import (
    BoundaryTracker, UnusableBook, clean_book, execution_book,
    ioc_plan, timestamp_seconds, validate_market_config,
)


def price_band(instrument, reference):
    """Return the conservative intersection of documented price-change bounds."""
    limits = getattr(instrument, 'price_change_limit', None)
    if limits is None:
        return (-math.inf, math.inf)
    if (isinstance(reference, bool) or not isinstance(reference, (int, float))
            or not math.isfinite(reference) or reference <= 0):
        return None
    widths = []
    for name in ('absolute_change', 'relative_change'):
        value = limits.get(name) if isinstance(limits, Mapping) else getattr(limits, name, None)
        if value is None:
            continue
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0):
            return None
        width = value * reference if name == 'relative_change' else value
        if not math.isfinite(width):
            return None
        widths.append(width)
    if not widths:
        return None
    width = min(widths)
    return reference - width, reference + width


__all__ = ['BoundaryTracker', 'UnusableBook', 'clean_book', 'execution_book',
           'ioc_plan', 'price_band', 'timestamp_seconds', 'validate_market_config']
