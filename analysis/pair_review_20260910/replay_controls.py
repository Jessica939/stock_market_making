"""Compare holding/size controls under the same offline terminal-fill assumptions."""
from collections import Counter
import json
import gzip
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT.parent))
from stock_market_making.strategies.common.runner import DEFAULTS, Feed, validate_config
from stock_market_making.strategies.common.simulation import ReplayExchange, SimClock, read_frames
from stock_market_making.strategies.pair.policy import Policy
from stock_market_making.strategies.pair.holding import validate_holding_config
from stock_market_making.strategies.pair.session import PairSession


class Journal:
    failed = False
    def __init__(self):
        self.counts = Counter()
        self.exit_reasons = Counter()
    def emit(self, kind, **fields):
        self.counts[kind] += 1
        if kind == 'pair_exit_requested':
            self.exit_reasons[fields['reason']] += 1


def run(path, variant):
    config = dict(DEFAULTS)
    config.update(json.loads((ROOT/'strategies/pair/config.json').read_text()))
    if variant == 'previous_controls':
        config['max_hold_seconds'] = 45
        config['cycle_extension_seconds'] = config['cycle_path_grace_seconds'] = 0
        config['max_order_lots'] = 20
        for key in list(config):
            if key.startswith('pair_'):
                del config[key]
    validate_config(config)
    validate_holding_config(config)
    if variant == 'three_second_controls':
        config['pair_independent_holding'] = False
        config['max_hold_seconds'] = 45
        config['cycle_extension_seconds'] = config['cycle_path_grace_seconds'] = 0
    if variant == 'independent_45s':
        config['max_hold_seconds'] = 45
        config['cycle_extension_seconds'] = config['cycle_path_grace_seconds'] = 0
    clock, journal = SimClock(), Journal()
    exchange = ReplayExchange(config['symbols'], clock, fill_fraction=.5)
    session = None
    for raw in read_frames(str(path), config['symbols']):
        if session is None:
            first_epoch = raw['epoch']
        elapsed = raw['epoch'] - first_epoch
        if elapsed > config['session_seconds']:
            break
        clock.now = max(clock.now, elapsed)
        exchange.advance(raw)
        if session is None:
            feed = Feed(exchange, config['symbols'], config, clock.monotonic,
                        lambda:first_epoch+clock.now, journal)
            session = PairSession(Policy(config), 'pair', exchange, feed, config, journal,
                                  clock.monotonic, clock.sleep,
                                  terminal_quantity=exchange.ioc_terminal_quantity)
            if variant == 'previous_controls':
                session.observe_inventory = lambda *a, **kw: False
        session.step(delayed=True)
        clock.sleep(config['loop_seconds'])
        if session.stop_requested:
            break
    if session is None:
        return dict(recording=path.parent.name, variant=variant, empty=True)
    summary = session.finish(min(config['shutdown_grace_seconds'],
                                 max(0, session.executor.reduction_deadline-clock.now)))
    return dict(recording=path.parent.name, variant=variant, counts=dict(journal.counts),
                exit_reasons=dict(journal.exit_reasons), summary=summary)


if __name__ == '__main__':
    results = []
    for path in sorted((ROOT/'data/market').glob('*/orderbooks_00001.jsonl.gz')):
        try:
            with gzip.open(path, 'rb') as stream:
                while stream.read(1024 * 1024):
                    pass
        except (EOFError, OSError) as exc:
            results.append(dict(recording=path.parent.name, excluded=True, error=str(exc)))
            print(path.parent.name, 'EXCLUDED:', exc, flush=True)
            continue
        for variant in ('previous_controls', 'three_second_controls', 'independent_45s', 'new_controls'):
            result = run(path, variant)
            results.append(result)
            s = result.get('summary', {})
            print(path.parent.name, variant, 'pnl=', s.get('pnl_change'),
                  'flat=',s.get('flat'), 'fault=',s.get('fatal_error'), flush=True)
    Path(__file__).with_name('replay_controls.json').write_text(
        json.dumps(dict(assumptions='Snapshot replay, 50% displayed liquidity, simulator terminal proof in BOTH variants; previous_controls is a control ablation, not exact historical code.',
                       results=results), indent=2)+'\n', encoding='utf-8')
