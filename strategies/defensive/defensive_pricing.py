"""Independent PHILIPS market-making signals; no exchange calls or connection.

The book passed here must have our own displayed orders removed. Parameters
are engineering starting points, not fitted estimates of profitability.
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import math


@dataclass(frozen=True)
class Settings:
    position_limit: int = 100
    outstanding_limit: int = 200
    order_volume: int = 10
    soft_limit: int = 80
    panic_limit: int = 95
    panic_target: int = 70
    inventory_dead_zone: int = 30
    inventory_skew_ticks: float = 12.0
    inventory_exponent: float = 4.0
    obi_weight_ticks: float = 0.5
    tfi_weight_ticks: float = 0.5
    min_half_spread_ticks: float = 1.0
    depth_levels: int = 5
    reprice_tolerance_ticks: float = 2.0
    flow_window_seconds: float = 1.0
    toxic_flow_ratio: float = 0.8
    toxic_min_volume: int = 50
    toxic_move_ticks: float = 4.0
    cooldown_seconds: float = 2.0
    stale_book_seconds: float = 2.0
    panic_slippage_ticks: int = 3
    panic_retry_seconds: float = 0.5
    poll_seconds: float = 0.2

    def __post_init__(self):
        integers = ('position_limit', 'outstanding_limit', 'order_volume',
                    'soft_limit', 'panic_limit', 'panic_target',
                    'inventory_dead_zone', 'depth_levels', 'toxic_min_volume',
                    'panic_slippage_ticks')
        for name in integers:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f'{name} must be a nonnegative integer')
        if not (0 <= self.inventory_dead_zone < self.panic_target < self.soft_limit
                < self.panic_limit <= self.position_limit <= 100):
            raise ValueError('Require dead zone < panic target < soft < panic <= limit <= 100')
        if not (0 < self.order_volume <= self.position_limit
                and 2 * self.order_volume <= self.outstanding_limit <= 200
                and self.depth_levels > 0 and self.toxic_min_volume > 0):
            raise ValueError('Invalid order volume, depth, flow or outstanding limit')
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if not math.isfinite(value):
                raise ValueError(f'{name} must be finite')
        for name in ('inventory_skew_ticks', 'obi_weight_ticks', 'tfi_weight_ticks'):
            if getattr(self, name) < 0:
                raise ValueError(f'{name} cannot be negative')
        for name in ('inventory_exponent', 'min_half_spread_ticks',
                     'reprice_tolerance_ticks', 'flow_window_seconds',
                     'toxic_move_ticks', 'cooldown_seconds', 'stale_book_seconds',
                     'panic_retry_seconds'):
            if getattr(self, name) <= 0:
                raise ValueError(f'{name} must be positive')
        if not 0 < self.toxic_flow_ratio <= 1 or self.poll_seconds < 0.2:
            raise ValueError('Flow ratio must be in (0,1]; polling must be >= 0.2 seconds')
        if self.inventory_exponent > 50:
            raise ValueError('inventory_exponent must be <= 50')


def timestamp_seconds(value):
    """Optibook naive datetimes are interpreted as UTC; never as Windows local time."""
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def on_grid(price, tick, *, up=False):
    if not (math.isfinite(price) and math.isfinite(tick) and tick > 0):
        raise ValueError('Invalid price or tick')
    quantum = Decimal(str(tick))
    units = (Decimal(str(price)) / quantum).to_integral_value(
        rounding=ROUND_CEILING if up else ROUND_FLOOR)
    return float(units * quantum)


def valid_levels(levels, tick, *, descending):
    previous = math.inf if descending else -math.inf
    for level in levels or ():
        price, volume = level.price, level.volume
        if (isinstance(price, bool) or isinstance(volume, bool)
                or not math.isfinite(price) or price <= 0
                or not isinstance(volume, int) or volume <= 0
                or abs(price / tick - round(price / tick)) > 1e-6):
            return False
        if (descending and price >= previous) or (not descending and price <= previous):
            return False
        previous = price
    return True


def fresh_book(book, tick, wall_seconds, max_age):
    if book is None or not math.isfinite(tick) or tick <= 0:
        return False
    stamp = timestamp_seconds(getattr(book, 'timestamp', None))
    if stamp is None or not -0.5 <= wall_seconds - stamp <= max_age:
        return False
    return (valid_levels(book.bids, tick, descending=True)
            and valid_levels(book.asks, tick, descending=False)
            and not (book.bids and book.asks and book.bids[0].price >= book.asks[0].price))


def price_band(instrument, last_trade):
    """Conservative intersection of documented absolute/relative price constraints.

    The supplied API reference does not specify how the server combines them.
    Requiring BOTH is deliberately stricter than either common wider-band rule.
    No observed last trade + active limits means no new orders, not a made-up mid.
    """
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


def constrained_price(price, instrument, last_trade, *, side):
    band = price_band(instrument, last_trade)
    if band is None:
        return None
    tick = instrument.tick_size
    low = max(tick, on_grid(band[0], tick, up=True))
    high = on_grid(band[1], tick) if math.isfinite(band[1]) else math.inf
    if low > high:
        return None
    result = on_grid(min(high, max(low, price)), tick, up=side == 'ask')
    return result if 0 < result and low <= result <= high else None


@dataclass
class SignalState:
    flow: deque = field(default_factory=lambda: deque(maxlen=10000))
    mids: deque = field(default_factory=lambda: deque(maxlen=10000))
    seen_ids: set = field(default_factory=set)
    id_queue: deque = field(default_factory=deque)
    last_trade: float | None = None
    last_trade_timestamp: float = -math.inf
    last_book_timestamp: float = -math.inf
    blocked_until: dict = field(default_factory=lambda: {'bid': 0.0, 'ask': 0.0})


class SignalEngine:
    def __init__(self, settings=None):
        self.settings = settings or Settings()
        self.states = {}

    def state(self, instrument_id):
        return self.states.setdefault(instrument_id, SignalState())

    def observe_trades(self, instrument_id, trades, now, wall_seconds):
        state = self.state(instrument_id)
        for trade in trades:
            stamp = timestamp_seconds(getattr(trade, 'timestamp', None))
            price, volume = getattr(trade, 'price', None), getattr(trade, 'volume', None)
            if (stamp is None or stamp > wall_seconds + 0.5
                    or not isinstance(price, (float, int)) or isinstance(price, bool)
                    or not math.isfinite(price) or price <= 0
                    or isinstance(volume, bool) or not isinstance(volume, int) or volume <= 0):
                continue
            trade_id = getattr(trade, 'trade_id', None)
            key = ('id', trade_id) if trade_id is not None else (
                'fields', stamp, price, volume, getattr(trade, 'aggressor_side', None))
            if key in state.seen_ids:
                continue
            state.seen_ids.add(key)
            state.id_queue.append(key)
            while len(state.id_queue) > 20000:
                state.seen_ids.discard(state.id_queue.popleft())
            if stamp >= state.last_trade_timestamp:
                state.last_trade, state.last_trade_timestamp = float(price), stamp
            age = max(0.0, wall_seconds - stamp)
            side = getattr(trade, 'aggressor_side', None)
            if age <= self.settings.flow_window_seconds and side in ('bid', 'ask'):
                state.flow.append((now - age, volume if side == 'bid' else -volume))

    def quote(self, instrument_id, instrument, book, position, now, wall_seconds):
        cfg, state, tick = self.settings, self.state(instrument_id), instrument.tick_size
        if (not fresh_book(book, tick, wall_seconds, cfg.stale_book_seconds)
                or not book.bids or not book.asks):
            return None
        if isinstance(position, bool) or not isinstance(position, int):
            raise ValueError('Position must be an integer')
        bid, ask = book.bids[0].price, book.asks[0].price
        mid = (bid + ask) / 2
        book_stamp = timestamp_seconds(book.timestamp)
        new_book = book_stamp > state.last_book_timestamp
        if book_stamp < state.last_book_timestamp:
            return None
        if new_book:
            state.last_book_timestamp = book_stamp
            state.mids.append((now - max(0.0, wall_seconds - book_stamp), mid))
        # Filter all items: arrival order need not match exchange timestamps.
        state.flow = deque((item for item in state.flow
                            if now - item[0] < cfg.flow_window_seconds), maxlen=10000)
        state.mids = deque((item for item in state.mids
                            if now - item[0] < cfg.flow_window_seconds), maxlen=10000)
        buy_depth = sum(level.volume / (1 + index)
                        for index, level in enumerate(book.bids[:cfg.depth_levels]))
        sell_depth = sum(level.volume / (1 + index)
                         for index, level in enumerate(book.asks[:cfg.depth_levels]))
        obi = (buy_depth - sell_depth) / (buy_depth + sell_depth)
        flow_volume = sum(abs(volume) for _, volume in state.flow)
        tfi = sum(volume for _, volume in state.flow) / flow_volume if flow_volume else 0.0
        move = (mid - state.mids[0][1]) / tick if state.mids else 0.0
        # Repeated observations may extend a cooldown only while fresh toxic
        # evidence remains inside the one-second signal window.
        for side, sign in (('bid', -1), ('ask', 1)):
            if ((flow_volume >= cfg.toxic_min_volume and sign * tfi >= cfg.toxic_flow_ratio)
                    or (new_book and sign * move >= cfg.toxic_move_ticks)):
                state.blocked_until[side] = now + cfg.cooldown_seconds
        fair = mid + tick * (cfg.obi_weight_ticks * obi + cfg.tfi_weight_ticks * tfi)
        utilization = min(1.0, max(0.0, (abs(position) - cfg.inventory_dead_zone)
                                  / (cfg.position_limit - cfg.inventory_dead_zone)))
        skew = (math.copysign(1, position) * cfg.inventory_skew_ticks * tick
                * math.expm1(cfg.inventory_exponent * utilization)
                / math.expm1(cfg.inventory_exponent))
        center = fair - skew
        half_spread = max(tick * cfg.min_half_spread_ticks, (ask - bid) / 2)
        buy_price = constrained_price(min(center - half_spread, ask - tick),
                                      instrument, state.last_trade, side='bid')
        sell_price = constrained_price(max(center + half_spread, bid + tick),
                                       instrument, state.last_trade, side='ask')
        volumes = {}
        for side, signed_position in (('bid', position), ('ask', -position)):
            scale = max(0.0, 1.0 - max(0, signed_position) / cfg.soft_limit)
            volumes[side] = min(math.ceil(cfg.order_volume * scale),
                                max(0, cfg.position_limit - signed_position))
            if signed_position >= cfg.soft_limit or state.blocked_until[side] > now:
                volumes[side] = 0
        # Band clamping must not accidentally turn a passive quote marketable.
        if buy_price is None or buy_price >= ask:
            volumes['bid'] = 0
        if sell_price is None or sell_price <= bid:
            volumes['ask'] = 0
        if buy_price is not None and sell_price is not None and buy_price >= sell_price:
            return None
        return dict(
            desired={'bid': (buy_price, volumes['bid']), 'ask': (sell_price, volumes['ask'])},
            fair_value=fair, center=center, bid_price=buy_price, ask_price=sell_price,
            buy_volume=volumes['bid'], sell_volume=volumes['ask'],
            inventory_adjustment=skew, half_spread=half_spread, obi=obi, tfi=tfi,
            flow_volume=flow_volume, move_ticks=move,
            blocked_bid=state.blocked_until['bid'] > now,
            blocked_ask=state.blocked_until['ask'] > now,
            book_scope='excluding_own_orders',
        )

    def panic_order(self, instrument_id, instrument, book, position, now, wall_seconds):
        """Bounded IOC reaching at most configured ticks beyond current best.

        Works with just the side required to exit. Executor must cancel every
        own order and recheck actual exposure before submitting this proposal.
        """
        cfg, tick = self.settings, instrument.tick_size
        if not fresh_book(book, tick, wall_seconds, cfg.stale_book_seconds):
            return None
        if abs(position) <= cfg.panic_target:
            return None
        side = 'ask' if position > 0 else 'bid'
        levels = book.bids if side == 'ask' else book.asks
        if not levels:
            return None
        sign = -1 if side == 'ask' else 1
        slippage_edge = on_grid(levels[0].price + sign * cfg.panic_slippage_ticks * tick,
                                tick, up=side == 'ask')
        price = constrained_price(slippage_edge,
                                  instrument, self.state(instrument_id).last_trade, side=side)
        if price is None:
            return None
        if (side == 'ask' and price < slippage_edge) or (side == 'bid' and price > slippage_edge):
            return None  # Never let a price band widen our independent slippage budget.
        reachable = sum(level.volume for level in levels
                        if (level.price >= price if side == 'ask' else level.price <= price))
        volume = min(cfg.order_volume, abs(position) - cfg.panic_target, reachable)
        if volume <= 0:
            return None
        return dict(side=side, price=price, volume=volume)
