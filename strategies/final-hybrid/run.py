"""A-only baseline maker + B IOC sniper. No connection without --live."""
import argparse
import importlib
import json
from pathlib import Path
import sys
import time
import traceback

DIRECTORY = Path(__file__).resolve().parent
ROOT = DIRECTORY.parents[1]
sys.path.insert(0, str(ROOT.parent))
PACKAGE = 'stock_market_making.strategies.final-hybrid'
strategy_module = importlib.import_module(PACKAGE + '.strategy')
execution = importlib.import_module(PACKAGE + '.execution')
FillStream = importlib.import_module(PACKAGE + '.fill_stream').FillStream
UpdateRateLimiter = importlib.import_module(PACKAGE + '.order_execution').UpdateRateLimiter
from stock_market_making.strategies.common.runner import Journal
from stock_market_making.strategies.hybrid.state import StateStore
from stock_market_making.recording.shared_market_recording import RecordingExchange

VERSION = 'final_hybrid_v1'
A, B = strategy_module.SYMBOLS


def build(exchange, config, event, *, clock=time.monotonic, sleep=time.sleep,
          epoch=time.time, terminal_quantity=None):
    fills = FillStream(exchange)
    limiter = UpdateRateLimiter(config['max_updates_per_second'], clock=clock, sleep=sleep)
    sender = execution.HybridExchange(exchange, limiter=limiter, position_limit=100,
                                      max_outstanding_volume=200)
    executor = execution.HybridExecutor(sender, fills, clock=clock, sleep=sleep,
        settlement_seconds=config['settlement_seconds'], event=event,
        terminal_quantity=terminal_quantity)
    strategy = strategy_module.HybridStrategy(sender, executor, config, event=event,
                                               clock=clock, epoch=epoch)
    executor.deadline = strategy.start + config['session_seconds'] + config['shutdown_grace_seconds']
    return strategy, fills


def record_fills(fills, event):
    for iid in (A, B):
        for trade in fills.poll_new_trades(iid):
            event('fill', **vars(trade))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--live', action='store_true')
    parser.add_argument('--config', type=Path, default=DIRECTORY / 'config.json')
    parser.add_argument('--duration', type=float)
    parser.add_argument('--log-dir', type=Path, default=ROOT / 'data/runs/final-hybrid')
    parser.add_argument('--state-file', type=Path, default=ROOT / 'state/default/final-hybrid.json')
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding='utf-8'))
        if args.duration is not None:
            config['session_seconds'] = args.duration
        strategy_module.validate(config)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    if not args.live:
        print(json.dumps(dict(strategy=VERSION, market_making=[A], ioc_sniping=[B],
            position_limit_per_instrument=100, config=config), indent=2))
        return 0

    journal = Journal(args.log_dir, strategy='final-hybrid', mode='live', config=config)
    guard = StateStore(args.state_file)
    exchange = strategy = fills = None
    armed = False
    error = None
    summary = dict(safe_to_start=False)
    try:
        guard.acquire()
        if guard.path.exists():
            prior = json.loads(guard.path.read_text(encoding='utf-8'))
            if prior.get('strategy') != VERSION or prior.get('safe_to_start') is not True:
                raise ValueError('previous hybrid run is unresolved; reconcile its account/state first')
        from optibook.synchronous_client import Exchange
        exchange = RecordingExchange(Exchange(max_nr_trade_history=10000), ROOT / 'data/market',
                                     interval=config['recording_seconds'])
        exchange.connect()
        strategy, fills = build(exchange, config, journal.emit)
        guard.write(dict(strategy=VERSION, safe_to_start=False, reason='active_run',
                         baseline_B=strategy.baseline_b, run_id=journal.storage.run_id))
        armed = True
        strategy.executor.reconcile(A, None)
        record_fills(fills, journal.emit)
        exchange.start_recording()
        journal.storage.link_market(exchange.recorder.directory)
        journal.emit('settings', strategy_version=VERSION, config=config,
                     market_making=[A], ioc_sniping=[B], baseline_B=strategy.baseline_b)
        print(f'{VERSION}: A maker / B IOC; 50ms default loop; log: {journal.path}', flush=True)
        end = strategy.start + config['session_seconds']
        while exchange.is_connected() and time.monotonic() < end:
            started = time.monotonic()
            if journal.failed:
                strategy.stopped = True
            strategy.step()
            record_fills(fills, journal.emit)
            for iid in (A, B):
                exchange.poll_new_trade_ticks(iid)
            exchange.sample_market_data()
            if strategy.stopped and strategy.cycle_position == 0:
                break
            time.sleep(max(0, config['loop_seconds'] - (time.monotonic() - started)))
    except KeyboardInterrupt:
        print('Stopping A quotes and closing confirmed B sniper inventory.', flush=True)
    except Exception as exc:
        error = str(exc)
        journal.emit('fatal_error', error=error, traceback=traceback.format_exc())
    finally:
        try:
            if armed and strategy is not None and exchange.is_connected():
                # Cancel A before waiting on B, so shutdown cannot leave stale A quotes.
                strategy.stop()
                deadline = min(time.monotonic() + config['shutdown_grace_seconds'],
                               strategy.executor.deadline)
                strategy.executor.deadline = deadline
                while (strategy.cycle_position and not strategy.executor.halted
                       and time.monotonic() < deadline and exchange.is_connected()):
                    strategy.step()
                    record_fills(fills, journal.emit)
                    time.sleep(config['loop_seconds'])
                record_fills(fills, journal.emit)
                actual = strategy.positions()
                no_orders = all(not exchange.get_outstanding_orders(iid) for iid in (A, B))
                safe = (strategy.cycle_position == 0 and actual[B] == strategy.baseline_b
                        and no_orders and not strategy.executor.halted
                        and not strategy.risk_stopped and error is None and not journal.failed)
                summary = dict(safe_to_start=safe, actual_positions=actual,
                    baseline_B=strategy.baseline_b, owned_B=strategy.cycle_position,
                    B_equity_realized=strategy.cycle_cash, risk_stopped=strategy.risk_stopped,
                    unresolved=strategy.executor.pending, A_residual_retained=True)
        except Exception as exc:
            error = error or str(exc)
            summary['safe_to_start'] = False
            journal.emit('finish_error', error=str(exc), traceback=traceback.format_exc())
        finally:
            try:
                if exchange is not None:
                    exchange.disconnect()
            except Exception as exc:
                error = error or str(exc)
                summary['safe_to_start'] = False
            finally:
                journal.emit('session_end', **summary, error=error)
                journal.close()
                if journal.failed:
                    summary['safe_to_start'] = False
                try:
                    if armed:
                        guard.write(dict(strategy=VERSION, **summary, error=error,
                                         run_id=journal.storage.run_id))
                finally:
                    guard.close()
    print(json.dumps(dict(summary=summary, error=error), ensure_ascii=False), flush=True)
    return 0 if summary['safe_to_start'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
