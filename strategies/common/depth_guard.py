"""Bounded signal depth and independent price protection for real execution.

Large end-of-book orders are observable liquidity, not identified counterparties.
They may be excluded from signals without pretending they cannot actually trade.
"""
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
import math
from statistics import median


class UnusableBook(ValueError):
    pass


def _number(value, name, *, positive=False, nonnegative=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or (positive and value <= 0)
            or (nonnegative and value < 0)):
        raise UnusableBook(f'invalid {name}')
    return value


def _settings(config):
    if not isinstance(config, Mapping):
        raise UnusableBook('config must be a mapping')
    defaults = dict(max_book_age_seconds=2.0, max_spread_ticks=20,
                    boundary_volume=20000, boundary_volume_tolerance=0.25,
                    boundary_min_distance_ticks=5, max_depth_ticks=4,
                    max_depth_levels=5, feature_volume_cap=100,
                    boundary_outlier_ratio=20, boundary_outlier_min_volume=1000,
                    boundary_memory_seconds=30, max_reference_deviation_ticks=20)
    cfg = {key: config.get(key, default) for key, default in defaults.items()}
    for key, value in cfg.items():
        _number(value, key, positive=key != 'boundary_volume_tolerance',
                nonnegative=key == 'boundary_volume_tolerance')
    if not 0 <= cfg['boundary_volume_tolerance'] < 1:
        raise UnusableBook('boundary_volume_tolerance must be in [0,1)')
    for key in ('max_depth_levels', 'feature_volume_cap', 'boundary_volume',
                'boundary_outlier_min_volume'):
        if not isinstance(cfg[key], int):
            raise UnusableBook(f'{key} must be an integer')
    if cfg['boundary_min_distance_ticks'] <= 1:
        raise UnusableBook('boundary_min_distance_ticks must exceed one tick')
    return cfg


def timestamp_seconds(value):
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if isinstance(value, datetime):
            # UTC is the explicit adapter convention for naive SDK timestamps.
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            value = value.timestamp()
        return float(_number(value, 'exchange timestamp'))
    except (ValueError, TypeError, OverflowError) as exc:
        raise UnusableBook('missing or invalid exchange timestamp') from exc


def validate_market_config(config):
    """Validate before connecting, not on the first live book after team takeover."""
    return _settings(config)


def _levels(raw, side, tick, *, allow_empty=False):
    levels = raw.get(side) if isinstance(raw, Mapping) else getattr(raw, side, None)
    result = []
    try:
        for level in levels or ():
            if isinstance(level, (list, tuple)):
                price, volume = level
            else:
                price, volume = level.price, level.volume
            _number(price, 'level price', positive=True)
            _number(volume, 'level volume', nonnegative=True)
            if int(volume) != volume or abs(price / tick - round(price / tick)) > 1e-6:
                raise UnusableBook('off-grid price or non-integer volume')
            if volume:
                result.append((float(price), int(volume)))
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise UnusableBook('malformed price/volume level') from exc
    if not result and not allow_empty:
        raise UnusableBook('empty side')
    prices = [p for p, _ in result]
    if prices != sorted(set(prices), reverse=side == 'bids'):
        raise UnusableBook('unsorted or duplicated prices')
    return result


