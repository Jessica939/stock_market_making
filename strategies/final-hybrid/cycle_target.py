"""Reuse baseline-refine's cycle forecast and frozen target controller."""
import importlib
import math
from pathlib import Path

_package = 'stock_market_making.strategies.baseline-refine'
_signal = importlib.import_module(_package + '.cycle_signal')
CyclePosition = importlib.import_module(_package + '.cycle_position').CyclePosition
bootstrap_cycle = importlib.import_module(_package + '.cycle_history').bootstrap_cycle


def settings(config):
    options = config.get('b_cycle', {})
    if not isinstance(options, dict) or set(options) - {'enabled', 'signal', 'position'}:
        raise ValueError('b_cycle requires enabled, signal and position settings')
    enabled = options.get('enabled', False)
    if type(enabled) is not bool:
        raise ValueError('b_cycle.enabled must be boolean')
    signal = _signal.CycleSettings(**dict(
        {'horizon_seconds': 45., 'max_age_seconds': config['max_book_age_seconds'],
         'max_pair_gap_seconds': config['max_pair_time_gap_seconds'],
         'max_spread_ticks': config['max_spread_ticks']}, **options.get('signal', {})))
    position = CyclePosition(**options.get('position', {}))
    return enabled, signal, position


class TargetCycle:
    def __init__(self, config):
        self.enabled, signal, self.position = settings(config)
        self.model = _signal.CycleSignal(signal)
        self.signal = dict(active=False)
        self.last_bootstrap = -math.inf
        self.scanned = False

    def observe(self, raw, exchange, ticks, now, wall, event):
        if not self.enabled:
            return
        if self.model.needs_bootstrap and now-self.last_bootstrap >= 5:
            self.last_bootstrap = now
            report = bootstrap_cycle(self.model, exchange,
                Path(__file__).resolve().parents[2] / 'data/market', ticks, now, wall,
                scan_recordings=not self.scanned)
            self.scanned = True
            event('cycle_bootstrap', **report)
        self.signal = self.model.observe(raw, ticks, now, wall)
        event('cycle_signal', signal=self.signal)

    def quote(self, raw, quantity, tick, now):
        return self.position.apply(dict(cycle=self.signal,
            buy_volume=self.position.target_lots, sell_volume=self.position.target_lots),
            raw, quantity, tick, now)
