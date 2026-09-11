"""One conservative budget at the public SDK boundary, including reads."""
from collections import Counter, deque
from functools import wraps
import logging
import math
import time


class RequestBudget:
    def __init__(self, maximum=200, *, clock=time.monotonic, sleep=time.sleep):
        if type(maximum) is not int or not 1 <= maximum <= 200:
            raise ValueError('request budget must be an integer in 1..200')
        self.maximum, self.clock, self.sleep = maximum, clock, sleep
        self.capacity = max(1, maximum-math.ceil(maximum*.05))
        # Leave a boundary margin for server-side receipt time and housekeeping.
        self.window = 1.05
        self.sent = deque()
        self.counts = Counter()
        self.wait_seconds = 0.
        self.last_now = -math.inf

    def wait(self, slots=1):
        slots = min(slots, self.capacity)
        while True:
            now = self.clock()
            if not math.isfinite(now) or now < self.last_now:
                raise RuntimeError('invalid or reversed request-budget clock')
            self.last_now = now
            while self.sent and now-self.sent[0] >= self.window:
                self.sent.popleft()
            if len(self.sent)+slots <= self.capacity:
                return
            delay = max(.001, self.sent[0]+self.window-now+.001)
            self.wait_seconds += delay
            self.sleep(delay)

    def call(self, name, method, *args, **kwargs):
        self.wait()
        # Account for the actual API invocation, even when it raises/rejects.
        self.sent.append(self.clock())
        self.counts[name] += 1
        return method(*args, **kwargs)

    def acquire(self):
        """Leave room for an order's fresh preflight reads plus its submission.

        This does not count/reserve an imaginary order. Actual read/write calls
        below the sender each consume their own slot at invocation time.
        """
        self.wait(32)

    def snapshot(self):
        return dict(max_requests_per_second=self.maximum, window_seconds=self.window,
                    client_call_budget=self.capacity,
                    calls=dict(self.counts), wait_seconds=self.wait_seconds,
                    scope='conservative_public_sdk_calls_including_reads')


class BudgetedExchange:
    """Place underneath recording, fills and senders so none bypass the budget.

    The local reference does not identify which getters are RPCs in the deployed
    SDK. Count getters and polls conservatively, even when an SDK caches them.
    is_connected/disconnect are lifecycle checks, never a trading data snapshot.
    """
    def __init__(self, raw, budget):
        self.raw, self.request_budget = raw, budget

    def __getattr__(self, name):
        value = getattr(self.raw, name)
        limited = (name.startswith(('get_', 'poll_'))
                   or name in ('connect', 'insert_order', 'amend_order', 'delete_order', 'delete_orders'))
        if not callable(value) or not limited:
            return value
        @wraps(value)
        def call(*args, **kwargs):
            return self.request_budget.call(name, value, *args, **kwargs)
        return call


class DisconnectCapture(logging.Handler):
    """Keep the server reason instead of hiding it behind the next API error."""
    def __init__(self):
        super().__init__(logging.WARNING)
        self.reason = None

    def emit(self, record):
        message = record.getMessage()
        if 'Max requests per second exceeded' in message or 'Forced disconnect' in message:
            self.reason = message