class BoundaryTracker:
    """Bounded suspicious-price memory; make one instance per instrument.

    A partially filled wall continuously present at the same price remains
    suspect. A disappeared price is retired on the next snapshot: moving walls
    must not leave ghosts that later block the ordinary touch. Thirty seconds
    caps memory across observation gaps. This is not external order identity.
    """
    def __init__(self):
        self.levels = {}
        self.last_stamp = -math.inf
        self.tick = None

    def identify(self, sides, tick, stamp, cfg):
        if stamp < self.last_stamp:
            raise UnusableBook('out-of-order boundary snapshot')
        if self.tick is not None and self.tick != tick:
            self.levels.clear()
        self.tick, self.last_stamp = tick, stamp
        present = {(side, round(price / tick))
                   for side, levels in sides.items() for price, _ in levels}
        self.levels = {key: last for key, last in self.levels.items()
                       if key in present and stamp - last <= cfg['boundary_memory_seconds']}
        found = {}
        for side, levels in sides.items():
            if not levels:
                continue
            radius = min(cfg['max_depth_ticks'], cfg['boundary_min_distance_ticks'] - 1)
            near = [(p, v) for p, v in levels if abs(p - levels[0][0]) / tick <= radius + 1e-8]
            # Outlier classification itself must not be influenced by far tails.
            ordinary = [v for _, v in near if v <= cfg['feature_volume_cap'] * 10]
            scale = min(cfg['feature_volume_cap'], median(ordinary)) if ordinary else cfg['feature_volume_cap']

            def large(volume):
                return (volume >= cfg['boundary_volume'] * (1 - cfg['boundary_volume_tolerance'])
                        or (volume >= cfg['boundary_outlier_min_volume']
                            and volume >= cfg['boundary_outlier_ratio'] * scale))

            supported_touch = any(not large(v) for _, v in near[1:])
            for index, (price, volume) in enumerate(levels):
                key = (side, round(price / tick))
                remembered = key in self.levels
                # A newly observed huge best with supporting depth may be real
                # near liquidity. Do not discard that touch on quantity alone.
                suspect = remembered or (large(volume) and (index > 0 or not supported_touch))
                if suspect:
                    self.levels[key] = stamp
                    found[key] = 'remembered_boundary' if remembered else 'large_tail_or_isolated_level'
        if len(self.levels) > 512:
            self.levels = dict(sorted(self.levels.items(), key=lambda item: item[1])[-512:])
        return found


def clean_book(raw, tick, now_epoch, config, *, for_execution=False,
               reference_price=None, boundary_tracker=None):
    """Separate near-core features from full execution depth; never invent a mid."""
    cfg = _settings(config)
    _number(tick, 'tick size', positive=True)
    _number(now_epoch, 'wall clock')
    if raw is None:
        raise UnusableBook('missing book')
    if reference_price is not None:
        _number(reference_price, 'trusted reference', positive=True)
    stamp = timestamp_seconds(raw.get('timestamp', raw.get('book_timestamp'))
                              if isinstance(raw, Mapping) else getattr(raw, 'timestamp', None))
    age = now_epoch - stamp
    if age > cfg['max_book_age_seconds'] or age < -1.0:
        raise UnusableBook('stale book or clock disagreement')
    sides = {side: _levels(raw, key, tick, allow_empty=True)
             for side, key in (('bid', 'bids'), ('ask', 'asks'))}
    if not any(sides.values()):
        raise UnusableBook('empty book')
    if sides['bid'] and sides['ask'] and sides['bid'][0][0] >= sides['ask'][0][0]:
        raise UnusableBook('locked/crossed book')
    tracker = boundary_tracker if boundary_tracker is not None else BoundaryTracker()
    suspect = tracker.identify(sides, tick, stamp, cfg)
    walls, core, reasons = [], {}, []
    # Price distance is restricted before rank, even if the book has few levels.
    radius = min(cfg['max_depth_ticks'], cfg['boundary_min_distance_ticks'] - 1)
    for side, levels in sides.items():
        core[side] = []
        if not levels:
            reasons.append(f'empty_{side}')
            continue
        best = levels[0][0]
        for index, (price, volume) in enumerate(levels):
            key = side, round(price / tick)
            distance = abs(price - best) / tick
            if key in suspect:
                walls.append(dict(side=side, price=price, volume=volume,
                                  reason=suspect[key], distance_ticks=distance))
                if index == 0:
                    reasons.append(f'suspected_boundary_at_{side}_touch')
                continue
            if distance <= radius + 1e-8 and len(core[side]) < cfg['max_depth_levels']:
                core[side].append((price, volume))
        if not core[side]:
            reasons.append(f'no_normal_{side}_core')
    bid = sides['bid'][0][0] if sides['bid'] else None
    ask = sides['ask'][0][0] if sides['ask'] else None
    mid = bid + (ask - bid) / 2 if bid is not None and ask is not None else None
    if mid is not None and (ask - bid) / tick > cfg['max_spread_ticks'] + 1e-8:
        reasons.append('spread_too_wide')
    if reference_price is not None:
        limit = cfg['max_reference_deviation_ticks'] * tick
        if any(abs(price - reference_price) > limit + 1e-8 for price in (bid, ask) if price is not None):
            reasons.append('touch_too_far_from_trusted_reference')
    usable = not reasons
    if not usable and not for_execution:
        raise UnusableBook(', '.join(reasons))
    imbalance = microprice = None
    if usable:
        cap = cfg['feature_volume_cap']
        wb = sum(min(v, cap) * 0.5 ** ((bid - p) / tick / 3) for p, v in core['bid'])
        wa = sum(min(v, cap) * 0.5 ** ((p - ask) / tick / 3) for p, v in core['ask'])
        qb, qa = min(core['bid'][0][1], cap), min(core['ask'][0][1], cap)
        imbalance = (wb - wa) / (wb + wa)
        microprice = bid + (ask - bid) * (qb / (qb + qa))
    return dict(bid=bid, ask=ask, mid=mid if usable else None, tick=tick,
                bids=core['bid'], asks=core['ask'],
                execution_bids=sides['bid'], execution_asks=sides['ask'],
                imbalance=imbalance, microprice=microprice, timestamp=stamp,
                boundary_levels=walls, age_seconds=age, signal_usable=usable,
                quality_reasons=reasons, feature_radius_ticks=radius,
                execution_reference=reference_price,
                max_reference_deviation_ticks=cfg['max_reference_deviation_ticks'])


