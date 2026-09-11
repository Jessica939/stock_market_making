"""Small B-only directional cycle policy using the shared verified IOC sender."""

from datetime import datetime, timezone
import math
from types import SimpleNamespace as NS

from ..baseline.cycle_signal import CycleSettings, CycleSignal, book_time
from ..common.execution import Executor, ExecutionFault
from ..common.market import UnusableBook

SYMBOLS = ("PHILIPS_A", "PHILIPS_B")
B = SYMBOLS[1]


def validate(config):
    positive = (
        "hold_seconds",
        "execution_delay_seconds",
        "entry_wait_seconds",
        "edge_buffer_ticks",
        "max_book_age_seconds",
        "decision_seconds",
        "loop_seconds",
        "max_session_loss",
        "max_drawdown",
        "session_seconds",
        "closeout_seconds",
        "shutdown_grace_seconds",
        "settlement_seconds",
    )
    for key in positive:
        value = config[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{key} must be finite and positive")
    for key, low, high in (
        ("order_lots", 1, 2),
        ("depth_reserve_lots", 0, 200),
        ("max_sweep_ticks", 0, 10),
        ("max_updates_per_second", 1, 22),
    ):
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise ValueError(f"{key} must be integer {low}..{high}")
    for key in ("fee_per_lot", "stop_per_share"):
        value = config[key]
        if key == "stop_per_share" and value is None:
            continue
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
            or (key == "stop_per_share" and value == 0)
        ):
            raise ValueError(f"invalid {key}")
    if config["hold_seconds"] != 15:
        raise ValueError("hold_seconds must match the validated 15-second forecast")
    if (
        not config["hold_seconds"] + config["execution_delay_seconds"]
        < config["closeout_seconds"]
        < config["session_seconds"]
    ):
        raise ValueError("require hold + execution delay < closeout < session")
    if config["loop_seconds"] < 0.2 or config["settlement_seconds"] > 30:
        raise ValueError("loop must be >=0.2 seconds and settlement <=30 seconds")


def object_book(raw):
    """Support SDK objects and the shared offline ReplayExchange dictionaries."""
    if raw is None or not isinstance(raw, dict):
        return raw
    stamp = raw.get("timestamp")
    if isinstance(stamp, (int, float)):
        stamp = datetime.fromtimestamp(stamp, timezone.utc)
    return NS(
        timestamp=stamp,
        bids=[NS(price=p, volume=v) for p, v in raw.get("bids", []) if v > 0],
        asks=[NS(price=p, volume=v) for p, v in raw.get("asks", []) if v > 0],
    )


def bounded_book(raw, tick, wall, config):
    stamp = book_time(raw)
    if stamp is None or not 0 <= wall - stamp <= config["max_book_age_seconds"]:
        raise UnusableBook("missing or stale book")
    sides = {}
    for side in ("bids", "asks"):
        levels = getattr(raw, side, None)
        if not levels:
            raise UnusableBook("two-sided book required")
        previous = math.inf if side == "bids" else -math.inf
        clean = []
        for level in levels:
            p, v = level.price, level.volume
            if (
                isinstance(p, bool)
                or not isinstance(p, (int, float))
                or not math.isfinite(p)
                or p <= 0
                or type(v) is not int
                or v <= 0
                or abs(p / tick - round(p / tick)) > 1e-6
                or (p >= previous if side == "bids" else p <= previous)
            ):
                raise UnusableBook("invalid price levels")
            clean.append((p, v))
            previous = p
        sides[side] = clean
    bid, ask = sides["bids"][0][0], sides["asks"][0][0]
    if bid >= ask:
        raise UnusableBook("crossed book")
    for side, levels in sides.items():
        reserve = config["depth_reserve_lots"]
        bounded = []
        touch = levels[0][0]
        for price, volume in levels:
            if abs(price - touch) > config["max_sweep_ticks"] * tick + 1e-8:
                break
            removed = min(reserve, volume)
            reserve -= removed
            volume -= removed
            if volume:
                bounded.append((price, volume))
        sides[side] = bounded
    return dict(
        **sides, bid=bid, ask=ask, mid=(bid + ask) / 2, tick=tick, timestamp=stamp
    )


