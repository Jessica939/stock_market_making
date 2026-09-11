"""baseline-refine market making plus B stale IOC sniping; live is explicit."""
import argparse
import json
from pathlib import Path
import re
import signal
import sys
import time
import traceback

DIRECTORY = Path(__file__).resolve().parent
ROOT = DIRECTORY.parents[1]
sys.path.insert(0, str(ROOT.parent))

from stock_market_making.recording.storage import MARKET_DIR, RUNS_DIR
from stock_market_making.strategies.common.runner import Journal
from stock_market_making.strategies.stale_quote_sniping.engine import validate as validate_stale
from stock_market_making.strategies.baseline_stale.engine import CombinedEngine
from stock_market_making.strategies.baseline_stale.state import StateStore
from stock_market_making.strategies.baseline_refine_loader import load_baseline_refine

VERSION = 'baseline_stale_v1'


def step_or_recover(engine, raw, journal):
    """Run one strategy iteration, recovering only while state is confirmed.

    Unknown insert/cancel outcomes and attribution faults set one of the hard
    fault flags before raising.  Those exceptions must still reach shutdown.
    Other exceptions are isolated to one loop after all passive MM orders are
    cancelled and the shared inventory is successfully audited.
    """
    try:
        engine.step()
        return True
    except Exception as exc:
        if (engine.account.halted or engine.stale.executor.hard_fault
                or not raw.is_connected()):
            raise
        engine.account.cancel_owner('mm')
        actual = engine.account.audit()
        detail = traceback.format_exc()
        journal.emit('combined_step_recovered', error=str(exc),
                     error_type=type(exc).__name__, traceback=detail,
                     actual_positions=actual,
                     baseline_positions=engine.account.positions['mm'].copy(),
                     stale_positions=engine.account.positions['pair'].copy())
        print(f'Recovered {type(exc).__name__} in strategy loop; continuing: {exc}',
              file=sys.stderr, flush=True)
        return False


def load_config(path=None):
    config = json.loads((DIRECTORY / 'config.json').read_text(encoding='utf-8'))
    if path:
        config.update(json.loads(Path(path).read_text(encoding='utf-8')))
    stale_path = DIRECTORY.parent / 'stale_quote_sniping' / 'config.json'
    stale = json.loads(stale_path.read_text(encoding='utf-8'))
    stale.update(max_order_lots=config['stale_b_position_limit'],
                 session_seconds=config['session_seconds'],
                 closeout_seconds=config['closeout_seconds'],
                 loop_seconds=config['loop_seconds'],
                 max_updates_per_second=config['max_updates_per_second'])
    validate_stale(stale)
    if config['symbols'] != ['PHILIPS_A', 'PHILIPS_B']:
        raise ValueError('symbols must be PHILIPS_A, PHILIPS_B')
    for key in ('position_limit', 'max_net_lots', 'max_outstanding_volume',
                'max_updates_per_second', 'baseline_b_position_limit',
                'baseline_b_soft_limit', 'stale_b_position_limit'):
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(key + ' must be a positive integer')
    if not config['baseline_b_soft_limit'] <= config['baseline_b_position_limit'] <= config['position_limit'] <= 100:
        raise ValueError('invalid baseline B allocation')
    if (config['stale_b_position_limit'] > config['position_limit']
            or config['baseline_b_position_limit'] + config['stale_b_position_limit']
            > config['position_limit']):
        raise ValueError('combined baseline/stale B allocation exceeds position_limit')
    return config, stale


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--live', action='store_true')
    mode.add_argument('--check', action='store_true')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--account', default='default',
                        help='Local state namespace; does not select exchange credentials')
    parser.add_argument('--state-file', type=Path)
    parser.add_argument('--adopt-current', action='store_true',
                        help='Explicitly assign current A/B inventory to baseline and reset stale ownership')
    parser.add_argument('--log-dir', type=Path, default=RUNS_DIR / 'baseline_stale')
    parser.add_argument('--price-data-dir', type=Path, default=MARKET_DIR)
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', args.account):
            raise ValueError('account must contain only letters, numbers, underscores or hyphens')
        args.state_file = args.state_file or ROOT / 'state' / args.account / 'baseline_stale.json'
        if args.adopt_current and not args.live:
            raise ValueError('--adopt-current requires --live')
        config, stale = load_config(args.config)
        baseline = load_baseline_refine()
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        parser.error(str(exc))
    if not args.live:
        print(json.dumps(dict(strategy=VERSION, config=config, stale=stale,
                              baseline_version=baseline['STRATEGY_VERSION'],
                              execution='one connection, one fill consumer, owner-attributed inventory'),
                         ensure_ascii=False, indent=2))
        return 0

    from optibook.synchronous_client import Exchange
    from stock_market_making.recording.shared_market_recording import RecordingExchange
    raw = RecordingExchange(Exchange(max_nr_trade_history=10000), args.price_data_dir)
    journal = Journal(args.log_dir, strategy=VERSION, config=config, stale_config=stale)
    engine = None
    failed = False
    state = StateStore(args.state_file)

    def stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        state.acquire()
        if args.adopt_current:
            archive = state.archive_for_adoption()
            restored = None
            journal.emit('ownership_adoption_requested', prior_state_backup=(
                str(archive) if archive is not None else None))
        else:
            restored = state.load(config)
        raw.connect()
        engine = CombinedEngine(raw, config, stale, journal, baseline,
                                price_data_dir=args.price_data_dir, restored=restored)
        state.checkpoint(engine)
        if args.adopt_current:
            journal.emit('ownership_adopted', baseline_positions=engine.account.positions['mm'],
                         stale_positions=engine.account.positions['pair'])
        raw.start_recording()
        journal.storage.link_market(raw.recorder.directory)
        journal.emit('settings', strategy_version=VERSION, config=config, stale_config=stale)
        while raw.is_connected() and time.monotonic() < engine.account.deadline:
            try:
                state.invalidate(config)
                step_or_recover(engine, raw, journal)
                # Persist attribution for manual recovery. Resting orders or a
                # stale position keep it unrecoverable until clean shutdown.
                if (state.data.get('positions') != engine.account.positions
                        or state.data.get('cash') != engine.account.cash):
                    state.checkpoint(engine)
            finally:
                raw.sample_market_data()
                time.sleep(config['loop_seconds'])
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        failed = True
        if engine is not None:
            engine.account.halted = True
        detail = traceback.format_exc()
        journal.emit('combined_fault', error=str(exc), traceback=detail)
        print(detail, file=sys.stderr)
    finally:
        try:
            if engine is not None:
                summary = engine.finish('runner_exit')
                state.checkpoint(engine)
                failed |= summary['halted'] or not summary['stale_flat']
                print(json.dumps(summary, ensure_ascii=False))
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