def ioc_plan(book, side, requested, slippage_ticks):
    """Use real lots with both current-touch and independent reference bounds."""
    if side not in ('bid', 'ask'):
        raise UnusableBook('invalid IOC side')
    if isinstance(requested, bool) or not isinstance(requested, int) or requested <= 0:
        raise UnusableBook('IOC volume must be a positive integer')
    _number(slippage_ticks, 'IOC slippage', nonnegative=True)
    tick = _number(book.get('tick'), 'IOC tick', positive=True)
    key = 'asks' if side == 'bid' else 'bids'
    levels = _levels({key: book.get('execution_' + key, book.get(key))}, key, tick)
    degraded = book.get('signal_usable', True) is False
    reference = book.get('execution_reference')
    if degraded and reference is None:
        raise UnusableBook('degraded depth has no fresh independent exit reference')
    if reference is not None:
        _number(reference, 'execution reference', positive=True)
    max_reference = _number(book.get('max_reference_deviation_ticks', 20),
                            'reference deviation', positive=True)
    bound_ticks = Decimal(str(levels[0][0])) / Decimal(str(tick))
    bound_ticks += (1 if side == 'bid' else -1) * Decimal(str(slippage_ticks))
    quantity, worst = 0, levels[0][0]
    for price, volume in levels:
        units = Decimal(str(price)) / Decimal(str(tick))
        if (side == 'bid' and units > bound_ticks) or (side == 'ask' and units < bound_ticks):
            break
        if reference is not None and abs(price - reference) / tick > max_reference + 1e-8:
            break  # Do not shift the slippage origin to a newly exposed far wall.
        take = min(requested - quantity, volume)
        quantity += take
        if take:
            worst = price
        if quantity == requested:
            break
    return worst, quantity


def execution_book(raw, tick, now_epoch, config, side, *, reference_price=None,
                   boundary_tracker=None):
    """Backward-compatible reducing view; retain actual one-sided liquidity."""
    if side not in ('bid', 'ask'):
        raise UnusableBook('invalid reducing side')
    book = clean_book(raw, tick, now_epoch, config, for_execution=True,
                      reference_price=reference_price, boundary_tracker=boundary_tracker)
    needed = 'asks' if side == 'bid' else 'bids'
    if not book['execution_' + needed]:
        raise UnusableBook('missing required liquidation side')
    return book
