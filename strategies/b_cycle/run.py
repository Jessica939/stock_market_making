"""B-only cycle trading. No connection without explicit --live."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
import time

DIRECTORY=Path(__file__).resolve().parent
ROOT=DIRECTORY.parents[1]
sys.path.insert(0,str(ROOT.parent))
from stock_market_making.strategies.b_cycle.engine import Engine, SYMBOLS, validate
from stock_market_making.strategies.baseline.cycle_signal import CycleSettings
from stock_market_making.strategies.common.runner import Journal
from stock_market_making.strategies.common.simulation import SimClock, ReplayExchange, read_frames
from stock_market_making.strategies.hybrid.state import StateStore


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--live',action='store_true')
    mode.add_argument('--replay',help='One chronological full-depth recording path/glob')
    mode.add_argument('--check',action='store_true',help='Validate without connecting (default)')
    parser.add_argument('--config',type=Path,default=DIRECTORY/'config.json')
    parser.add_argument('--duration',type=float)
    parser.add_argument('--fill-fraction',type=float,default=.5)
    parser.add_argument('--state-file',type=Path,default=ROOT/'state/default/b_cycle.json')
    parser.add_argument('--log-dir',type=Path,default=ROOT/'data/runs/b_cycle')
    args=parser.parse_args(argv)
    try:
        config=json.loads(args.config.read_text(encoding='utf-8'))
        if args.duration is not None:
            config['session_seconds']=args.duration
        validate(config)
        if not 0<args.fill_fraction<=1:
            raise ValueError('fill-fraction must be in (0,1]')
    except (OSError,ValueError,TypeError,KeyError) as exc:
        parser.error(str(exc))
    if not args.live and not args.replay:
        print(json.dumps(dict(strategy='b_cycle_v1',config=config,cycle=asdict(CycleSettings())),indent=2))
        return 0
    journal=Journal(args.log_dir,strategy='b_cycle',mode='live' if args.live else 'replay',
                    config=config,cycle_settings=asdict(CycleSettings()),replay_source=args.replay)
    journal.emit('settings',strategy_version='b_cycle_v1',config=config,
                 cycle_settings=asdict(CycleSettings()),fill_fraction=None if args.live else args.fill_fraction)
    exchange=engine=guard=None;summary={'flat':False};error=None;armed=False
    try:
        if args.live:
            guard=StateStore(args.state_file);guard.acquire()
            if guard.path.exists():
                old=json.loads(guard.path.read_text(encoding='utf-8'))
                if old.get('strategy')!='b_cycle_v1' or old.get('safe_to_start') is not True:
                    raise ValueError('Previous B cycle run is unconfirmed or risk-stopped; reconcile account and checkpoint before restart')
            from optibook.synchronous_client import Exchange
            from stock_market_making.recording.shared_market_recording import RecordingExchange
            exchange=RecordingExchange(Exchange(max_nr_trade_history=10000),ROOT/'data/market')
            exchange.connect()
            engine=Engine(exchange,config,journal,time.monotonic,time.sleep,time.time)
            engine.startup()
            guard.write(dict(strategy='b_cycle_v1',safe_to_start=False,reason='active_run',
                             run_id=journal.storage.run_id,config=config));armed=True
            exchange.start_recording();journal.storage.link_market(exchange.recorder.directory)
            print(f'B cycle v1: 2-lot maximum; log: {journal.path}',flush=True)
            while exchange.is_connected() and engine.clock()<engine.end:
                engine.step();exchange.sample_market_data()
                if engine.stopped and engine.executor.positions()['PHILIPS_B']==0:
                    break
                time.sleep(config['loop_seconds'])
        else:
            clock=SimClock();exchange=ReplayExchange(SYMBOLS,clock,config['fee_per_lot'],args.fill_fraction)
            first=None
            for frame in read_frames(args.replay,SYMBOLS,.1):
                if first is None:
                    first=frame['epoch']
                clock.now=max(clock.now,frame['epoch']-first)
                exchange.advance(frame)
                if engine is None:
                    engine=Engine(exchange,config,journal,clock.monotonic,clock.sleep,
                                  lambda:first+clock.now,terminal_quantity=exchange.ioc_terminal_quantity)
                    engine.startup()
                if clock.now>=engine.end:
                    break
                engine.step()
                if engine.stopped and engine.executor.positions()['PHILIPS_B']==0:
                    break
            if engine is None:
                raise ValueError('No replay frames')
    except KeyboardInterrupt:
        print('Stopping entries and attempting confirmed-position closeout.',flush=True)
    except Exception as exc:
        error=str(exc);journal.emit('fatal_error',error=error)
    finally:
        try:
            if engine is not None:
                try:
                    summary=engine.finish(live=args.live and armed)
                except Exception as exc:
                    error=error or str(exc);journal.emit('finish_error',error=str(exc))
            if guard is not None and armed:
                safe=summary.get('flat') is True and not summary.get('risk_stopped') and error is None and not journal.failed
                guard.write(dict(strategy='b_cycle_v1',safe_to_start=safe,summary=summary,error=error,
                                 run_id=journal.storage.run_id,config=config))
        finally:
            try:
                if exchange is not None:
                    exchange.disconnect()
            finally:
                if guard is not None:
                    guard.close()
                journal.close()
    print(json.dumps(dict(summary=summary,error=error),ensure_ascii=False),flush=True)
    return 0 if summary.get('flat') and error is None and not journal.failed else 2


if __name__=='__main__':
    raise SystemExit(main())
