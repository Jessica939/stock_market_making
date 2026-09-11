"""Load baseline notebook definitions without executing its launch/analysis cells."""
import argparse
import json
from pathlib import Path


def load_strategy():
    path = Path(__file__).with_name('strategy_with_logging.ipynb')
    notebook = json.loads(path.read_text(encoding='utf-8'))
    namespace = {'__name__': 'baseline_strategy', '__file__': str(path)}
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
    parser.add_argument('--live', action='store_true', help='Connect to Optibook and trade; Ctrl+C stops')
    args = parser.parse_args()
    if not args.live:
        parser.error('Explicit --live is required to connect and send orders')
    load_strategy()['main']()


if __name__ == '__main__':
    main()
