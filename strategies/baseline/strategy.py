#!/usr/bin/env python
# coding: utf-8

# In[ ]:


import logging
from dataclasses import asdict
import math
from pathlib import Path
import sys
import time


def find_workspace():
    anchors = [Path.cwd(), Path('/home/workspace')]
    if '__file__' in globals():
        anchors.insert(0, Path(__file__).resolve().parent)
    for anchor in anchors:
        for parent in (anchor, *anchor.parents):
            for candidate in (parent, parent / 'your_optiver_workspace'):
                if ((candidate / 'stock_market_making' / 'recording' / 'trade_logger.py').is_file()
                        and (candidate / 'stock_market_making' / 'recording' / 'strategy_recording.py').is_file()):
                    return candidate.resolve()
    raise FileNotFoundError(
        'Cannot find the strategy helpers. Keep the complete stock_market_making/ '
        'directory under your_optiver_workspace/ when copying this strategy.')


WORKSPACE_ROOT = find_workspace()
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

# During local development, ``common`` may live in the deployment workspace
# while this package is edited in a separate checkout. Prefer this file's
# checkout for stock_market_making imports in that layout.
SOURCE_PACKAGE_PARENT = Path(__file__).resolve().parents[3]
if ((SOURCE_PACKAGE_PARENT / 'stock_market_making' / 'recording' / 'storage.py').is_file()
        and str(SOURCE_PACKAGE_PARENT) not in sys.path):
    sys.path.insert(0, str(SOURCE_PACKAGE_PARENT))

from stock_market_making.recording.strategy_recording import RecordedExchange, StrategyRecorder
from stock_market_making.order_execution import LimitedExchange, QuoteManager
from stock_market_making.quote_helpers import external_price_book
from stock_market_making.strategies.baseline.cycle_signal import (
    CycleSettings, CycleSignal, usable_book, apply_cycle_quote,
)
from stock_market_making.recording.record_philips_prices import PhilipsPriceRecorder
from stock_market_making.recording.storage import MARKET_DIR, RUNS_DIR, RunStorage
from stock_market_making.strategies.baseline.quote_protection import ProtectionSettings, protect_quote


# In[ ]:


TRADE_INSTRUMENTS = {'PHILIPS_A', 'PHILIPS_B'}
POSITION_LIMIT = 100
SOFT_LIMIT = 70
ORDER_VOLUME = 5
VWAP_HALF_LIFE_TICKS = 5

# Period is a hypothesis; fit amplitude/phase from this session only.
# Start fitting after 30 seconds / 20 fresh samples, then blend gradually.
# A remains the reference. Only B receives cycle-based quote adjustment.
CYCLE_SETTINGS = CycleSettings(
    enabled=True,
    period_seconds=180.0,
    history_seconds=720.0,
    min_fit_seconds=30.0,
    ramp_seconds=180.0,
    horizon_seconds=15.0,
    quote_gain=0.5,
    max_quote_shift_ticks=3.0,
    adverse_size_fraction=0.5,
)

# Starting settings for controlled experiments; profitability is unverified.
INVENTORY_SCALE = 35
INVENTORY_SKEW_TICKS = 2.0
SPREAD_MULTIPLIER = 1.0
MIN_HALF_SPREAD_TICKS = 1.0
MARKOUT_HORIZONS = (1, 3, 5, 15, 30, 60)
MARKOUT_TIMEOUT_SECONDS = 90
STRATEGY_VERSION = 'baseline_cycle_risk_only_v2'
CYCLE_PRICE_SHIFT_ENABLED = False
B_PROTECTION = ProtectionSettings(
    max_increasing_volume=2, min_half_spread_ticks=3.0,
    adverse_extra_ticks=1.0, adverse_threshold_ticks=0.5,
    adverse_size_fraction=0.5,
)

# 交易规则：每个品种的买卖挂单数量合计；所有品种共用的更新次数。
MAX_OUTSTANDING_VOLUME = 200
MAX_UPDATES_PER_SECOND = 25

# 记录目录；通常无需修改。
LOG_DIR = RUNS_DIR / 'baseline'
PRICE_DATA_DIR = MARKET_DIR


