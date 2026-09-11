"""Poll private trades once and share confirmed fills with execution and logs."""
from collections import defaultdict


class FillStream:
    """Exchange adapter: execution can refresh without stealing recorder trades.

    Totals are confirmed executions, not inferred from insert success, an empty
    poll, or an absent resting order. Repeated trade IDs are counted only once.
    """

    def __init__(self, exchange):
        self.exchange = exchange
        self._seen = {}
        self._totals = defaultdict(int)
        self._for_recorder = defaultdict(list)

    def __getattr__(self, name):
        return getattr(self.exchange, name)

    def refresh(self, instrument_id):
        trades = self.exchange.poll_new_trades(instrument_id)
        if trades is None:
            raise RuntimeError('private trade poll did not return a batch')
        confirmed = 0
        for trade in trades:
            if type(trade.volume) is not int or trade.volume <= 0 or trade.side not in ('bid', 'ask'):
                raise ValueError('invalid private trade quantity or side')
            key = (instrument_id, trade.trade_id)
            fields = (trade.order_id, trade.side, trade.price, trade.volume, trade.timestamp)
            if key in self._seen:
                if self._seen[key] != fields:
                    raise RuntimeError('private trade ID was reused with different fields')
                continue
            self._seen[key] = fields
            self._totals[instrument_id, trade.order_id] += trade.volume
            self._for_recorder[instrument_id].append(trade)
            confirmed += trade.volume
        return confirmed

    def confirmed_volume(self, instrument_id, order_id):
        return self._totals[instrument_id, order_id]

    def poll_new_trades(self, instrument_id):
        self.refresh(instrument_id)
        trades = self._for_recorder[instrument_id]
        self._for_recorder[instrument_id] = []
        return trades
