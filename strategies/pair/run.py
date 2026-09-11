"""Run the relative-value policy as a plain script; demo is the default."""
from pathlib import Path
import sys


DIRECTORY = Path(__file__).resolve().parent
sys.path.insert(0, str(DIRECTORY.parents[2]))

from stock_market_making.strategies.common.runner import main
from stock_market_making.strategies.pair.holding import validate_holding_config
from stock_market_making.strategies.pair.policy import Policy
from stock_market_making.strategies.pair.session import PairSession


def prepare_config(config):
    """Supply pair-only timing defaults after CLI overrides are applied."""
    config.setdefault('entry_cutoff_seconds', min(120.0, config['session_seconds'] / 2))
    config.setdefault('liquidation_buffer_seconds',
                      min(60.0, config['entry_cutoff_seconds'] / 2))


def run(argv=None):
    return main(Policy, 'pair', DIRECTORY, argv, session_class=PairSession,
                prepare_config=prepare_config,
                strategy_validator=validate_holding_config)


if __name__ == '__main__':
    raise SystemExit(run())
