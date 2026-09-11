"""Recording details for the stock strategy; reuse its existing connection.

RecordedExchange delegates requests to the supplied Exchange. Only inserts and
cancellations add events; this adapter never connects, polls trades, or sleeps.
StrategyRecorder keeps the original TradeLogger JSONL format and sampling logic.
"""

from stock_market_making.recording.trade_logger import TradeLogger


class StrategyRecorder(TradeLogger):
    def quote(self, instrument_id, book, position, quote):
        """Record the decision and the book that was used to calculate it."""
        self.write(
            'quote', instrument=instrument_id, position=position,
            book_timestamp=book.timestamp,
            best_bid=book.bids[0].price, best_ask=book.asks[0].price,
            **quote,
        )


class RecordedExchange:
    """An Exchange adapter that records each order request and its response.

    Other methods go directly to the existing Exchange. Return values and
    exceptions are unchanged. Timing and error handling belong to the strategy.
    """

    def __init__(self, exchange, recorder):
        self._exchange = exchange
        self._recorder = recorder

    def __getattr__(self, name):
        return getattr(self._exchange, name)

    def delete_orders(self, instrument_id, *, reason=None):
        fields = {'instrument': instrument_id}
        if reason is not None:
            fields['reason'] = reason
        self._recorder.event('cancel_requested', **fields)
        result = self._exchange.delete_orders(instrument_id)
        self._recorder.event('cancel_returned', **fields)
        return result

    def insert_order(self, instrument_id, *, price, volume, side, order_type='limit'):
        fields = dict(instrument=instrument_id, side=side, price=price, volume=volume)
        self._recorder.event('order_attempt', **fields)
        response = self._exchange.insert_order(
            instrument_id, price=price, volume=volume,
            side=side, order_type=order_type,
        )
        self._recorder.write(
            'order_response', **fields, success=response.success,
            order_id=response.order_id, error_reason=response.error_reason,
        )
        return response

    def delete_order(self, instrument_id, *, order_id):
        fields = dict(instrument=instrument_id, order_id=order_id)
        self._recorder.event('cancel_order_requested', **fields)
        response = self._exchange.delete_order(instrument_id, order_id=order_id)
        self._recorder.write('cancel_response', **fields, success=response.success,
                             error_reason=response.error_reason)
        return response

    def amend_order(self, instrument_id, *, order_id, volume):
        fields = dict(instrument=instrument_id, order_id=order_id, volume=volume)
        self._recorder.event('amend_requested', **fields)
        response = self._exchange.amend_order(instrument_id, order_id=order_id, volume=volume)
        self._recorder.write('amend_response', **fields, success=response.success,
                             error_reason=response.error_reason)
        return response
