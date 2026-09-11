"""Import helper for the on-disk ``baseline-refine`` package name."""
import importlib


def load_baseline_refine():
    module = importlib.import_module('stock_market_making.strategies.baseline-refine.run')
    # The combined runner owns the live loop; importing the notebook's main cell
    # would duplicate its connection, recorder and sender.
    return module.load_quote_definitions()
