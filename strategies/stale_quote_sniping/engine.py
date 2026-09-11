"""B-only stale-quote sniper using the shared verified IOC executor."""
from datetime import datetime, timezone
import math
from types import SimpleNamespace as NS

from .model import BasisSettings, CausalBasisModel
from ..baseline.cycle_signal import book_time
from ..common.execution import Executor, ExecutionFault
from ..common.market import UnusableBook


SYMBOLS = ("PHILIPS_A", "PHILIPS_B")
A, B = SYMBOLS


def _positive(config, names):
    for key in names:
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(key + " must be finite and positive")


def validate(config):
    _positive(config, (
        "hold_seconds", "execution_delay_seconds", "entry_wait_seconds",
        "entry_edge_ticks", "cooldown_seconds", "max_book_age_seconds",
        "max_pair_time_gap_seconds", "max_spread_ticks", "decision_seconds",
        "loop_seconds", "max_session_loss", "max_drawdown", "session_seconds",
        "closeout_seconds", "shutdown_grace_seconds", "settlement_seconds",
        "period_seconds", "history_seconds", "warmup_seconds", "sample_seconds",
        "refit_seconds", "max_gap_seconds", "size_step_ticks",
    ))
    exit_confirmation = config["exit_confirmation_seconds"]
    if (isinstance(exit_confirmation, bool)
            or not isinstance(exit_confirmation, (int, float))
            or not math.isfinite(exit_confirmation) or exit_confirmation < 0):
        raise ValueError("exit_confirmation_seconds must be finite and nonnegative")
    for key, low, high in (
        ("order_lots", 1, 50), ("max_order_lots", 1, 50),
        ("lots_per_step", 1, 50), ("depth_reserve_lots", 0, 1000),
        ("replay_depth_reserve_lots", 0, 1000),
        ("max_sweep_ticks", 0, 20), ("max_updates_per_second", 1, 22),
    ):
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{key} must be integer {low}..{high}")
    if config["order_lots"] > config["max_order_lots"]:
        raise ValueError("order_lots must not exceed max_order_lots")
    for key in ("fee_per_lot", "stop_per_share"):
        value = config[key]
        if key == "stop_per_share" and value is None:
            continue
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0
                or (key == "stop_per_share" and value == 0)):
            raise ValueError("invalid " + key)
    if not config["hold_seconds"] + exit_confirmation < config["closeout_seconds"] < config["session_seconds"]:
        raise ValueError("require hold + exit confirmation < closeout < session")
    if config["loop_seconds"] < 0.05 or config["settlement_seconds"] > 30:
        raise ValueError("loop must be >=0.05 seconds and settlement <=30 seconds")
    BasisSettings(**{key: config[key] for key in BasisSettings.__dataclass_fields__})


class OwnedBExchange:
    """Expose only this run's B delta to Executor, while trading the raw account.

    A and the inherited B baseline are reference/account state, not inventory
    owned by this strategy. Any unexplained B change still appears as a delta
    and fails the engine's ownership checks.
    """

    def __init__(self, raw, baseline_b, baseline_cash):
        self.raw = raw
        self.baseline_b = baseline_b
        self.baseline_cash = baseline_cash

    def is_connected(self):
        return self.raw.is_connected()

    def get_positions(self):
        positions = self.raw.get_positions()
        if not isinstance(positions, dict) or B not in positions:
            return positions
        return {B: positions[B] - self.baseline_b}

    def get_positions_and_cash(self):
        holdings = self.raw.get_positions_and_cash()
        if not isinstance(holdings, dict) or B not in holdings:
            return holdings
        item = holdings[B]
        if not isinstance(item, dict):
            return {B: item}
        return {B: dict(item, volume=item.get("volume", self.baseline_b) - self.baseline_b,
                        cash=item.get("cash", self.baseline_cash) - self.baseline_cash)}

    def get_outstanding_orders(self, iid):
        return self.raw.get_outstanding_orders(iid)

    def poll_new_trades(self, iid):
        return self.raw.poll_new_trades(iid)

    def delete_order(self, iid, order_id):
        return self.raw.delete_order(iid, order_id=order_id)

    def insert_order(self, iid, **kwargs):
        return self.raw.insert_order(iid, **kwargs)


def object_book(raw):
    if raw is None or not isinstance(raw, dict):
        return raw
    value = raw.get("timestamp")
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, timezone.utc)
    return NS(timestamp=value,
              bids=[NS(price=p, volume=v) for p, v in raw.get("bids", []) if v > 0],
              asks=[NS(price=p, volume=v) for p, v in raw.get("asks", []) if v > 0])


