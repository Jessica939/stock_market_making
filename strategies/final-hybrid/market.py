"""Validated external depth and edge sizing from stale_quote_sniping."""
from datetime import datetime, timezone
import math

class UnusableBook(ValueError):
    """No usable execution opportunity in the current snapshot."""

def book_time(book):
    stamp = getattr(book, 'timestamp', None)
    if not isinstance(stamp, datetime):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()

def bounded_book(raw, tick, wall, config):
    stamp = book_time(raw)
    if stamp is None or not 0 <= wall - stamp <= config["max_book_age_seconds"]:
        raise UnusableBook("missing or stale book")
    sides = {}
    for name in ("bids", "asks"):
        levels = getattr(raw, name, None)
        if not levels:
            raise UnusableBook("two-sided book required")
        prior = math.inf if name == "bids" else -math.inf
        clean = []
        for level in levels:
            price, volume = level.price, level.volume
            if (isinstance(price, bool) or not isinstance(price, (int, float))
                    or not math.isfinite(price) or price <= 0
                    or isinstance(volume, bool) or not isinstance(volume, int) or volume <= 0
                    or abs(price / tick - round(price / tick)) > 1e-6
                    or (price >= prior if name == "bids" else price <= prior)):
                raise UnusableBook("invalid price levels")
            clean.append((price, volume))
            prior = price
        sides[name] = clean
    bid, ask = sides["bids"][0][0], sides["asks"][0][0]
    if bid >= ask or ask - bid > config["max_spread_ticks"] * tick + 1e-9:
        raise UnusableBook("crossed or excessively wide book")
    for name, levels in sides.items():
        reserve = config["depth_reserve_lots"]
        touch = levels[0][0]
        bounded = []
        for price, volume in levels:
            if abs(price - touch) > config["max_sweep_ticks"] * tick + 1e-9:
                break
            removed = min(reserve, volume)
            reserve -= removed
            volume -= removed
            if volume:
                bounded.append((price, volume))
        sides[name] = bounded
    return dict(**sides, execution_bids=sides["bids"], execution_asks=sides["asks"],
                bid=bid, ask=ask, mid=(bid + ask) / 2, tick=tick, timestamp=stamp,
                signal_usable=True)

def vwap(book, buy, quantity):
    left = quantity
    total = 0.0
    for price, volume in book["asks" if buy else "bids"]:
        take = min(left, volume)
        total += price * take
        left -= take
        if left == 0:
            return total / quantity
    raise UnusableBook("insufficient bounded depth")

def scaled_entry_size(edge, tick, config):
    """Scale only the excess edge above the entry floor, up to a hard cap."""
    net_edge = edge - 2 * config["fee_per_lot"]
    excess_ticks = max(0.0, net_edge / tick - config["entry_edge_ticks"])
    steps = math.floor((excess_ticks + 1e-9) / config["size_step_ticks"])
    return min(config["max_order_lots"],
               config["order_lots"] + steps * config["lots_per_step"])

def sized_execution(book, buy, fair, config, max_quantity=None):
    """Find a size whose own VWAP still justifies its edge-based size tier."""
    sign = 1 if buy else -1
    base = config["order_lots"]
    execution = vwap(book, buy, base)
    edge = sign * (fair - execution)
    quantity = scaled_entry_size(edge, book["tick"], config)
    if max_quantity is not None:
        quantity = min(quantity, max_quantity)
    available = sum(volume for _, volume in book["asks" if buy else "bids"])
    quantity = min(quantity, available)
    while quantity > base:
        execution = vwap(book, buy, quantity)
        edge = sign * (fair - execution)
        justified = scaled_entry_size(edge, book["tick"], config)
        if justified >= quantity:
            break
        quantity = max(base, justified)
    execution = vwap(book, buy, quantity)
    return quantity, execution, sign * (fair - execution)

def cap_depth(levels, quantity):
    capped = []
    left = quantity
    for price, volume in levels:
        take = min(left, volume)
        if take:
            capped.append((price, take))
            left -= take
        if not left:
            break
    return capped
