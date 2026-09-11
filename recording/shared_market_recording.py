"""Record full books and copies of public trades through a bot's one connection."""
from collections import defaultdict
from pathlib import Path
import time

from stock_market_making.recording.record_philips_prices import PhilipsPriceRecorder


class _RecordingView:
    def __init__(self, exchange):
        self.exchange = exchange

    def __getattr__(self, name):
        return getattr(self.exchange.raw, name)

    def poll_new_trade_ticks(self, instrument_id):
        # The strategy owns the real consumer. Recording drains only its copy.
        return self.exchange.batches.pop(instrument_id, [])


class RecordingExchange:
    def __init__(self, raw, directory, *, interval=0.5, clock=time.monotonic):
        self.raw, self.directory = raw, Path(directory)
        self.interval, self.clock = interval, clock
        self.batches = defaultdict(list)
        self.recorder = None
        self.last_sample = -float('inf')

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def start_recording(self):
        if self.recorder is None:
            self.recorder = PhilipsPriceRecorder(_RecordingView(self), self.directory)
            print(f'Market data (same connection): {self.recorder.directory}', flush=True)

    def poll_new_trade_ticks(self, instrument_id):
        ticks = self.raw.poll_new_trade_ticks(instrument_id)
        if self.recorder is not None and ticks:
            self.batches[instrument_id].extend(ticks)
        return ticks

    def sample_market_data(self):
        if (self.recorder is not None and self.raw.is_connected()
                and self.clock() - self.last_sample >= self.interval):
            self.recorder.sample()
            self.last_sample = self.clock()

    def disconnect(self):
        try:
            if self.recorder is not None:
                try:
                    if self.raw.is_connected():
                        self.recorder.sample()
                    else:
                        self.recorder.drain_trades()
                finally:
                    self.recorder.close()
                    self.recorder = None
        finally:
            self.raw.disconnect()
