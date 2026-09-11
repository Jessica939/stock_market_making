"""Load the isolated baseline-refine strategy; connect only with --live."""
import argparse
from dataclasses import asdict
import importlib
import json
import math
from pathlib import Path
import sys

DIRECTORY = Path(__file__).resolve().parent
PACKAGE = 'stock_market_making.strategies.baseline-refine'
sys.path.insert(0, str(DIRECTORY.parents[2]))


def load_quote_definitions():
    """Load the actual parameters and quote function without SDK/logger setup."""
    cycle = importlib.import_module(PACKAGE + '.cycle_signal')
    importlib.import_module(PACKAGE + '.cycle_history')
    protection = importlib.import_module(PACKAGE + '.quote_protection')
    namespace = dict(math=math, CycleSettings=cycle.CycleSettings,
                     ProtectionSettings=protection.ProtectionSettings,
                     apply_cycle_quote=cycle.apply_cycle_quote, protect_quote=protection.protect_quote,
                     RUNS_DIR=DIRECTORY.parents[1]/'data/runs',
                     MARKET_DIR=DIRECTORY.parents[1]/'data/market')
    notebook = json.loads((DIRECTORY/'strategy_with_logging.ipynb').read_text(encoding='utf-8'))
    for cell in notebook['cells']:
        source = ''.join(cell['source'])
        if source.startswith('TRADE_INSTRUMENTS =') or source.startswith('def calculate_quote('):
            exec(compile(source, str(DIRECTORY/'strategy_with_logging.ipynb'), 'exec'), namespace)
    position = importlib.import_module(PACKAGE + '.cycle_position')
    position.CyclePosition(**namespace['B_POSITION_SETTINGS'])
    orders = importlib.import_module(PACKAGE + '.order_execution')
    orders.QuoteManager(None, position_limit=namespace['POSITION_LIMIT'],
                        soft_limit=namespace['SOFT_LIMIT'], net_position_limit=namespace['NET_POSITION_LIMIT'])
    return namespace


def load_strategy():
    path = Path(__file__).with_name('strategy_with_logging.ipynb')
    notebook = json.loads(path.read_text(encoding='utf-8'))
    namespace = {'__name__': 'baseline_refine', '__package__': PACKAGE, '__file__': str(path)}
    for cell in notebook['cells']:
        if cell['cell_type'] == 'code' and 'strategy' in cell.get('metadata', {}).get('tags', []):
            exec(compile(''.join(cell['source']), str(path), 'exec'), namespace)
    required = ('CYCLE_SETTINGS', 'calculate_quote', 'main')
    missing = [name for name in required if name not in namespace]
    if missing:
        raise ValueError(
            f'{path}: missing strategy definitions: {", ".join(missing)}. '
            'Upload the current strategy_with_logging.ipynb including its cell metadata; '
            'definition cells must retain the strategy tag. Do not run launch cells to fix this.')
    if not all(callable(namespace[name]) for name in ('calculate_quote', 'main')):
        raise ValueError(f'{path}: calculate_quote and main must be callable')
    return namespace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--live', action='store_true', help='Connect to Optibook and trade; Ctrl+C stops')
    mode.add_argument('--check', action='store_true', help='Check parameters/quotes offline; SDK/logger deployment is not checked')
    args = parser.parse_args()
    if args.check:
        strategy = load_quote_definitions()
        print(json.dumps(dict(strategy=strategy['STRATEGY_VERSION'],
                              position=strategy['B_POSITION_SETTINGS'],
                              exchange_position_limit=strategy['EXCHANGE_POSITION_LIMIT'],
                              net_position_limit=strategy['NET_POSITION_LIMIT'],
                              net_position_scope='PHILIPS_A + PHILIPS_B and same-side resting orders',
                              a_entry_volume=strategy['ORDER_VOLUME'],
                              loop_seconds=strategy['LOOP_SECONDS'],
                              max_updates_per_second=strategy['MAX_UPDATES_PER_SECOND'],
                              horizon_seconds=strategy['CYCLE_SETTINGS'].horizon_seconds,
                              cycle_settings=asdict(strategy['CYCLE_SETTINGS']),
                              online_adaptation='time-ordered period/window selection and issued forecast monitoring',
                              startup_history='recent recorded midpoints, then public trade history cache',
                              live_dependencies_checked=False), indent=2))
        return
    load_strategy()['main']()


if __name__ == '__main__':
    main()
