"""A maker + B IOC sniper, with an explicit recovery workflow."""
import argparse
import importlib
import json
import logging
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
budget_module = importlib.import_module(PACKAGE + '.request_budget')
reconciliation = importlib.import_module(PACKAGE + '.reconcile')
from stock_market_making.strategies.common.runner import Journal
from stock_market_making.strategies.hybrid.state import StateStore
from stock_market_making.recording.shared_market_recording import RecordingExchange

VERSION = 'final_hybrid_v1_3'
COMPATIBLE_VERSIONS = {'final_hybrid_v1', 'final_hybrid_v1_1', 'final_hybrid_v1_2', VERSION}
A, B = strategy_module.SYMBOLS


def build(exchange, config, event, *, clock=time.monotonic, sleep=time.sleep,
          epoch=time.time, terminal_quantity=None):
    strategy_module.validate(config)
    budget = getattr(exchange, 'request_budget', None)
    if budget is None:
        budget = budget_module.RequestBudget(min(config.get('max_requests_per_second', 200),
            config['max_updates_per_second']), clock=clock, sleep=sleep)
        exchange = budget_module.BudgetedExchange(exchange, budget)
    fills = FillStream(exchange)
    sender = execution.HybridExchange(exchange, limiter=budget, position_limit=100,
                                      max_outstanding_volume=200)
    sender.clock = clock
    executor = execution.HybridExecutor(sender, fills, clock=clock, sleep=sleep,
        settlement_seconds=config['settlement_seconds'], event=event,
        terminal_quantity=terminal_quantity)
    strategy = strategy_module.HybridStrategy(sender, executor, config, event=event,
                                               clock=clock, epoch=epoch)
    executor.deadline = (float('inf') if config.get('run_forever', True)
                         else strategy.start + config['session_seconds'] + config['shutdown_grace_seconds'])
    return strategy, fills


