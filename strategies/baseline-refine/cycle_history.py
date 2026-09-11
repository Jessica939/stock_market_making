"""Read recent history without consuming the recorder's public trade stream."""
import csv
from datetime import datetime, timezone
import math
from pathlib import Path

SYMBOLS = ('PHILIPS_A', 'PHILIPS_B')


def _stamp(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise ValueError('history timestamp must be a datetime')
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def price_rows(path, settings, ticks, wall):
    """Pair recorder midpoints by sample ID, validating their original times."""
    pairs = {}
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        for row in csv.DictReader(stream):
            try:
                symbol = row['instrument_id']
                if symbol not in SYMBOLS or row['status'] != 'ok':
                    continue
                observed, stamp = _stamp(row['observed_at_utc']), _stamp(row['book_timestamp'])
                bid, ask = float(row['best_bid']), float(row['best_ask'])
                tick = ticks[symbol]
                if (not all(math.isfinite(v) for v in (bid, ask, tick)) or tick <= 0
                        or not wall-settings.history_seconds <= observed <= wall
                        or not 0 <= observed-stamp <= settings.max_age_seconds
                        or not 0 < bid < ask or ask-bid > settings.max_spread_ticks*tick+1e-9
                        or any(abs(p/tick-round(p/tick)) > 1e-6 for p in (bid, ask))):
                    continue
                pairs.setdefault(row['sample_id'], {})[symbol] = (observed, stamp, (bid+ask)/2)
            except (KeyError, TypeError, ValueError, OverflowError):
                continue  # Includes a partially flushed final CSV row.
    result = []
    previous = None
    for pair in sorted(pairs.values(), key=lambda p: max(v[0] for v in p.values())):
        if not all(s in pair for s in SYMBOLS):
            continue
        a, b = (pair[s] for s in SYMBOLS)
        stamps = (a[1], b[1])
        if (abs(a[0]-b[0]) > settings.max_pair_gap_seconds
                or abs(a[1]-b[1]) > settings.max_pair_gap_seconds
                or (previous is not None and any(x <= y for x, y in zip(stamps, previous)))):
            continue
        previous = stamps
        result.append((max(a[0], b[0]), a[2]-b[2]))
    return result


def trade_rows(histories, settings, wall):
    """Use only past as-of pairs; trade prices are noisier than midpoints."""
    events = []
    for symbol in SYMBOLS:
        for trade in histories[symbol]:
            try:
                stamp, price = _stamp(trade.timestamp), float(trade.price)
                if math.isfinite(price) and price > 0 and wall-settings.history_seconds <= stamp <= wall:
                    events.append((stamp, symbol, price))
            except (AttributeError, TypeError, ValueError, OverflowError):
                continue
    latest, previous, result = {}, None, []
    for stamp, symbol, price in sorted(events):
        latest[symbol] = (stamp, price)
        if len(latest) != 2:
            continue
        a, b = (latest[s] for s in SYMBOLS)
        stamps = (a[0], b[0])
        if (abs(a[0]-b[0]) > settings.max_pair_gap_seconds
                or (previous is not None and any(x <= y for x, y in zip(stamps, previous)))):
            continue
        previous = stamps
        result.append((stamp, a[1]-b[1]))
    return result


def bootstrap_cycle(model, exchange, directory, ticks, now, wall, *, scan_recordings=True):
    """Prefer recorded mids, then read the SDK's optional public history cache.

    Cache availability/backfill depends on the deployed SDK/server. Never call
    poll_new_trade_ticks here: PhilipsPriceRecorder owns that stream.
    """
    attempts = []
    if not model.settings.enabled:
        return dict(loaded=False, reason='disabled', attempts=attempts)
    paths = sorted(Path(directory).glob('philips_*/prices.csv'), reverse=True)[:32] if scan_recordings else []
    for path in paths:
        try:
            rows = price_rows(path, model.settings, ticks, wall)
            if not rows:
                continue
            report = model.seed(rows, ticks, now, wall, source=str(path),
                                max_age_seconds=model.settings.period_seconds)
            attempts.append(report)
            if report['loaded']:
                return dict(report, attempts=attempts)
        except (OSError, csv.Error) as error:
            attempts.append(dict(source=str(path), loaded=False, reason=type(error).__name__))
    source = 'exchange.get_trade_tick_history'
    reader = getattr(exchange, 'get_trade_tick_history', None)
    if callable(reader):
        try:
            histories = {s: reader(s) or [] for s in SYMBOLS}
            rows = trade_rows(histories, model.settings, wall)
            report = model.seed(rows, ticks, now, wall, source=source,
                                max_age_seconds=model.settings.period_seconds)
            attempts.append(report)
            if report['loaded']:
                return dict(report, attempts=attempts)
        except Exception as error:
            attempts.append(dict(source=source, loaded=False, reason=type(error).__name__))
    else:
        attempts.append(dict(source=source, loaded=False, reason='history_api_unavailable'))
    return dict(loaded=False, reason='history_unavailable_or_unusable', attempts=attempts)