# In[ ]:


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
    bid_price = round(math.floor((center - half_spread) / tick_size + 1e-9) * tick_size, 10)
    ask_price = round(math.ceil((center + half_spread) / tick_size - 1e-9) * tick_size, 10)

    # Taper before rounding to integer lots; retain one increasing-side lot
    # until the existing soft threshold is reached.
    buy_scale = max(0.0, 1.0 - max(position, 0) / SOFT_LIMIT)
    sell_scale = max(0.0, 1.0 - max(-position, 0) / SOFT_LIMIT)
    buy_volume = min(math.ceil(ORDER_VOLUME * buy_scale - 1e-9),
                     max(0, POSITION_LIMIT - position))
    sell_volume = min(math.ceil(ORDER_VOLUME * sell_scale - 1e-9),
                      max(0, POSITION_LIMIT + position))
    if bid_price <= 0:
        buy_volume = 0
    if ask_price <= 0:
        sell_volume = 0
    return dict(fair_value=fair_value, center=center,
                bid_price=bid_price, ask_price=ask_price,
                buy_volume=buy_volume, sell_volume=sell_volume,
                inventory_adjustment=inventory_adjustment, half_spread=half_spread,
                book_scope='excluding_own_orders')


# In[ ]:


def main():
    from optibook.synchronous_client import Exchange

    logging.getLogger('client').setLevel('ERROR')
    exchange = Exchange(max_nr_trade_history=10000)
    recorder = price_recorder = None
    try:
        exchange.connect()
        instruments = exchange.get_tradable_instruments()
        monitor_ids = tuple(instruments)
        missing_ids = TRADE_INSTRUMENTS.difference(instruments)
        if missing_ids or not TRADE_INSTRUMENTS:
            raise ValueError(f'Trading selection is empty or unavailable: {sorted(missing_ids)}')
        trade_ids = tuple(iid for iid in monitor_ids if iid in TRADE_INSTRUMENTS)

        run_storage = RunStorage('baseline', directory=LOG_DIR)
        recorder = StrategyRecorder(
            exchange, monitor_ids, run_storage.directory, horizons=MARKOUT_HORIZONS,
            markout_timeout_seconds=MARKOUT_TIMEOUT_SECONDS)
        print(f'Strategy: {STRATEGY_VERSION}; B protection: {asdict(B_PROTECTION)}', flush=True)
        price_recorder = PhilipsPriceRecorder(exchange, PRICE_DATA_DIR)
        run_storage.link_events(recorder.path)
        run_storage.link_market(price_recorder.directory)
        print(f'Run manifest: {run_storage.path}', flush=True)
        print(f'PHILIPS A/B CSV folder: {price_recorder.directory}', flush=True)
        exchange = LimitedExchange(
            exchange, max_outstanding_volume=MAX_OUTSTANDING_VOLUME,
            max_updates_per_second=MAX_UPDATES_PER_SECOND)
        exchange = RecordedExchange(exchange, recorder)
        quote_manager = QuoteManager(exchange, position_limit=POSITION_LIMIT, soft_limit=SOFT_LIMIT)
        print(f'Log file: {recorder.path}', flush=True)
        print(f'Trading: {trade_ids}; monitoring and cancelling: {monitor_ids}', flush=True)
        run_config = dict(strategy_version=STRATEGY_VERSION,
                       cycle_price_shift_enabled=CYCLE_PRICE_SHIFT_ENABLED,
                       b_protection=asdict(B_PROTECTION),
                       markout_timeout_seconds=MARKOUT_TIMEOUT_SECONDS,
                       position_limit=POSITION_LIMIT, soft_limit=SOFT_LIMIT,
                       order_volume=ORDER_VOLUME, half_life_ticks=VWAP_HALF_LIFE_TICKS,
                       inventory_scale=INVENTORY_SCALE, inventory_skew_ticks=INVENTORY_SKEW_TICKS,
                       spread_multiplier=SPREAD_MULTIPLIER, min_half_spread_ticks=MIN_HALF_SPREAD_TICKS,
                       markout_horizons=MARKOUT_HORIZONS,
                       max_outstanding_volume=MAX_OUTSTANDING_VOLUME,
                       max_updates_per_second=MAX_UPDATES_PER_SECOND,
                       trade_ids=trade_ids, monitor_ids=monitor_ids,
                       cycle_settings=asdict(CYCLE_SETTINGS), cycle_reference='PHILIPS_A')
        run_storage.update(config=run_config)
        recorder.event('settings', **run_config)

        cycle_model = CycleSignal(CYCLE_SETTINGS)
        tick_sizes = {iid: info.tick_size for iid, info in instruments.items()}
        last_error_print = -float('inf')
        while exchange.is_connected():
            cycle_start = time.monotonic()
            try:
                recorder.sample()
                price_recorder.sample()  # 同一连接，每轮记录 PHILIPS A/B 行情。

                # Non-traded instruments stay monitored and have no resting orders.
                for instrument_id in monitor_ids:
                    if instrument_id not in trade_ids:
                        quote_manager.reconcile(instrument_id, None)

                # Model and recorders share one connection; no additional trade polling.
                external_books = {}
                signal_ids = tuple(dict.fromkeys((*trade_ids, 'PHILIPS_A', 'PHILIPS_B')))
                for instrument_id in signal_ids:
                    if instrument_id not in instruments:
                        continue
                    raw_book = exchange.get_last_price_book(instrument_id)
                    own_orders = exchange.get_outstanding_orders(instrument_id)
                    tick_size = tick_sizes[instrument_id]
                    external_books[instrument_id] = (
                        external_price_book(raw_book, own_orders, tick_size)
                        if usable_book(raw_book, tick_size, time.time(), CYCLE_SETTINGS) else None
                    )
                cycle_model.observe(external_books, tick_sizes, time.monotonic(), time.time())
                for instrument_id in trade_ids:
                    tick_size = tick_sizes[instrument_id]
                    book = external_books.get(instrument_id)
                    # Repricing the previous instrument may take time.
                    if not usable_book(book, tick_size, time.time(), CYCLE_SETTINGS):
                        quote_manager.reconcile(instrument_id, None)
                        recorder.event('skip_quote', instrument=instrument_id,
                                       reason='stale, wide or invalid external book')
                        continue
                    position = exchange.get_positions()[instrument_id]
                    quote = calculate_quote(book, position, tick_size)
                    signal = cycle_model.observe(
                        external_books, tick_sizes, time.monotonic(), time.time()
                    )
                    quote = apply_cycle_quote(
                        quote, book, position, tick_size, instrument_id,
                        signal, CYCLE_SETTINGS, SOFT_LIMIT,
                        apply_price_shift=CYCLE_PRICE_SHIFT_ENABLED
                    )
                    quote = protect_quote(
                        quote, book, position, tick_size, instrument_id, B_PROTECTION
                    )
                    if quote is None:
                        quote_manager.reconcile(instrument_id, None)
                        continue
                    recorder.quote(instrument_id, book, position, quote)
                    result = quote_manager.reconcile(instrument_id, quote)
                    recorder.event('quote_reconciled', instrument=instrument_id, result=result)

            except Exception as error:
                cycle_model.reset()
                recorder.event('cycle_error', error=str(error))
                if time.monotonic() - last_error_print >= 10:
                    print('Cycle stopped (further errors go to the log):', error, flush=True)
                    last_error_print = time.monotonic()
                for instrument_id in monitor_ids:
                    try:
                        exchange.delete_orders(instrument_id, reason='cycle error')
                    except Exception as cancel_error:
                        recorder.event('cancel_error', instrument=instrument_id, error=str(cancel_error))
                    finally:
                        time.sleep(0.1)
            finally:
                if exchange.is_connected():
                    recorder.sample()
                recorder.event('cycle_work_finished', seconds=time.monotonic() - cycle_start)
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            if price_recorder is not None:
                try:
                    if exchange.is_connected():
                        price_recorder.drain_trades()
                finally:
                    price_recorder.close()
        finally:
            try:
                # Disconnect removes orders; existing positions remain.
                exchange.disconnect()
            finally:
                if recorder is not None:
                    recorder.close()


def cli(argv=None):
    """Run live trading only after an explicit safety flag."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--live', action='store_true',
        help='Connect to Optibook and trade; Ctrl+C stops',
    )
    args = parser.parse_args(argv)
    if not args.live:
        parser.error('Explicit --live is required to connect and send orders')
    main()


if __name__ == '__main__':
    cli()
