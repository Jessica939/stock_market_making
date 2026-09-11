"""Baseline-refine pricing, used only for A."""
import math

POSITION_LIMIT = SOFT_LIMIT = ORDER_VOLUME = INVENTORY_SCALE = 100
INVENTORY_SKEW_TICKS = 2.0
SPREAD_MULTIPLIER = MIN_HALF_SPREAD_TICKS = 1.0
VWAP_HALF_LIFE_TICKS = 5

def calculate_quote(book, position, tick_size):
    """Inventory-aware prices and target remaining sizes, using external depth."""
    if not book or not book.bids or not book.asks:
        return None
    if not math.isfinite(tick_size) or tick_size <= 0:
        return None
    parameters = (INVENTORY_SCALE, INVENTORY_SKEW_TICKS, SPREAD_MULTIPLIER,
                  MIN_HALF_SPREAD_TICKS, VWAP_HALF_LIFE_TICKS)
    if (not all(math.isfinite(value) for value in parameters)
            or INVENTORY_SCALE <= 0 or SOFT_LIMIT <= 0 or SOFT_LIMIT > POSITION_LIMIT
            or INVENTORY_SKEW_TICKS < 0 or SPREAD_MULTIPLIER <= 0
            or MIN_HALF_SPREAD_TICKS <= 0 or VWAP_HALF_LIFE_TICKS <= 0):
        raise ValueError('Invalid inventory or spread settings')
    bids, asks = book.bids, book.asks
    if (any(not math.isfinite(level.price) or level.price <= 0
            or not math.isfinite(level.volume) or level.volume <= 0
            for level in (*bids, *asks))
            or bids[0].price >= asks[0].price):
        return None
    bid_weights = [level.volume * 0.5 ** (
        (bids[0].price - level.price) / tick_size / VWAP_HALF_LIFE_TICKS)
        for level in bids]
    ask_weights = [level.volume * 0.5 ** (
        (level.price - asks[0].price) / tick_size / VWAP_HALF_LIFE_TICKS)
        for level in asks]
    bid_vwap = sum(level.price * weight for level, weight in zip(bids, bid_weights)) / sum(bid_weights)
    ask_vwap = sum(level.price * weight for level, weight in zip(asks, ask_weights)) / sum(ask_weights)
    fair_value = (bid_vwap + ask_vwap) / 2

    inventory_adjustment = INVENTORY_SKEW_TICKS * tick_size * position / INVENTORY_SCALE
    center = fair_value - inventory_adjustment
    half_spread = max(MIN_HALF_SPREAD_TICKS * tick_size,
                      SPREAD_MULTIPLIER * (asks[0].price - bids[0].price) / 2)

    # Compete at the touch. On a two-tick spread both improvements would
    # meet: give the inventory-reducing side priority instead of self-crossing.
    bid_price = round(min(bids[0].price + tick_size, asks[0].price - tick_size), 10)
    ask_price = round(max(asks[0].price - tick_size, bids[0].price + tick_size), 10)
    if bid_price >= ask_price:
        if position > 0 or (position == 0 and center < (bids[0].price + asks[0].price) / 2):
            bid_price = bids[0].price
        else:
            ask_price = asks[0].price
    buy_volume = min(ORDER_VOLUME, max(0, POSITION_LIMIT - position))
    sell_volume = min(ORDER_VOLUME, max(0, POSITION_LIMIT + position))
    if position < 0:
        buy_volume = abs(position)
    elif position > 0:
        sell_volume = position
    if bid_price <= 0:
        buy_volume = 0
    if ask_price <= 0:
        sell_volume = 0
    return dict(fair_value=fair_value, center=center,
                bid_price=bid_price, ask_price=ask_price,
                buy_volume=buy_volume, sell_volume=sell_volume,
                inventory_adjustment=inventory_adjustment, half_spread=half_spread,
                book_scope='excluding_own_orders', reduce_bid=position < 0, reduce_ask=position > 0)