def vwap(book, buy, quantity):
    left, total = quantity, 0.0
    for price, volume in book["asks" if buy else "bids"]:
        take = min(left, volume)
        total += price * take
        left -= take
        if not left:
            return total / quantity
    raise UnusableBook("insufficient bounded depth")


class Feed:
    def __init__(self, exchange, config, epoch, clock):
        self.exchange, self.config, self.epoch, self.clock = (
            exchange,
            config,
            epoch,
            clock,
        )
        self.instruments = exchange.get_tradable_instruments()
        self.ticks = {i: self.instruments[i].tick_size for i in SYMBOLS}
        if any(
            isinstance(t, bool)
            or not isinstance(t, (int, float))
            or not math.isfinite(t)
            or t <= 0
            for t in self.ticks.values()
        ):
            raise ValueError("invalid instrument ticks")
        self.last_prices = {}
        self.pending = None
        self.exit_epoch = 0.0

    def observe(self):
        raw = {}
        for i in SYMBOLS:
            for trade in self.exchange.poll_new_trade_ticks(i):
                self.last_prices[i] = trade.price
            raw[i] = object_book(self.exchange.get_last_price_book(i))
        return raw

    def one(self, iid, reducing=False, reducing_side=None):
        if iid != B:
            raise UnusableBook("B cycle may send orders only for B")
        book = bounded_book(
            object_book(self.exchange.get_last_price_book(iid)),
            self.ticks[iid],
            self.epoch(),
            self.config,
        )
        if reducing:
            if book["timestamp"] < self.exit_epoch:
                raise UnusableBook("await post-deadline exit book")
            return book
        pending = self.pending
        if pending is None or self.clock() >= pending["expires"]:
            raise UnusableBook("entry signal expired")
        if book["timestamp"] < pending["ready_epoch"]:
            raise UnusableBook("await post-signal executable book")
        size = self.config["order_lots"]
        buy, sell = vwap(book, True, size), vwap(book, False, size)
        sign = pending["sign"]
        half = (buy - sell) / 2
        margin = (
            self.config["edge_buffer_ticks"] * book["tick"]
            + 2 * self.config["fee_per_lot"]
        )
        key = "asks" if sign > 0 else "bids"
        # Bound the IOC worst price, not just average price. This is stricter
        # than the research VWAP gate and survives the final execution refresh.
        book[key] = [
            (p, v)
            for p, v in book[key]
            if sign * (pending["target"] - p) > half + margin
        ]
        vwap(book, sign > 0, size)  # Require the full proposed lot before inserting.
        return book