def bounded_book(raw, tick, wall, config):
    stamp = book_time(raw)
    if stamp is None or not 0 <= wall - stamp <= config["max_book_age_seconds"]:
        raise UnusableBook("missing or stale book")
    sides = {}
    for name in ("bids", "asks"):
        levels = getattr(raw, name, None)
        if not levels:
            raise UnusableBook("two-sided book required")
        prior = math.inf if name == "bids" else -math.inf
        clean = []
        for level in levels:
            price, volume = level.price, level.volume
            if (isinstance(price, bool) or not isinstance(price, (int, float))
                    or not math.isfinite(price) or price <= 0
                    or isinstance(volume, bool) or not isinstance(volume, int) or volume <= 0
                    or abs(price / tick - round(price / tick)) > 1e-6
                    or (price >= prior if name == "bids" else price <= prior)):
                raise UnusableBook("invalid price levels")
            clean.append((price, volume))
            prior = price
        sides[name] = clean
    bid, ask = sides["bids"][0][0], sides["asks"][0][0]
    if bid >= ask or ask - bid > config["max_spread_ticks"] * tick + 1e-9:
        raise UnusableBook("crossed or excessively wide book")
    for name, levels in sides.items():
        reserve = config["depth_reserve_lots"]
        touch = levels[0][0]
        bounded = []
        for price, volume in levels:
            if abs(price - touch) > config["max_sweep_ticks"] * tick + 1e-9:
                break
            removed = min(reserve, volume)
            reserve -= removed
            volume -= removed
            if volume:
                bounded.append((price, volume))
        sides[name] = bounded
    return dict(**sides, execution_bids=sides["bids"], execution_asks=sides["asks"],
                bid=bid, ask=ask, mid=(bid + ask) / 2, tick=tick, timestamp=stamp,
                signal_usable=True)


def vwap(book, buy, quantity):
    left = quantity
    total = 0.0
    for price, volume in book["asks" if buy else "bids"]:
        take = min(left, volume)
        total += price * take
        left -= take
        if left == 0:
            return total / quantity
    raise UnusableBook("insufficient bounded depth")


def scaled_entry_size(edge, tick, config):
    """Scale only the excess edge above the entry floor, up to a hard cap."""
    net_edge = edge - 2 * config["fee_per_lot"]
    excess_ticks = max(0.0, net_edge / tick - config["entry_edge_ticks"])
    steps = math.floor((excess_ticks + 1e-9) / config["size_step_ticks"])
    return min(config["max_order_lots"],
               config["order_lots"] + steps * config["lots_per_step"])


def sized_execution(book, buy, fair, config, max_quantity=None):
    """Find a size whose own VWAP still justifies its edge-based size tier."""
    sign = 1 if buy else -1
    base = config["order_lots"]
    execution = vwap(book, buy, base)
    edge = sign * (fair - execution)
    quantity = scaled_entry_size(edge, book["tick"], config)
    if max_quantity is not None:
        quantity = min(quantity, max_quantity)
    available = sum(volume for _, volume in book["asks" if buy else "bids"])
    quantity = min(quantity, available)
    while quantity > base:
        execution = vwap(book, buy, quantity)
        edge = sign * (fair - execution)
        justified = scaled_entry_size(edge, book["tick"], config)
        if justified >= quantity:
            break
        quantity = max(base, justified)
    execution = vwap(book, buy, quantity)
    return quantity, execution, sign * (fair - execution)


def cap_depth(levels, quantity):
    capped = []
    left = quantity
    for price, volume in levels:
        take = min(left, volume)
        if take:
            capped.append((price, take))
            left -= take
        if not left:
            break
    return capped