def reconcile_run(args, config):
    guard = StateStore(args.state_file)
    exchange = None
    cancelled = []
    try:
        guard.acquire()
        prior = json.loads(guard.path.read_text(encoding='utf-8'))
        if prior.get('strategy') not in COMPATIBLE_VERSIONS:
            raise ValueError('unrecognized strategy state')
        if prior.get('risk_stopped'):
            raise ValueError('risk-stopped run requires review; reconciliation will not reset its risk stop')
        baseline = reconciliation.previous_baseline(prior, args.log_dir)
        from optibook.synchronous_client import Exchange
        exchange = budget_module.BudgetedExchange(Exchange(max_nr_trade_history=10000),
            budget_module.RequestBudget(config.get('max_requests_per_second', 200),
                                        clock=time.monotonic, sleep=time.sleep))
        exchange.connect()
        # Recovery owns cleanup for both instruments: cancel first, verify the
        # account second, then either unlock or leave the state locked.
        cancelled = reconciliation.cancel_outstanding_orders(exchange)
        result = reconciliation.audit_account(exchange, baseline)
        result['cancelled_orders'] = cancelled
        exchange.disconnect()
        exchange = None
        guard.write(dict(strategy=VERSION, **result, run_id=prior.get('run_id'),
                         previous_error=prior.get('error'), reconciled_at_epoch=time.time()))
        print(json.dumps(dict(summary=result, next_step='run --live'), ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps(dict(summary=dict(safe_to_start=False, cancelled_orders=cancelled),
                              error=str(exc)), ensure_ascii=False))
        return 2
    finally:
        try:
            if exchange is not None:
                exchange.disconnect()
        finally:
            guard.close()


def record_fills(fills, event):
    for iid in (A, B):
        for trade in fills.poll_new_trades(iid):
            event('fill', **vars(trade))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--live', action='store_true')
    mode.add_argument('--reconcile', action='store_true',
                      help='Reconnect, check original B baseline/no A/B orders, then unlock')
    parser.add_argument('--cancel-orders', action='store_true',
                        help='Compatibility flag; --reconcile always cancels PHILIPS_A/B orders')
    parser.add_argument('--config', type=Path, default=DIRECTORY / 'config.json')
    parser.add_argument('--duration', type=float)
    parser.add_argument('--log-dir', type=Path, default=ROOT / 'data/runs/final-hybrid')
    parser.add_argument('--state-file', type=Path, default=ROOT / 'state/default/final-hybrid.json')
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding='utf-8'))
        config.setdefault('max_requests_per_second', 200)
        config.setdefault('maker_seconds', .25)
        config.setdefault('run_forever', True)
        if args.duration is not None:
            config['session_seconds'] = args.duration
        strategy_module.validate(config)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    if args.cancel_orders and not args.reconcile:
        parser.error('--cancel-orders requires --reconcile')
    if args.reconcile:
        return reconcile_run(args, config)
    if not args.live:
        print(json.dumps(dict(strategy=VERSION, market_making=[A], ioc_sniping=[B],
            cycle_target=[B] if config.get('b_cycle', {}).get('enabled') else [],
            position_limit_per_instrument=100, config=config), indent=2))
        return 0

    journal = Journal(args.log_dir, strategy='final-hybrid', mode='live', config=config)
    guard = StateStore(args.state_file)
    exchange = strategy = fills = None
    armed = False
    error = None
    summary = dict(safe_to_start=False)
    capture = budget_module.DisconnectCapture()
    logging.getLogger().addHandler(capture)
    budget = None
    try:
        guard.acquire()
        if guard.path.exists():
            prior = json.loads(guard.path.read_text(encoding='utf-8'))
            if prior.get('strategy') not in COMPATIBLE_VERSIONS or prior.get('safe_to_start') is not True:
                raise ValueError('previous hybrid run is unresolved; run --reconcile to check the account (no orders sent)')
        from optibook.synchronous_client import Exchange
        budget = budget_module.RequestBudget(min(config['max_requests_per_second'],
                                                 config['max_updates_per_second']),
                                             clock=time.monotonic, sleep=time.sleep)
        client = budget_module.BudgetedExchange(Exchange(max_nr_trade_history=10000), budget)
        exchange = RecordingExchange(client, ROOT / 'data/market',
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
                     cycle_target=[B] if strategy.target_cycle.enabled else [],
                     market_making=[A], ioc_sniping=[B], baseline_B=strategy.baseline_b)
        print(f'{VERSION}: A maker / B IOC; loop={config["loop_seconds"]}s; '
              f'A refresh={config["maker_seconds"]}s; limit={budget.maximum}/s, '
              f'client budget={budget.capacity}/{budget.window}s; '
              f'log: {journal.path}', flush=True)
        end = strategy.start + config['session_seconds']
        last_budget_log = -float('inf')
        while True:
            started = time.monotonic()
            try:
                if not exchange.is_connected():
                    journal.emit('connection_lost_reconnecting')
                    while not exchange.is_connected():
                        try:
                            exchange.connect()
                        except Exception as exc:
                            journal.emit('connection_reconnect_failed', error=str(exc))
                        if not exchange.is_connected():
                            time.sleep(1.)
                    journal.emit('connection_reconnected')
                if not config['run_forever'] and time.monotonic() >= end:
                    break
                strategy.step()
                record_fills(fills, journal.emit)
                for iid in (A, B):
                    exchange.poll_new_trade_ticks(iid)
                exchange.sample_market_data()
                if time.monotonic()-last_budget_log >= 1:
                    journal.emit('request_budget', **budget.snapshot(),
                                 loop_work_seconds=time.monotonic()-started,
                                 loop_target_seconds=config['loop_seconds'])
                    last_budget_log = time.monotonic()
                if (not config['run_forever'] and strategy.stopped
                        and strategy.cycle_position == 0):
                    break
            except Exception as exc:
                # Every live-loop fault is retried. On the next successful
                # step B is rebased from the exchange account if necessary.
                journal.emit('runtime_error_continuing', error=str(exc), traceback=traceback.format_exc())
                time.sleep(.25)
                continue
            time.sleep(max(0, config['loop_seconds'] - (time.monotonic() - started)))
    except KeyboardInterrupt:
        print('Stopping A quotes and closing confirmed B sniper inventory.', flush=True)
    except Exception as exc:
        error = capture.reason or str(exc)
        journal.emit('fatal_error', error=error, api_error=str(exc), traceback=traceback.format_exc())
    finally:
        try:
            if armed and strategy is not None:
                # Preserve ownership even if all subsequent reads are impossible.
                summary.update(baseline_B=strategy.baseline_b, owned_B=strategy.cycle_position,
                    unresolved=strategy.executor.pending, risk_stopped=strategy.risk_stopped,
                    last_confirmed_B=strategy.baseline_b+strategy.cycle_position,
                    positions_confirmed_at_finish=False)
                if not exchange.is_connected():
                    error = capture.reason or error or 'connection lost; final positions could not be confirmed'
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
                    unresolved=strategy.executor.pending, A_residual_retained=True,
                    positions_confirmed_at_finish=True)
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
                if budget is not None:
                    journal.emit('request_budget', **budget.snapshot())
                journal.emit('session_end', **summary, error=error)
                journal.close()
                logging.getLogger().removeHandler(capture)
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