class Engine:
    def __init__(
        self, exchange, config, journal, clock, sleep, epoch, *, terminal_quantity=None
    ):
        validate(config)
        self.exchange, self.config, self.journal = exchange, config, journal
        self.clock, self.sleep, self.epoch = clock, sleep, epoch
        self.feed = Feed(exchange, config, epoch, clock)
        ec = dict(
            config,
            position_limit=config["order_lots"],
            max_order_lots=config["order_lots"],
            max_net_lots=config["order_lots"],
            max_outstanding_volume=200,
            entry_slippage_ticks=config["max_sweep_ticks"],
            exit_slippage_ticks=config["max_sweep_ticks"],
        )
        self.executor = Executor(
            exchange,
            SYMBOLS,
            self.feed,
            ec,
            journal,
            clock,
            sleep,
            terminal_quantity=terminal_quantity,
        )
        self.model = CycleSignal(CycleSettings())
        self.start = clock()
        self.end = self.start + config["session_seconds"]
        self.cutoff = self.end - config["closeout_seconds"]
        self.executor.reduction_deadline = self.end + config["shutdown_grace_seconds"]
        self.pending = None
        self.opened = None
        self.exit_reason = None
        self.exit_ready = None
        self.exit_epoch = None
        self.stopped = False
        self.risk_stopped = False
        self.last_decision = -math.inf
        self.last_clock = self.start
        self.cash0 = None
        self.high = 0.0
        self.last_equity = None
        self.equity_at = None

    def startup(self):
        positions = self.exchange.get_positions()
        if not isinstance(positions, dict) or any(
            type(q) is not int or q != 0 for q in positions.values()
        ):
            raise ExecutionFault("B cycle requires an entirely flat account at startup")
        for iid in self.feed.instruments:
            if self.exchange.get_outstanding_orders(iid):
                raise ExecutionFault(
                    "cancel old strategy orders before starting B cycle"
                )
        self.executor.audit("b_cycle_startup")
        self.cash0 = self.cash()

    def cash(self):
        holdings = self.exchange.get_positions_and_cash()
        value = holdings[B]["cash"]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ExecutionFault("B cash unavailable")
        return value

    def request_exit(self, reason, immediate=False):
        if self.exit_reason is None or immediate:
            self.exit_reason = reason
            delay = 0 if immediate else self.config["execution_delay_seconds"]
            self.exit_ready = self.clock() + delay
            self.exit_epoch = 0.0 if immediate else self.epoch() + delay
            self.feed.exit_epoch = self.exit_epoch
            self.journal.emit("b_exit_intent", reason=reason, ready=self.exit_ready)
        self.pending = None
        self.feed.pending = None

    def step(self):
        now = self.clock()
        if not math.isfinite(now) or now < self.last_clock:
            raise ExecutionFault("invalid/reversed strategy clock")
        self.last_clock = now
        q = self.executor.audit("b_cycle_step")
        if (
            q[SYMBOLS[0]] != 0
            or abs(q[B]) > self.config["order_lots"]
            or not self.executor.account_consistent
        ):
            raise ExecutionFault("unowned or inconsistent inventory")
        if any(self.executor.orders(i) for i in SYMBOLS):
            raise ExecutionFault("unexpected resting order")
        if self.journal.failed or now >= self.cutoff:
            self.stopped = True
        raw = self.feed.observe()
        signal = self.model.observe(raw, self.feed.ticks, now, self.epoch())
        if q[B]:
            if self.opened is None:
                raise ExecutionFault("inventory has no confirmed entry state")
            if self.stopped:
                self.request_exit("stopping", True)
            elif (
                now >= self.opened + self.config["hold_seconds"]
                and self.exit_reason is None
            ):
                self.request_exit("hold_timeout")
            try:
                book = bounded_book(
                    raw[B], self.feed.ticks[B], self.epoch(), self.config
                )
                px = vwap(book, q[B] < 0, abs(q[B]))
                equity = (
                    self.cash()
                    - self.cash0
                    + q[B] * px
                    - abs(q[B]) * self.config["fee_per_lot"]
                )
                self.last_equity = equity
                self.equity_at = self.epoch()
                self.high = max(self.high, equity)
                if (
                    equity <= -self.config["max_session_loss"]
                    or self.high - equity >= self.config["max_drawdown"]
                ):
                    self.stopped = True
                    self.risk_stopped = True
                    self.request_exit("account_risk", True)
                stop = self.config["stop_per_share"]
                if (
                    stop is not None
                    and equity - self.entry_realized <= -abs(q[B]) * stop
                ):
                    self.request_exit("position_stop")
                self.journal.emit(
                    "b_holding",
                    position=q[B],
                    equity=equity,
                    age=now - self.opened,
                    exit_reason=self.exit_reason,
                    signal=signal,
                )
                if (
                    self.exit_reason
                    and self.clock() >= self.exit_ready
                    and book["timestamp"] >= self.exit_epoch
                ):
                    self.executor.send(
                        B, "ask" if q[B] > 0 else "bid", abs(q[B]), reducing=True
                    )
                    if self.executor.positions()[B] == 0:
                        self.opened = None
                        self.exit_reason = None
                        self.feed.exit_epoch = 0.0
            except UnusableBook as exc:
                self.journal.emit(
                    "b_exit_blocked",
                    reason=str(exc),
                    position=q[B],
                    exit_reason=self.exit_reason,
                )
            return
        equity = self.cash() - self.cash0
        self.last_equity = equity
        self.equity_at = self.epoch()
        self.high = max(self.high, equity)
        if (
            equity <= -self.config["max_session_loss"]
            or self.high - equity >= self.config["max_drawdown"]
        ):
            self.stopped = True
            self.risk_stopped = True
        if self.stopped or self.executor.halted:
            self.pending = None
            self.feed.pending = None
            return
        if self.pending:
            if now >= self.pending["expires"]:
                self.journal.emit("b_entry_expired")
                self.pending = None
                self.feed.pending = None
            elif now >= self.pending["ready"]:
                sign = self.pending["sign"]
                self.feed.pending = self.pending
                self.executor.entry_deadline = min(self.pending["expires"], self.cutoff)
                try:
                    entered_at = self.clock()
                    self.entry_realized = equity
                    filled = self.executor.send(
                        B, "bid" if sign > 0 else "ask", self.config["order_lots"]
                    )
                    self.pending = None
                    self.feed.pending = None
                    if filled:
                        self.opened = entered_at
                        self.journal.emit(
                            "b_entry_confirmed",
                            position=self.executor.positions()[B],
                            opened_at=self.opened,
                        )
                except UnusableBook as exc:
                    self.journal.emit("b_entry_blocked", reason=str(exc))
            return
        if (
            now < self.executor.cooldown_until
            or now - self.last_decision < self.config["decision_seconds"]
        ):
            return
        self.last_decision = now
        if not signal.get("active"):
            self.journal.emit("b_signal", signal=signal)
            return
        try:
            book = bounded_book(raw[B], self.feed.ticks[B], self.epoch(), self.config)
            size = self.config["order_lots"]
            buy, sell = vwap(book, True, size), vwap(book, False, size)
            prediction = signal["predicted_B_change"] * signal["fit_weight"]
            threshold = (
                buy
                - sell
                + self.config["edge_buffer_ticks"] * book["tick"]
                + 2 * self.config["fee_per_lot"]
            )
            self.journal.emit(
                "b_signal",
                signal=signal,
                weighted_prediction=prediction,
                threshold=threshold,
            )
            if abs(prediction) > threshold:
                delay = self.config["execution_delay_seconds"]
                self.pending = dict(
                    sign=1 if prediction > 0 else -1,
                    target=book["mid"] + prediction,
                    ready=now + delay,
                    ready_epoch=self.epoch() + delay,
                    expires=min(
                        now + delay + self.config["entry_wait_seconds"], self.cutoff
                    ),
                )
                self.journal.emit("b_entry_pending", **self.pending)
        except UnusableBook as exc:
            self.journal.emit("b_entry_blocked", reason=str(exc))

    def finish(self, live=False):
        self.stopped = True
        self.pending = None
        self.feed.pending = None
        deadline = min(
            self.clock() + self.config["shutdown_grace_seconds"],
            self.executor.reduction_deadline,
        )
        self.executor.reduction_deadline = deadline
        if live:
            while (
                self.exchange.is_connected()
                and not self.executor.unresolved
                and self.clock() < deadline
            ):
                if not any(self.executor.positions().values()):
                    break
                self.request_exit("shutdown", True)
                self.step()
                self.sleep(self.config["loop_seconds"])
        positions = (
            self.executor.audit("b_cycle_finish")
            if self.exchange.is_connected()
            else None
        )
        flat = (
            positions is not None
            and not any(positions.values())
            and not self.executor.unresolved
            and not any(self.executor.orders(i) for i in SYMBOLS)
        )
        if flat and self.cash0 is not None:
            self.last_equity = self.cash() - self.cash0
            self.equity_at = self.epoch()
        summary = dict(
            flat=flat,
            positions=positions,
            equity=self.last_equity,
            equity_at_epoch=self.equity_at,
            risk_stopped=self.risk_stopped,
            unresolved=self.executor.unresolved,
        )
        self.journal.emit("session_end", **summary)
        return summary