class Feed:
    def __init__(self, exchange, config, epoch, clock):
        self.exchange, self.config, self.epoch, self.clock = exchange, config, epoch, clock
        self.instruments = exchange.get_tradable_instruments()
        if any(i not in self.instruments for i in SYMBOLS):
            raise ValueError("PHILIPS_A and PHILIPS_B must both be tradable")
        self.ticks = {i: self.instruments[i].tick_size for i in SYMBOLS}
        if any(isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) or t <= 0
               for t in self.ticks.values()):
            raise ValueError("invalid instrument ticks")
        self.last_prices = {}
        self.pending = None
        self.exit_epoch = 0.0

    def observe(self):
        raw = {}
        for iid in SYMBOLS:
            for trade in self.exchange.poll_new_trade_ticks(iid):
                self.last_prices[iid] = trade.price
            raw[iid] = object_book(self.exchange.get_last_price_book(iid))
        return raw

    def pair(self, raw=None):
        raw = self.observe() if raw is None else raw
        books = {iid: bounded_book(raw[iid], self.ticks[iid], self.epoch(), self.config)
                 for iid in SYMBOLS}
        if abs(books[A]["timestamp"] - books[B]["timestamp"]) > self.config["max_pair_time_gap_seconds"]:
            raise UnusableBook("unsynchronized pair books")
        return books

    def one(self, iid, reducing=False, reducing_side=None):
        if iid != B:
            raise UnusableBook("stale quote sniper may send orders only for B")
        if reducing:
            book = bounded_book(object_book(self.exchange.get_last_price_book(B)),
                                self.ticks[B], self.epoch(), self.config)
            if book["timestamp"] <= self.exit_epoch:
                raise UnusableBook("await post-decision exit book")
            return book
        pending = self.pending
        if pending is None or self.clock() >= pending["expires"]:
            raise UnusableBook("entry signal expired")
        books = self.pair()
        sign = pending["sign"]
        predicted_basis = CausalBasisModel.predict_snapshot(pending["model"], self.clock())
        fair = books[A]["mid"] - predicted_basis
        threshold = self.config["entry_edge_ticks"] * books[B]["tick"] + 2 * self.config["fee_per_lot"]
        key = "asks" if sign > 0 else "bids"
        filtered = [(price, volume) for price, volume in books[B][key]
                    if sign * (fair - price) + 1e-9 >= threshold]
        candidate = dict(books[B], **{key: filtered})
        size, execution, edge = sized_execution(
            candidate, sign > 0, fair, self.config, max_quantity=pending["volume"])
        self.pending["recheck"] = dict(fair_B=fair, execution_vwap=execution,
                                        edge=edge, threshold=threshold, volume=size,
                                        observed_at=self.epoch())
        if edge + 1e-9 < threshold:
            raise UnusableBook("stale-quote edge disappeared")
        filtered = cap_depth(filtered, size)
        books[B][key] = filtered
        books[B]["execution_" + key] = filtered
        vwap(books[B], sign > 0, size)
        return books[B]


