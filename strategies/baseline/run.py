"""Compatibility entry point for the baseline Python strategy."""
import argparse
from pathlib import Path


def load_strategy():
    path = Path(__file__).with_name('strategy.py')
    namespace = {'__name__': 'baseline_strategy', '__file__': str(path)}
    exec(compile(path.read_text(encoding='utf-8'), str(path), 'exec'), namespace)
    required = ('CYCLE_SETTINGS', 'calculate_quote', 'main')
    missing = [name for name in required if name not in namespace]
    if missing:
        raise ValueError(
            f'{path}: missing strategy definitions: {", ".join(missing)}.')
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
