"""Launch the volatility-adaptive market maker; live mode is explicit."""

import argparse
import json
from pathlib import Path
import sys


DIRECTORY = Path(__file__).resolve().parent
sys.path.insert(0, str(DIRECTORY.parents[2]))

from stock_market_making.strategies.volatility_adaptive import strategy


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--live', action='store_true', help='Connect and send orders')
    mode.add_argument('--check', action='store_true', help='Validate settings without connecting')
    args = parser.parse_args(argv)
    if not args.live:
        print(json.dumps(strategy.check(), indent=2))
        return 0
    strategy.main()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