class Engine:
    def __init__(self, exchange, config, journal, clock, sleep, epoch, *, terminal_quantity=None):
        validate(config)
        self.exchange, self.config, self.journal = exchange, config, journal
        self.clock, self.sleep, self.epoch = clock, sleep, epoch
        self.feed = Feed(exchange, config, epoch, clock)
        positions = exchange.get_positions()
        holdings = exchange.get_positions_and_cash()
        if (not isinstance(positions, dict) or B not in positions
                or any(isinstance(q, bool) or not isinstance(q, int) for q in positions.values())
                or not isinstance(holdings, dict) or B not in holdings
                or not isinstance(holdings[B], dict)):
            raise ExecutionFault("cannot establish startup position/cash baseline")
        baseline_cash = holdings[B].get("cash")
        if (isinstance(baseline_cash, bool) or not isinstance(baseline_cash, (int, float))
                or not math.isfinite(baseline_cash)):
            raise ExecutionFault("cannot establish B cash baseline")
        self.baseline_positions = positions.copy()
        self.baseline_b = positions[B]
        self.owned_exchange = OwnedBExchange(exchange, self.baseline_b, baseline_cash)
        execution_config = dict(
            config, position_limit=config["max_order_lots"],
            max_order_lots=config["max_order_lots"],
            max_net_lots=config["max_order_lots"], max_outstanding_volume=200,
            entry_slippage_ticks=config["max_sweep_ticks"],
            exit_slippage_ticks=config["max_sweep_ticks"],
        )
        self.executor = Executor(self.owned_exchange, (B,), self.feed, execution_config, journal,
                                 clock, sleep, terminal_quantity=terminal_quantity)
        settings = BasisSettings(**{key: config[key] for key in BasisSettings.__dataclass_fields__})
        self.model = CausalBasisModel(settings)
        self.start = clock()
        self.end = self.start + config["session_seconds"]
        self.cutoff = self.end - config["closeout_seconds"]
        self.executor.entry_deadline = self.cutoff
        self.executor.reduction_deadline = self.end + config["shutdown_grace_seconds"]
        self.pending = None
        self.entry = None
        self.exit_reason = None
        self.exit_ready = None
        self.stopped = False
        self.risk_stopped = False
        self.last_decision = -math.inf
        self.last_clock = self.start
        self.cooldown_until = -math.inf
        self.cash0 = None
        self.high = 0.0
        self.last_equity = None
        self.equity_at = None

    def startup(self):
        if self.exchange.get_outstanding_orders(B):
            raise ExecutionFault("cancel existing PHILIPS_B orders before startup")
        owned = self.executor.audit("stale_quote_startup")
        if owned[B] != 0:
            raise ExecutionFault("B changed while establishing the startup baseline")
        self.cash0 = self.cash()
        self.journal.emit("stale_inventory_baseline", positions=self.baseline_positions,
                          baseline_B=self.baseline_b, owned_B_delta=0)

    def cash(self):
        holdings = self.exchange.get_positions_and_cash()
        value = holdings[B]["cash"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ExecutionFault("B cash unavailable")
        return value

    def _equity(self, position, book=None):
        realized = self.cash() - self.cash0
        if not position:
            return realized
        book = book or bounded_book(object_book(self.exchange.get_last_price_book(B)),
                                    self.feed.ticks[B], self.epoch(), self.config)
        liquidation = vwap(book, position < 0, abs(position))
        return realized + position * liquidation - abs(position) * self.config["fee_per_lot"]

    def request_exit(self, reason, immediate=False):
        if self.exit_reason is None or immediate:
            delay = 0.0 if immediate else self.config["exit_confirmation_seconds"]
            self.exit_reason = reason
            self.exit_ready = self.clock() + delay
            self.feed.exit_epoch = 0.0 if immediate else self.epoch() + delay
            self.journal.emit("stale_exit_intent", reason=reason, ready=self.exit_ready,
                              confirmation_seconds=delay, entry=self.entry)
        self.pending = None
        self.feed.pending = None

    def _observe_signal(self, raw):
        try:
            books = self.feed.pair(raw)
        except UnusableBook as exc:
            return None, dict(active=False, reason=str(exc))
        signal = self.model.observe(
            now=self.clock(), a_mid=books[A]["mid"], b_mid=books[B]["mid"],
            book_stamps=(books[A]["timestamp"], books[B]["timestamp"]),
        )
        return books, signal

    def step(self):
        now = self.clock()
        if not math.isfinite(now) or now < self.last_clock:
            raise ExecutionFault("invalid/reversed strategy clock")
        self.last_clock = now
        positions = self.executor.audit("stale_quote_step")
        if abs(positions[B]) > self.config["max_order_lots"] or not self.executor.account_consistent:
            raise ExecutionFault("unowned or inconsistent inventory")
        if self.executor.orders(B):
            raise ExecutionFault("unexpected resting order")
        if self.journal.failed or now >= self.cutoff:
            self.stopped = True
        raw = self.feed.observe()
        books, signal = self._observe_signal(raw)
        position = positions[B]

        if position:
            if self.entry is None:
                raise ExecutionFault("inventory has no confirmed entry state")
            if self.stopped:
                self.request_exit("stopping", True)
            try:
                book = bounded_book(raw[B], self.feed.ticks[B], self.epoch(), self.config)
                equity = self._equity(position, book)
                self.last_equity, self.equity_at = equity, self.epoch()
                self.high = max(self.high, equity)
                if equity <= -self.config["max_session_loss"] or self.high - equity >= self.config["max_drawdown"]:
                    self.stopped = self.risk_stopped = True
                    self.request_exit("account_risk", True)
                stop = self.config["stop_per_share"]
                if stop is not None and equity - self.entry["realized_before"] <= -abs(position) * stop:
                    self.request_exit("position_stop", True)
                if self.exit_reason is None:
                    exit_vwap = vwap(book, position < 0, abs(position))
                    reached = self.entry["sign"] * (exit_vwap - self.entry["fair_B"]) >= -1e-9
                    if reached:
                        self.request_exit("fair_reached")
                    elif now >= self.entry["opened_at"] + self.config["hold_seconds"]:
                        self.request_exit("hold_timeout")
                self.journal.emit("stale_holding", position=position, equity=equity,
                                  age=now - self.entry["opened_at"], entry=self.entry,
                                  exit_reason=self.exit_reason, signal=signal)
                if self.exit_reason and now >= self.exit_ready:
                    self.executor.send(B, "ask" if position > 0 else "bid", abs(position), reducing=True)
                    if self.executor.positions()[B] == 0:
                        self.journal.emit("stale_exit_confirmed", reason=self.exit_reason,
                                          held_seconds=now - self.entry["opened_at"])
                        self.entry = None
                        self.exit_reason = None
                        self.feed.exit_epoch = 0.0
                        self.cooldown_until = now + self.config["cooldown_seconds"]
            except UnusableBook as exc:
                self.journal.emit("stale_exit_blocked", reason=str(exc), position=position,
                                  exit_reason=self.exit_reason)
            return

        equity = self._equity(0)
        self.last_equity, self.equity_at = equity, self.epoch()
        self.high = max(self.high, equity)
        if equity <= -self.config["max_session_loss"] or self.high - equity >= self.config["max_drawdown"]:
            self.stopped = self.risk_stopped = True
        if self.stopped or self.executor.halted:
            self.pending = self.feed.pending = None
            return

        if self.pending:
            if now >= self.pending["expires"]:
                self.journal.emit("stale_entry_expired", decision=self.pending)
                self.pending = self.feed.pending = None
            elif now >= self.pending["ready"]:
                pending = self.pending
                self.feed.pending = pending
                try:
                    realized = equity
                    filled = self.executor.send(B, "bid" if pending["sign"] > 0 else "ask",
                                                pending["volume"])
                    recheck = pending.get("recheck")
                    self.pending = self.feed.pending = None
                    if filled:
                        fair = recheck["fair_B"] if recheck else pending["fair_B"]
                        self.entry = dict(sign=pending["sign"], fair_B=fair,
                                          decision_fair_B=pending["fair_B"],
                                          decision_edge=pending["edge"], volume=filled,
                                          opened_at=now, realized_before=realized)
                        self.journal.emit("stale_entry_confirmed", owned_position=self.executor.positions()[B],
                                          actual_position=self.exchange.get_positions().get(B),
                                          entry=self.entry, recheck=recheck)
                except UnusableBook as exc:
                    self.journal.emit("stale_entry_blocked", reason=str(exc), decision=pending,
                                      recheck=pending.get("recheck"))
                    # One delayed recheck per signal. A disappeared or unavailable
                    # quote is not retried as a fresh opportunity.
                    self.pending = self.feed.pending = None
            return

        if (books is None or not signal.get("active") or now < self.cooldown_until
                or now - self.last_decision < self.config["decision_seconds"]):
            self.journal.emit("stale_signal", signal=signal)
            return
        self.last_decision = now
        try:
            buy = vwap(books[B], True, self.config["order_lots"])
            sell = vwap(books[B], False, self.config["order_lots"])
        except UnusableBook as exc:
            self.journal.emit("stale_entry_blocked", reason=str(exc), signal=signal)
            return
        fair = signal["fair_B"]
        long_edge, short_edge = fair - buy, sell - fair
        threshold = self.config["entry_edge_ticks"] * books[B]["tick"] + 2 * self.config["fee_per_lot"]
        self.journal.emit("stale_signal", signal=signal, buy_vwap=buy, sell_vwap=sell,
                          long_edge=long_edge, short_edge=short_edge, threshold=threshold)
        edge = max(long_edge, short_edge)
        if edge + 1e-9 < threshold:
            return
        sign = 1 if long_edge >= short_edge else -1
        volume, execution, edge = sized_execution(books[B], sign > 0, fair, self.config)
        if edge + 1e-9 < threshold:
            return
        delay = self.config["execution_delay_seconds"]
        self.pending = dict(sign=sign, fair_B=fair, edge=edge, volume=volume,
                            execution_vwap=execution, model=signal["model"],
                            decided_at=now, ready=now + delay,
                            expires=min(now + delay + self.config["entry_wait_seconds"], self.cutoff))
        self.feed.pending = self.pending
        self.journal.emit("stale_entry_pending", **self.pending)

    def finish(self, live=False):
        self.stopped = True
        self.pending = self.feed.pending = None
        deadline = min(self.clock() + self.config["shutdown_grace_seconds"],
                       self.executor.reduction_deadline)
        self.executor.reduction_deadline = deadline
        if live:
            while self.exchange.is_connected() and not self.executor.unresolved and self.clock() < deadline:
                if not any(self.executor.positions().values()):
                    break
                self.request_exit("shutdown", True)
                self.step()
                self.sleep(self.config["loop_seconds"])
        positions = self.executor.audit("stale_quote_finish") if self.exchange.is_connected() else None
        flat = (positions is not None and not any(positions.values()) and not self.executor.unresolved
                and not self.executor.orders(B))
        if flat and self.cash0 is not None:
            self.last_equity, self.equity_at = self.cash() - self.cash0, self.epoch()
        actual = self.exchange.get_positions() if self.exchange.is_connected() else None
        summary = dict(flat=flat, flat_scope="sniper-owned B delta restored to zero",
                       baseline_positions=self.baseline_positions, baseline_B=self.baseline_b,
                       owned_positions=positions, actual_positions=actual, equity=self.last_equity,
                       equity_at_epoch=self.equity_at, risk_stopped=self.risk_stopped,
                       unresolved=self.executor.unresolved)
        self.journal.emit("session_end", **summary)
        return summary
