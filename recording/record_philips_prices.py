"""Record PHILIPS A/B market data using an existing or standalone Exchange.

Run before the open; each stock is recorded when it becomes tradable. No orders
are submitted. Saves top-of-book/trade CSVs and compressed full-depth snapshots.
Snapshots contain every level returned by the API, not every intervening update.
"""

import argparse
import csv
import gzip
import json
import logging
import math
import shutil
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


LOGGER = logging.getLogger(__name__)
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1] / 'data' / 'market'
MIB = 1024 ** 2
ORDERBOOK_FLUSH_SECONDS = 5.0
STORAGE_REPORT_SECONDS = 60.0
PRICE_FIELDS = (
    'sample_id', 'observed_at_utc', 'instrument_id', 'book_timestamp', 'status',
    'best_bid', 'best_bid_volume', 'best_ask', 'best_ask_volume', 'mid', 'spread',
    'last_trade_price', 'last_trade_timestamp', 'error',
)
TRADE_FIELDS = (
    'observed_at_utc', 'instrument_id', 'trade_timestamp', 'trade_id',
    'price', 'volume', 'aggressor_side', 'buyer', 'seller',
)


def timestamp(value):
    """Keep the exchange's timezone information, including missing offsets."""
    return value.isoformat() if isinstance(value, datetime) else value


def is_philips_ab(instrument_id):
    return instrument_id.upper().replace('_', '') in {'PHILIPSA', 'PHILIPSB'}


class _OrderBookWriter:
    """Stream independent full snapshots into size-rotated gzip JSONL files.

    Rotation uses compressed bytes, with a possible overshoot of one snapshot
    plus the compressor's buffer/footer. Old chunks are never overwritten.
    """

    def __init__(self, directory, chunk_mb):
        if not math.isfinite(chunk_mb) or chunk_mb <= 0:
            raise ValueError('orderbook_chunk_mb must be finite and greater than zero.')
        self.directory = directory
        self.chunk_bytes = max(1, int(chunk_mb * MIB))
        self._raw = self._gzip = None
        self._index = 0
        self.closed = False

    def _finish_chunk(self):
        if self._gzip is not None:
            try:
                self._gzip.close()
            finally:
                self._raw.close()
                self._gzip = self._raw = None

    def write(self, row):
        if self.closed:
            raise ValueError('Order book writer is closed.')
        data = (json.dumps(row, ensure_ascii=False, allow_nan=False,
                           separators=(',', ':')) + '\n').encode('utf-8')
        if self._gzip is not None and self._raw.tell() >= self.chunk_bytes:
            self._finish_chunk()
        if self._gzip is None:
            self._index += 1
            path = self.directory / f'orderbooks_{self._index:05d}.jsonl.gz'
            self._raw = path.open('xb', buffering=0)
            try:
                self._gzip = gzip.GzipFile(filename='', mode='wb', fileobj=self._raw,
                                           compresslevel=1, mtime=0)
            except BaseException:
                self._raw.close()
                self._raw = None
                raise
        self._gzip.write(data)

    def flush(self):
        if self._gzip is not None:
            self._gzip.flush()
            if self._raw.tell() >= self.chunk_bytes:
                self._finish_chunk()

    def close(self):
        if not self.closed:
            try:
                self._finish_chunk()
            finally:
                self.closed = True


def _book_levels(book):
    """Keep every level in API order; make invalid numeric fields explicit."""
    if book is None:
        return None, None, []
    invalid = []
    sides = []
    for side in ('bids', 'asks'):
        levels = []
        for index, level in enumerate(getattr(book, side, None) or ()):
            values = []
            for field in ('price', 'volume'):
                value = getattr(level, field, None)
                if (not isinstance(value, (int, float)) or isinstance(value, bool)
                        or not math.isfinite(value)):
                    invalid.append(f'{side}[{index}].{field}')
                    value = None
                elif value < 0 or (field == 'price' and value == 0):
                    invalid.append(f'{side}[{index}].{field}')
                values.append(value)
            levels.append(values)
        sides.append(levels)
    return *sides, invalid


