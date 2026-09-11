"""Shared public interface for passive market-making execution."""

# Keep the original modules importable for existing notebooks and deployments.
# New strategy code should use this facade so shared infrastructure has one API.
from stock_market_making.order_execution import LimitedExchange, QuoteManager
from stock_market_making.quote_helpers import external_price_book

__all__ = ['LimitedExchange', 'QuoteManager', 'external_price_book']
