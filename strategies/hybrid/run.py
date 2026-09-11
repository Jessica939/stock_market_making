"""Single-connection simultaneous baseline MM + cycle pair. No connection without --live."""
import argparse
import json
from pathlib import Path
import sys
import time

DIRECTORY = Path(__file__).resolve().parent
sys.path.insert(0, str(DIRECTORY.parents[2]))
from stock_market_making.strategies.common.runner import DEFAULTS, Journal, validate_config
from stock_market_making.strategies.hybrid.engine import Hybrid
from stock_market_making.strategies.pair.policy import Policy
from stock_market_making.strategies.baseline.run import load_strategy
from stock_market_making.strategies.hybrid.state import StateStore
from stock_market_making.recording.storage import MARKET_DIR, RUNS_DIR, hybrid_state_path


def load_config(path=None):
    cfg = dict(DEFAULTS)
    cfg.update(json.loads((DIRECTORY.parent / 'pair/config.json').read_text(encoding='utf-8')))
    cfg.update(json.loads((DIRECTORY / 'config.json').read_text(encoding='utf-8')))
    if path:
        cfg.update(json.loads(Path(path).read_text(encoding='utf-8')))
    validate_config(cfg)
    if cfg['symbols'] != ['PHILIPS_A', 'PHILIPS_B'] or cfg['relation_mode'] != 'cycle':
        raise ValueError('Hybrid requires PHILIPS_A/B in that order and cycle mode')
    for key in ('mm_position_limit', 'mm_soft_limit', 'mm_order_volume', 'pair_position_limit', 'pair_lot_size'):
        if isinstance(cfg[key], bool) or not isinstance(cfg[key], int) or not 0 < cfg[key] <= 100:
            raise ValueError('Invalid ' + key)
    if (not cfg['mm_order_volume'] <= cfg['mm_soft_limit'] <= cfg['mm_position_limit'] <= cfg['position_limit']
            or not cfg['pair_lot_size'] <= cfg['pair_position_limit'] <= cfg['position_limit']):
        raise ValueError('Invalid strategy allocations')
    if (isinstance(cfg['mm_max_spread_ticks'], bool)
            or not isinstance(cfg['mm_max_spread_ticks'], (int, float))
            or not 0 < cfg['mm_max_spread_ticks'] <= 100):
        raise ValueError('Invalid MM spread limit')
    # Reject invalid pair settings before creating or connecting an exchange.
    Policy(dict(cfg, position_limit=cfg['pair_position_limit'], lot_size=cfg['pair_lot_size']))
    return cfg


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--live', action='store_true')
    mode.add_argument('--check', action='store_true', help='Validate config and baseline definitions without connecting')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--account', default='default', help='Local state namespace; does not select exchange credentials')
    parser.add_argument('--state-file', type=Path,
                        help='Persistent ownership checkpoint; use the same file on every restart')
    parser.add_argument('--log-dir', type=Path, default=RUNS_DIR / 'hybrid')
    parser.add_argument('--price-data-dir', type=Path, default=MARKET_DIR)
    args = parser.parse_args()
    try:
        args.state_file = args.state_file or hybrid_state_path(args.account)
        cfg = load_config(args.config)
        mm_strategy = load_strategy()
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    if args.check:
        print('Hybrid configuration and baseline definitions OK. No exchange connected.')
        return 0
    from optibook.synchronous_client import Exchange
    from stock_market_making.recording.shared_market_recording import RecordingExchange
    raw = RecordingExchange(Exchange(max_nr_trade_history=10000), args.price_data_dir)
    journal = Journal(args.log_dir, strategy='hybrid', config=cfg, account=args.account,
                      state_file=str(args.state_file.resolve()))
    engine = None
    state = StateStore(args.state_file)
    failed = False
    exit_reason = 'runner_exit'
    try:
        state.acquire()
        restored = state.load(cfg)
        raw.connect()
        engine = Hybrid(raw, cfg, journal, mm_strategy=mm_strategy,
                        state_store=state, restored=restored)
        raw.start_recording()
        journal.storage.link_market(raw.recorder.directory)
        journal.emit('settings', strategy='hybrid', config=cfg)
        while raw.is_connected() and time.monotonic() < engine.account.deadline:
            try:
                engine.step()
                if engine.stopping and not any(engine.account.positions['pair'].values()):
                    break
            finally:
                raw.sample_market_data()
                time.sleep(cfg['loop_seconds'])
        exit_reason = 'connection_lost' if not raw.is_connected() else (
            'session_deadline' if time.monotonic() >= engine.account.deadline else 'stop_requested')
    except KeyboardInterrupt:
        exit_reason = 'keyboard_interrupt'
        failed = engine is None
    except Exception as exc:
        exit_reason = 'execution_fault'
        failed = True
        if engine:
            engine.account.halted = True
        journal.emit('hybrid_fault', error=str(exc))
        print(str(exc), file=sys.stderr)
    finally:
        try:
            if engine:
                try:
                    summary = engine.finish(reason=exit_reason)
                    print(json.dumps(summary, ensure_ascii=False))
                    failed |= summary['halted'] or not summary['pair_flat'] or summary['unresolved_pair_ioc']
                except Exception as exc:
                    failed = True
                    journal.emit('state_shutdown_failed', error=str(exc))
                    print(f'Final ownership checkpoint failed: {exc}', file=sys.stderr)
        finally:
            try:
                raw.disconnect()
            finally:
                try:
                    journal.close()
                finally:
                    state.close()
    return 2 if failed or journal.failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
