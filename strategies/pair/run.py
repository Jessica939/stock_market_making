"""Run the relative-value policy as a plain script; demo is the default."""
from pathlib import Path
import sys


DIRECTORY = Path(__file__).resolve().parent
sys.path.insert(0, str(DIRECTORY.parents[2]))

from stock_market_making.strategies.common.runner import main
from stock_market_making.strategies.pair.policy import Policy


if __name__ == '__main__':
    raise SystemExit(main(Policy, 'pair', DIRECTORY))
