"""Baseline-style market maker with causal EWMA volatility-aware spreads."""

from dataclasses import asdict
import logging
import math
from pathlib import Path
import sys
import time


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PACKAGE_ROOT.parent))

from stock_market_making.order_execution import LimitedExchange, QuoteManager
from stock_market_making.quote_helpers import external_price_book
from stock_market_making.recording.record_philips_prices import PhilipsPriceRecorder
from stock_market_making.recording.storage import MARKET_DIR, RUNS_DIR, RunStorage
from stock_market_making.strategy_recording import RecordedExchange, StrategyRecorder
from stock_market_making.strategies.baseline.cycle_signal import (
    CycleSettings, CycleSignal, apply_cycle_quote, book_time, usable_book,
)
from stock_market_making.strategies.baseline.quote_protection import (
    PositionAgeTracker, ProtectionSettings, protect_quote,
)
from stock_market_making.strategies.volatility_adaptive.volatility import (
    EWMAVolatility, VolatilitySettings, apply_volatility_quote,
)


TRADE_INSTRUMENTS = {'PHILIPS_A', 'PHILIPS_B'}
POSITION_LIMIT = 100
SOFT_LIMIT = 70
ORDER_VOLUME = 5
VWAP_HALF_LIFE_TICKS = 5
INVENTORY_SCALE = 35
INVENTORY_SKEW_TICKS = 2.0
SPREAD_MULTIPLIER = 1.0
MIN_HALF_SPREAD_TICKS = 1.0
MAX_OUTSTANDING_VOLUME = 200
MAX_UPDATES_PER_SECOND = 25
MARKOUT_HORIZONS = (1, 3, 5, 15, 30, 60)
MARKOUT_TIMEOUT_SECONDS = 90
STRATEGY_VERSION = 'volatility_adaptive_v1'
CYCLE_PRICE_SHIFT_ENABLED = False
LOG_DIR = RUNS_DIR / 'volatility_adaptive'
PRICE_DATA_DIR = MARKET_DIR

CYCLE_SETTINGS = CycleSettings(
    enabled=True, period_seconds=180.0, history_seconds=720.0,
    min_fit_seconds=30.0, ramp_seconds=180.0, horizon_seconds=15.0,
    quote_gain=0.5, max_quote_shift_ticks=3.0, adverse_size_fraction=0.5,
)
B_PROTECTION = ProtectionSettings(
    max_increasing_volume=2, max_abs_position=10, min_half_spread_ticks=3.0,
    min_reducing_half_spread_ticks=3.0, adverse_extra_ticks=1.0,
    adverse_threshold_ticks=0.5, strong_adverse_threshold_ticks=1.0,
    adverse_size_fraction=0.5, small_position_limit=10,
    adverse_min_hold_seconds=2.0,
)
VOLATILITY_SETTINGS = VolatilitySettings(
    half_life_seconds=10.0,
    risk_horizon_seconds=2.0,
    min_samples=5,
    base_full_spread_ticks=2.0,
    sigma_multiplier=2.0,
    max_full_spread_ticks=12.0,
    reduce_size_sigma_ticks=2.0,
    halt_increasing_sigma_ticks=5.0,
    minimum_size_fraction=0.25,
)


def calculate_quote(book, position, tick_size, volatility=None):
    """Calculate the base quote and optionally apply a volatility snapshot."""
    if not book or not book.bids or not book.asks:
        return None
    if not math.isfinite(tick_size) or tick_size <= 0:
        return None
    values = (INVENTORY_SCALE, INVENTORY_SKEW_TICKS, SPREAD_MULTIPLIER,
              MIN_HALF_SPREAD_TICKS, VWAP_HALF_LIFE_TICKS)
    if (not all(math.isfinite(value) for value in values)
            or INVENTORY_SCALE <= 0 or INVENTORY_SKEW_TICKS < 0
            or SPREAD_MULTIPLIER <= 0 or MIN_HALF_SPREAD_TICKS <= 0):
        raise ValueError('invalid quote settings')
    bids, asks = book.bids, book.asks
    if (any(not math.isfinite(level.price) or level.price <= 0
            or not math.isfinite(level.volume) or level.volume <= 0
            for level in (*bids, *asks)) or bids[0].price >= asks[0].price):
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
    buy_scale = max(0.0, 1.0 - max(position, 0) / SOFT_LIMIT)
    sell_scale = max(0.0, 1.0 - max(-position, 0) / SOFT_LIMIT)
    quote = dict(
        fair_value=fair_value, center=center, bid_price=bid_price, ask_price=ask_price,
        buy_volume=min(math.ceil(ORDER_VOLUME * buy_scale - 1e-9),
                       max(0, POSITION_LIMIT - position)),
        sell_volume=min(math.ceil(ORDER_VOLUME * sell_scale - 1e-9),
                        max(0, POSITION_LIMIT + position)),
        inventory_adjustment=inventory_adjustment, half_spread=half_spread,
        book_scope='excluding_own_orders',
    )
    if quote['bid_price'] <= 0:
        quote['buy_volume'] = 0
    return (apply_volatility_quote(
        quote, position, tick_size, volatility, VOLATILITY_SETTINGS)
            if volatility is not None else quote)