class PhilipsPriceRecorder:
    """Reuse a connected Exchange; sample() never connects, trades, or sleeps.

    This recorder must be the only consumer of poll_new_trade_ticks() for these
    instruments. It does not consume private fills used by a trading strategy.
    """

    def __init__(self, exchange, output_dir=DEFAULT_OUTPUT_DIR, *,
                 record_orderbooks=True, orderbook_chunk_mb=64.0):
        if not math.isfinite(orderbook_chunk_mb) or orderbook_chunk_mb <= 0:
            raise ValueError('orderbook_chunk_mb must be finite and greater than zero.')
        self.exchange = exchange
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
        self.directory = Path(output_dir) / f'philips_{stamp}_{uuid4().hex[:8]}'
        self.directory.mkdir(parents=True, exist_ok=False)
        self._files = ExitStack()
        self._csv_handles = []
        self._orderbooks = None
        self.closed = False
        self._last_flush = self._last_report = None
        self._stopped_at = None
        try:
            self.prices = self._writer('prices.csv', PRICE_FIELDS)
            self.trades = self._writer('trades.csv', TRADE_FIELDS)
            if record_orderbooks:
                self._orderbooks = _OrderBookWriter(self.directory, orderbook_chunk_mb)
                self._files.callback(self._orderbooks.close)
        except BaseException:
            self._files.close()
            raise
        self.known_instruments = set()
        self.last_trades = {}
        self.active = None
        self.sample_id = 0
        self.snapshot_count = 0
        self.orderbook_count = 0
        self.trade_count = 0
        self.started_at = None

    def _writer(self, filename, fields):
        handle = self._files.enter_context((self.directory / filename).open(
            'x', encoding='utf-8-sig', newline='', buffering=1))
        self._csv_handles.append(handle)
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        return writer

    def sample(self):
        """Record one polling cycle; return the number of price rows written."""
        if self.closed:
            raise ValueError('Price recorder is closed.')
        if not self.exchange.is_connected():
            raise ConnectionError('Exchange disconnected; recording stopped.')
        try:
            tradable = self.exchange.get_tradable_instruments()
        except Exception:
            LOGGER.exception('Cannot read tradable instruments; skipping this cycle.')
            return 0

        # Resolve actual IDs from the exchange, including after a delayed open.
        active = {iid for iid in tradable if is_philips_ab(iid)}
        if active != self.active:
            LOGGER.info('Tradable PHILIPS stocks: %s', ', '.join(sorted(active)) or
                        'none; waiting for the market to open')
            self.active = active
        self.known_instruments.update(active)
        self.sample_id += 1
        if active and self.started_at is None:
            self.started_at = time.monotonic()

        self.drain_trades()

        for iid in sorted(active):
            error = None
            try:
                book = self.exchange.get_last_price_book(iid)
            except Exception as exc:
                LOGGER.exception('Cannot read price book for %s.', iid)
                book, error = None, str(exc)
            bid = book.bids[0] if book and book.bids else None
            ask = book.asks[0] if book and book.asks else None
            status = ('read_error' if error is not None else 'no_book' if book is None else
                      'empty' if bid is None and ask is None else
                      'ask_only' if bid is None else 'bid_only' if ask is None else 'ok')
            mid = spread = None
            if bid is not None and ask is not None:
                if (math.isfinite(bid.price) and math.isfinite(ask.price)
                        and 0 < bid.price <= ask.price):
                    mid = (bid.price + ask.price) / 2
                    spread = ask.price - bid.price
                else:
                    status = 'invalid_book'
            last_trade = self.last_trades.get(iid)
            observed_at = datetime.now(timezone.utc).isoformat()
            book_timestamp = timestamp(getattr(book, 'timestamp', None))
            if self._orderbooks is not None:
                bids, asks, invalid = _book_levels(book)
                if invalid:
                    status = 'invalid_book'
                    error = 'Invalid book fields: ' + ', '.join(invalid)
                self._orderbooks.write(dict(
                    schema_version=1, sample_id=self.sample_id, observed_at_utc=observed_at,
                    instrument_id=iid, book_timestamp=book_timestamp, status=status,
                    bids=bids, asks=asks, error=error,
                ))
                self.orderbook_count += 1
            self.prices.writerow(dict(
                sample_id=self.sample_id,
                observed_at_utc=observed_at,
                instrument_id=iid, book_timestamp=book_timestamp,
                status=status, best_bid=bid.price if bid else None,
                best_bid_volume=bid.volume if bid else None,
                best_ask=ask.price if ask else None,
                best_ask_volume=ask.volume if ask else None, mid=mid, spread=spread,
                last_trade_price=last_trade.price if last_trade else None,
                last_trade_timestamp=timestamp(getattr(last_trade, 'timestamp', None)),
                error=error,
            ))
            self.snapshot_count += 1
        now = time.monotonic()
        if self._last_flush is None or now - self._last_flush >= ORDERBOOK_FLUSH_SECONDS:
            self.flush()
            self._last_flush = now
        if self._last_report is None:
            self._last_report = now
        elif now - self._last_report >= STORAGE_REPORT_SECONDS:
            self.log_storage_stats()
            self._last_report = now
        return len(active)

    def drain_trades(self):
        """Save pending public ticks, including trades arriving during a pause."""
        if self.closed:
            raise ValueError('Price recorder is closed.')
        for iid in sorted(self.known_instruments):
            try:
                ticks = self.exchange.poll_new_trade_ticks(iid)
            except Exception:
                LOGGER.exception('Cannot read public trades for %s.', iid)
                continue
            for tick in ticks or ():
                self.trades.writerow(dict(
                    observed_at_utc=datetime.now(timezone.utc).isoformat(),
                    instrument_id=iid, trade_timestamp=timestamp(tick.timestamp),
                    trade_id=tick.trade_id, price=tick.price, volume=tick.volume,
                    aggressor_side=tick.aggressor_side,
                    buyer=getattr(tick, 'buyer', None),
                    seller=getattr(tick, 'seller', None),
                ))
                self.last_trades[iid] = tick
                self.trade_count += 1

    def flush(self):
        """Publish buffered bytes; no exchange calls. A live gzip may lack its footer."""
        for handle in self._csv_handles:
            if not handle.closed:
                handle.flush()
        if self._orderbooks is not None:
            self._orderbooks.flush()

    def storage_stats(self, elapsed_seconds=None):
        """Measure this session (CSVs + compressed books), not the entire account.

        Free space is the filesystem's report; a cloud account quota may be lower.
        Rates use elapsed wall time since the first active sample, including pauses.
        """
        self.flush()
        if elapsed_seconds is None:
            end = self._stopped_at if self.closed else time.monotonic()
            elapsed_seconds = max(0.0, end - self.started_at) if self.started_at is not None else 0.0
        if not math.isfinite(elapsed_seconds) or elapsed_seconds < 0:
            raise ValueError('elapsed_seconds must be finite and nonnegative.')
        size = sum(path.stat().st_size for path in self.directory.iterdir() if path.is_file())
        try:
            free = shutil.disk_usage(self.directory).free
        except OSError:
            free = None
        rate = size * 3600 / elapsed_seconds if elapsed_seconds > 0 and size > 0 else None
        return dict(bytes_written=size, elapsed_seconds=elapsed_seconds, bytes_per_hour=rate,
                    free_bytes=free,
                    estimated_hours_remaining=free / rate if free is not None and rate else None)

    def log_storage_stats(self):
        stats = self.storage_stats()
        rate = stats['bytes_per_hour']
        LOGGER.info('Storage: %.2f MiB written; %s MiB/hour; filesystem free: %s MiB; '
                    'estimated remaining: %s hours (cloud quota may be lower).',
                    stats['bytes_written'] / MIB,
                    f'{rate / MIB:.2f}' if rate is not None else 'not measured yet',
                    f"{stats['free_bytes'] / MIB:.0f}" if stats['free_bytes'] is not None else 'unknown',
                    f"{stats['estimated_hours_remaining']:.1f}"
                    if stats['estimated_hours_remaining'] is not None else 'unknown')
        return stats

    def close(self):
        if not self.closed:
            try:
                self._files.close()
            finally:
                self.closed = True
                self._stopped_at = time.monotonic()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=(
        'Record PHILIPS A/B quotes, full-depth book snapshots and public trades. Start before the open; '
        'waits for tradable instruments. Uses the account\'s sole connection.'))
    parser.add_argument('--interval', type=float, default=0.5,
                        help='Seconds to sleep between samples (default: 0.5; minimum: 0.2).')
    parser.add_argument('--duration', type=float,
                        help='Stop N seconds after the first PHILIPS stock becomes tradable.')
    parser.add_argument('--output-dir', type=Path, default=DEFAULT_OUTPUT_DIR,
                        help='Parent folder for a new recording session.')
    parser.add_argument('--stop-file', type=Path,
                        help='Stop gracefully when this file exists (used by the notebook launcher).')
    parser.add_argument('--no-orderbooks', action='store_true',
                        help='Disable full-depth gzip JSONL; keep price and trade CSVs.')
    parser.add_argument('--orderbook-chunk-mb', type=float, default=64.0,
                        help='Rotate compressed book files at approximately N MiB (default: 64). '
                             'This is a per-file size, not a total storage cap.')
    parser.add_argument('--trade-history-size', type=int, default=10000,
                        help='Public/private trade buffer per instrument for standalone connection '
                             '(default: 10000). Very busy feeds can still exceed it between polls.')
    args = parser.parse_args(argv)
    if not math.isfinite(args.interval) or args.interval < 0.2:
        parser.error('--interval must be finite and at least 0.2 seconds.')
    if args.duration is not None and (not math.isfinite(args.duration) or args.duration <= 0):
        parser.error('--duration must be finite and greater than zero.')
    if not math.isfinite(args.orderbook_chunk_mb) or args.orderbook_chunk_mb <= 0:
        parser.error('--orderbook-chunk-mb must be finite and greater than zero.')
    if args.trade_history_size <= 0:
        parser.error('--trade-history-size must be greater than zero.')
    return args


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if args.stop_file is not None and args.stop_file.exists():
        LOGGER.info('Stop already requested; no connection was made.')
        return 0
    # Lazy import keeps --help and offline checks usable without Optibook.
    from optibook.synchronous_client import Exchange

    exchange = Exchange(max_nr_trade_history=args.trade_history_size)
    recorder = None
    try:
        with PhilipsPriceRecorder(exchange, args.output_dir,
                                  record_orderbooks=not args.no_orderbooks,
                                  orderbook_chunk_mb=args.orderbook_chunk_mb) as recorder:
            LOGGER.info('Recording folder: %s', recorder.directory)
            LOGGER.info('Full-depth books: %s; sample sleep: %.3fs. '
                        'Records latest API snapshots, not every order update.',
                        'gzip JSONL' if not args.no_orderbooks else 'disabled', args.interval)
            if args.stop_file is not None and args.stop_file.exists():
                LOGGER.info('Stop requested before connecting.')
                return 0
            LOGGER.warning('Connecting uses the account\'s sole connection and kicks any other client.')
            exchange.connect()
            try:
                while args.stop_file is None or not args.stop_file.exists():
                    recorder.sample()
                    time.sleep(args.interval)
                    if (args.duration is not None and recorder.started_at is not None
                            and time.monotonic() - recorder.started_at >= args.duration):
                        break
                if args.stop_file is not None and args.stop_file.exists():
                    LOGGER.info('Stop requested by the notebook.')
            finally:
                # Capture ticks received during the final sleep before closing.
                if exchange.is_connected():
                    recorder.drain_trades()
    except KeyboardInterrupt:
        LOGGER.info('Stopped by Ctrl+C.')
    except Exception:
        LOGGER.exception('Recording stopped due to an error.')
        return 1
    finally:
        try:
            if exchange.is_connected():
                exchange.disconnect()
        except Exception:
            LOGGER.exception('Could not cleanly disconnect the exchange.')
        if recorder is not None:
            LOGGER.info('Saved %s price snapshots, %s full-depth snapshots and %s public trades to %s',
                        recorder.snapshot_count, recorder.orderbook_count, recorder.trade_count,
                        recorder.directory)
            try:
                recorder.log_storage_stats()
            except OSError:
                LOGGER.exception('Could not measure final recording size.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