def check():
    """Return serialisable settings; constructors perform validation."""
    EWMAVolatility(VOLATILITY_SETTINGS)
    return dict(
        strategy=STRATEGY_VERSION,
        live=False,
        instruments=sorted(TRADE_INSTRUMENTS),
        volatility_settings=asdict(VOLATILITY_SETTINGS),
        cycle_settings=asdict(CYCLE_SETTINGS),
        b_protection=asdict(B_PROTECTION),
    )


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
        if missing_ids:
            raise ValueError(f'unavailable instruments: {sorted(missing_ids)}')
        trade_ids = tuple(iid for iid in monitor_ids if iid in TRADE_INSTRUMENTS)
        run_storage = RunStorage('volatility_adaptive', directory=LOG_DIR)
        recorder = StrategyRecorder(
            exchange, monitor_ids, run_storage.directory, horizons=MARKOUT_HORIZONS,
            markout_timeout_seconds=MARKOUT_TIMEOUT_SECONDS)
        price_recorder = PhilipsPriceRecorder(exchange, PRICE_DATA_DIR)
        run_storage.link_events(recorder.path)
        run_storage.link_market(price_recorder.directory)
        config = dict(
            strategy_version=STRATEGY_VERSION,
            volatility_settings=asdict(VOLATILITY_SETTINGS),
            cycle_settings=asdict(CYCLE_SETTINGS),
            cycle_price_shift_enabled=CYCLE_PRICE_SHIFT_ENABLED,
            b_protection=asdict(B_PROTECTION), position_limit=POSITION_LIMIT,
            soft_limit=SOFT_LIMIT, order_volume=ORDER_VOLUME,
            inventory_scale=INVENTORY_SCALE,
            inventory_skew_ticks=INVENTORY_SKEW_TICKS,
            spread_multiplier=SPREAD_MULTIPLIER,
            min_half_spread_ticks=MIN_HALF_SPREAD_TICKS,
            max_outstanding_volume=MAX_OUTSTANDING_VOLUME,
            max_updates_per_second=MAX_UPDATES_PER_SECOND,
            trade_ids=trade_ids, monitor_ids=monitor_ids,
        )
        run_storage.update(config=config)
        recorder.event('settings', **config)
        print(f'{STRATEGY_VERSION}: {trade_ids}; manifest: {run_storage.path}', flush=True)

        exchange = LimitedExchange(
            exchange, max_outstanding_volume=MAX_OUTSTANDING_VOLUME,
            max_updates_per_second=MAX_UPDATES_PER_SECOND)
        exchange = RecordedExchange(exchange, recorder)
        quote_manager = QuoteManager(
            exchange, position_limit=POSITION_LIMIT, soft_limit=SOFT_LIMIT)
        cycle_model = CycleSignal(CYCLE_SETTINGS)
        volatility_model = EWMAVolatility(VOLATILITY_SETTINGS)
        b_position_age = PositionAgeTracker()
        tick_sizes = {iid: info.tick_size for iid, info in instruments.items()}
        last_error_print = -math.inf

        while exchange.is_connected():
            cycle_start = time.monotonic()
            try:
                recorder.sample()
                price_recorder.sample()
                for instrument_id in monitor_ids:
                    if instrument_id not in trade_ids:
                        quote_manager.reconcile(instrument_id, None)
                external_books = {}
                for instrument_id in trade_ids:
                    raw_book = exchange.get_last_price_book(instrument_id)
                    own_orders = exchange.get_outstanding_orders(instrument_id)
                    tick = tick_sizes[instrument_id]
                    external_books[instrument_id] = (
                        external_price_book(raw_book, own_orders, tick)
                        if usable_book(raw_book, tick, time.time(), CYCLE_SETTINGS) else None)

                cycle_signal = cycle_model.observe(
                    external_books, tick_sizes, time.monotonic(), time.time())
                volatility = {}
                for instrument_id, book in external_books.items():
                    if book is None:
                        continue
                    mid = (book.bids[0].price + book.asks[0].price) / 2
                    volatility[instrument_id] = volatility_model.observe(
                        instrument_id, mid, book_time(book), tick_sizes[instrument_id])

                positions = exchange.get_positions()
                for instrument_id in trade_ids:
                    tick = tick_sizes[instrument_id]
                    book = external_books.get(instrument_id)
                    position = positions[instrument_id]
                    position_age = (b_position_age.observe(position, time.monotonic())
                                    if instrument_id == 'PHILIPS_B' else None)
                    if not usable_book(book, tick, time.time(), CYCLE_SETTINGS):
                        quote_manager.reconcile(instrument_id, None)
                        recorder.event('skip_quote', instrument=instrument_id,
                                       reason='stale, wide or invalid external book')
                        continue
                    quote = calculate_quote(
                        book, position, tick, volatility.get(instrument_id))
                    quote = apply_cycle_quote(
                        quote, book, position, tick, instrument_id, cycle_signal,
                        CYCLE_SETTINGS, SOFT_LIMIT,
                        apply_price_shift=CYCLE_PRICE_SHIFT_ENABLED)
                    quote = protect_quote(
                        quote, book, position, tick, instrument_id, B_PROTECTION,
                        position_age=position_age)
                    recorder.quote(instrument_id, book, position, quote)
                    result = quote_manager.reconcile(instrument_id, quote)
                    recorder.event('quote_reconciled', instrument=instrument_id,
                                   result=result)
            except Exception as error:
                cycle_model.reset()
                volatility_model.reset()
                recorder.event('strategy_error', error=str(error))
                if time.monotonic() - last_error_print >= 10:
                    print('Strategy error (further errors go to log):', error, flush=True)
                    last_error_print = time.monotonic()
                for instrument_id in monitor_ids:
                    try:
                        exchange.delete_orders(instrument_id)
                    except Exception as cancel_error:
                        recorder.event('cancel_error', instrument=instrument_id,
                                       error=str(cancel_error))
                    time.sleep(0.1)
            finally:
                if exchange.is_connected():
                    recorder.sample()
                recorder.event('cycle_work_finished',
                               seconds=time.monotonic() - cycle_start)
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
                exchange.disconnect()
            finally:
                if recorder is not None:
                    recorder.close()

