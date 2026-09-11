"""Shared CLI. Demo is default; only explicit --mode live imports Optibook."""

import argparse
from collections.abc import Mapping
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import time
from stock_market_making.recording.storage import MARKET_DIR, RUNS_DIR, RunStorage

from .execution import Executor, ExecutionFault
from .market import (
    clean_book,
    UnusableBook,
    timestamp_seconds,
    BoundaryTracker,
    ioc_plan,
    validate_market_config,
)
from .simulation import ReplayExchange, SimClock, demo_frames, read_frames
from ..pair.holding import HoldingGuard

DEFAULTS = dict(
    symbols=["PHILIPS_A", "PHILIPS_B"],
    session_seconds=1800,
    closeout_seconds=120,
    loop_seconds=0.25,
    max_updates_per_second=20,
    position_limit=100,
    max_outstanding_volume=200,
    max_order_lots=20,
    max_net_lots=30,
    max_book_age_seconds=2.0,
    max_pair_time_gap_seconds=0.75,
    max_spread_ticks=20,
    boundary_volume=20000,
    boundary_volume_tolerance=0.25,
    boundary_min_distance_ticks=5,
    max_depth_ticks=4,
    max_depth_levels=5,
    feature_volume_cap=100,
    entry_slippage_ticks=1,
    exit_slippage_ticks=3,
    settlement_seconds=0.25,
    fee_per_lot=0.0,
    max_session_loss=50.0,
    max_drawdown=60.0,
    shutdown_grace_seconds=5.0,
    initial_cleanup_seconds=10.0,
    max_mid_jump_ticks=20,
    jump_cooldown_seconds=5.0,
)


def validate_config(config):
    if type(config.get("pair_independent_holding", False)) is not bool:
        raise ValueError("pair_independent_holding must be boolean")
    for key, maximum in (
        ("pair_market_grace_seconds", 5),
        ("pair_valuation_grace_seconds", 2),
        ("pair_recovery_seconds", 2),
    ):
        value = config.get(key, 0)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 <= value <= maximum
        ):
            raise ValueError(f"{key} must be finite and in 0..{maximum}")
    if (
        config.get("pair_market_grace_seconds", 0) > 0
        and config.get("pair_recovery_seconds", 0)
        >= config["pair_market_grace_seconds"]
    ):
        raise ValueError("pair_recovery_seconds must be shorter than market grace")
    fraction = config.get("pair_exit_liquidity_fraction", 1.0)
    if (
        isinstance(fraction, bool)
        or not isinstance(fraction, (int, float))
        or not math.isfinite(fraction)
        or not 0 < fraction <= 1
    ):
        raise ValueError("pair_exit_liquidity_fraction must be in (0, 1]")
    if (
        not isinstance(config["symbols"], (list, tuple))
        or len(config["symbols"]) != 2
        or len(set(config["symbols"])) != 2
        or not all(isinstance(i, str) and i for i in config["symbols"])
    ):
        raise ValueError("exactly two distinct instrument IDs are required")
    for key in (
        "session_seconds",
        "closeout_seconds",
        "loop_seconds",
        "max_session_loss",
        "max_drawdown",
        "max_book_age_seconds",
        "settlement_seconds",
        "shutdown_grace_seconds",
        "initial_cleanup_seconds",
        "max_pair_time_gap_seconds",
        "max_mid_jump_ticks",
        "jump_cooldown_seconds",
    ):
        if (
            isinstance(config[key], bool)
            or not isinstance(config[key], (int, float))
            or not math.isfinite(config[key])
            or config[key] <= 0
        ):
            raise ValueError(f"{key} must be finite and positive")
    if config["loop_seconds"] < 0.2:
        raise ValueError("loop_seconds must be >= 0.2")
    for key, maximum in (
        ("position_limit", 100),
        ("max_outstanding_volume", 200),
        ("max_updates_per_second", 22),
        ("max_order_lots", 100),
        ("max_net_lots", 100),
    ):
        if (
            isinstance(config[key], bool)
            or not isinstance(config[key], int)
            or not 1 <= config[key] <= maximum
        ):
            raise ValueError(f"{key} must be integer 1..{maximum}")
    for key in ("fee_per_lot", "entry_slippage_ticks", "exit_slippage_ticks"):
        if (
            isinstance(config[key], bool)
            or not isinstance(config[key], (int, float))
            or config[key] < 0
            or not math.isfinite(config[key])
        ):
            raise ValueError(f"{key} must be finite and nonnegative")
    required_slippage = (
        config["entry_slippage_ticks"] + config["exit_slippage_ticks"]
    ) / 2
    for key in ("slippage_ticks", "ml_slippage_ticks_per_side"):
        config.setdefault(key, required_slippage)
        value = config[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < required_slippage
        ):
            raise ValueError(
                f"{key} must cover entry+exit slippage: at least {required_slippage:g} ticks per side"
            )
    validate_market_config(config)


class Journal:
    def __init__(
        self,
        directory=None,
        *,
        strategy="cleanup",
        mode="live",
        config=None,
        **metadata,
    ):
        self.storage = RunStorage(
            strategy, directory=directory, mode=mode, config=config, **metadata
        )
        self.path = self.storage.directory / "events.jsonl"
        self.storage.link_events(self.path)
        self.file = self.path.open("x", encoding="utf-8", buffering=1)
        self.failed = False
        self.closed = False

    def emit(self, kind, **fields):
        if self.failed or self.closed:
            return
        try:
            self.file.write(
                json.dumps(
                    dict(
                        type=kind,
                        recorded_at=datetime.now(timezone.utc).isoformat(),
                        **fields,
                    ),
                    ensure_ascii=False,
                    allow_nan=False,
                    default=str,
                )
                + "\n"
            )
        except (OSError, ValueError, TypeError) as exc:
            self.failed = True
            print(
                f"Journal failed; entries disabled, cleanup continues: {exc}",
                file=sys.stderr,
            )

    def close(self):
        if not self.closed:
            try:
                self.file.close()
            except OSError as exc:
                self.failed = True
                print(f"Journal close failed: {exc}", file=sys.stderr)
            finally:
                self.closed = True


class Feed:
    """One public-tick consumer; one() never consumes a trade stream.

    Exchange reads are sequential. Every execution request refreshes the raw
    book and tradability. A trusted reference expires rather than following a
    wall-only quote into a new, artificial price regime.
    """

    def __init__(
        self,
        exchange,
        symbols,
        config,
        clock=time.monotonic,
        epoch=time.time,
        journal=None,
    ):
        self.exchange, self.symbols, self.config = exchange, tuple(symbols), config
        self.clock, self.epoch, self.journal = clock, epoch, journal
        self.instruments = exchange.get_tradable_instruments()
        self.last_prices, self.last_trade_timestamps = {}, {}
        self.trusted_mids, self.previous_mids = {}, {}
        self.trackers = {iid: BoundaryTracker() for iid in self.symbols}
        self.boundary_signatures = {}
        self.cooldown_until = 0.0

    @staticmethod
    def _json_safe(value):
        # Preserve bad-field evidence without sending NaN into strict JSONL.
        if isinstance(value, float) and not math.isfinite(value):
            return str(value)
        if isinstance(value, Mapping):
            return {str(k): Feed._json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [Feed._json_safe(v) for v in value]
        return value

    def _emit(self, kind, **fields):
        if self.journal is not None:
            self.journal.emit(kind, **self._json_safe(fields))

    def _expire_references(self):
        now = self.epoch()
        if (
            isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not math.isfinite(now)
        ):
            raise UnusableBook("invalid local UTC clock")
        age_limit = min(2.0, self.config["max_book_age_seconds"])
        for iid, stamp in tuple(self.last_trade_timestamps.items()):
            if not -1 <= now - stamp <= age_limit:
                self.last_prices.pop(iid, None)
        for iid, (_, stamp) in tuple(self.trusted_mids.items()):
            if not -1 <= now - stamp <= age_limit:
                self.trusted_mids.pop(iid, None)

    def _reference(self, iid):
        self._expire_references()
        if iid in self.last_prices:
            return self.last_prices[iid]
        trusted = self.trusted_mids.get(iid)
        return trusted[0] if trusted is not None else None

    def one(self, iid, reducing=False, reducing_side=None):
        if iid not in self.symbols:
            raise UnusableBook("instrument outside managed selection")
        try:
            self.instruments = self.exchange.get_tradable_instruments()
            if iid not in self.instruments:
                raise UnusableBook(f"{iid}: not tradable")
            raw = self.exchange.get_last_price_book(iid)
        except UnusableBook:
            raise
        except Exception as exc:
            raise UnusableBook(f"{iid}: book/discovery read failed: {exc}") from exc
        sides = {}
        for side in ("bids", "asks"):
            levels = (
                raw.get(side, []) if isinstance(raw, dict) else getattr(raw, side, [])
            )
            items = []
            for level in levels or ():
                items.append(
                    list(level)
                    if isinstance(level, (tuple, list))
                    else [getattr(level, "price", None), getattr(level, "volume", None)]
                )
            sides[side] = items[:5]
            sides[side + "_outermost"] = items[-1] if items else None
        self._emit(
            "raw_book",
            instrument=iid,
            reducing=bool(reducing or reducing_side),
            timestamp=(
                raw.get("timestamp")
                if isinstance(raw, dict)
                else getattr(raw, "timestamp", None)
            ),
            **sides,
        )
        book = clean_book(
            raw,
            self.instruments[iid].tick_size,
            self.epoch(),
            self.config,
            for_execution=bool(reducing or reducing_side),
            reference_price=self._reference(iid),
            boundary_tracker=self.trackers[iid],
        )
        signature = json.dumps(
            self._json_safe(book.get("boundary_levels", [])),
            sort_keys=True,
            default=str,
        )
        if signature != self.boundary_signatures.get(iid):
            self.boundary_signatures[iid] = signature
            self._emit(
                "boundary_change",
                instrument=iid,
                levels=book.get("boundary_levels", []),
                signal_usable=book.get("signal_usable", True),
            )
        if book.get("signal_usable", True):
            mid, stamp = book.get("mid"), book.get("timestamp")
            if (
                isinstance(mid, (int, float))
                and math.isfinite(mid)
                and mid > 0
                and isinstance(stamp, (int, float))
                and math.isfinite(stamp)
            ):
                self.trusted_mids[iid] = (mid, stamp)
        return book

    def frame(self):
        trades, errors = {}, []
        self._expire_references()
        # Drain every selected instrument once even when one symbol is bad.
        for iid in self.symbols:
            trades[iid] = []
            try:
                ticks = self.exchange.poll_new_trade_ticks(iid)
                if not isinstance(ticks, (list, tuple)):
                    raise UnusableBook("public tick snapshot must be a list")
            except Exception as exc:
                errors.append(f"{iid}: public tick read failed: {exc}")
                continue
            for trade in ticks:
                price = getattr(trade, "price", None)
                row = dict(
                    price=price,
                    volume=getattr(trade, "volume", None),
                    side=getattr(trade, "aggressor_side", None),
                    timestamp=getattr(trade, "timestamp", None),
                    trade_id=getattr(trade, "trade_id", None),
                )
                trades[iid].append(self._json_safe(row))
                self._emit("public_trade", instrument=iid, **row)
                try:
                    stamp = timestamp_seconds(row["timestamp"])
                except (UnusableBook, ValueError, TypeError):
                    errors.append(f"{iid}: invalid public trade timestamp")
                    continue
                volume = row["volume"]
                if (
                    isinstance(volume, bool)
                    or not isinstance(volume, int)
                    or volume <= 0
                    or row["side"] not in ("bid", "ask")
                ):
                    errors.append(f"{iid}: invalid public trade side or volume")
                    continue
                if (
                    isinstance(price, bool)
                    or not isinstance(price, (int, float))
                    or not math.isfinite(price)
                    or price <= 0
                ):
                    errors.append(f"{iid}: invalid public trade price")
                    continue
                if (
                    not isinstance(price, bool)
                    and isinstance(price, (int, float))
                    and math.isfinite(price)
                    and price > 0
                    and -1
                    <= self.epoch() - stamp
                    <= min(2.0, self.config["max_book_age_seconds"])
                    and stamp >= self.last_trade_timestamps.get(iid, -math.inf)
                ):
                    self.last_prices[iid], self.last_trade_timestamps[iid] = (
                        price,
                        stamp,
                    )
        books = {}
        for iid in self.symbols:
            try:
                book = self.one(iid)
                if not book.get("signal_usable", True):
                    raise UnusableBook(f"{iid}: no trustworthy signal core")
                books[iid] = book
            except UnusableBook as exc:
                errors.append(str(exc))
        if errors:
            raise UnusableBook("; ".join(errors))
        stamps = [b["timestamp"] for b in books.values()]
        if max(stamps) - min(stamps) > self.config["max_pair_time_gap_seconds"]:
            raise UnusableBook("A/B snapshots too asynchronous")
        for iid, book in books.items():
            old = self.previous_mids.get(iid)
            if (
                old is not None
                and abs(book["mid"] - old) / book["tick"]
                > self.config["max_mid_jump_ticks"]
            ):
                self.cooldown_until = (
                    self.clock() + self.config["jump_cooldown_seconds"]
                )
            self.previous_mids[iid] = book["mid"]
        if self.clock() < self.cooldown_until:
            raise UnusableBook("jump cooldown")
        return dict(now=self.clock(), books=books, trades=trades)

    def holding_frame(self):
        """Fresh paired prices for risk monitoring, independent of entry spread/cooldown.

        Only the entry spread-width rejection is waived. Suspected boundary
        touches, missing cores, stale data and reference dislocations remain invalid.
        This frame is never used to open or replenish a position.
        """
        books = {}
        for iid in self.symbols:
            book = self.one(iid, reducing=True)
            reasons = set(book.get("quality_reasons", [])) - {"spread_too_wide"}
            if reasons or book["bid"] is None or book["ask"] is None:
                raise UnusableBook(
                    "holding prices unavailable: " + ", ".join(sorted(reasons))
                )
            books[iid] = dict(
                book, mid=(book["bid"] + book["ask"]) / 2, signal_usable=True
            )
        if (
            max(b["timestamp"] for b in books.values())
            - min(b["timestamp"] for b in books.values())
            > self.config["max_pair_time_gap_seconds"]
        ):
            raise UnusableBook("holding snapshots too asynchronous")
        return dict(now=self.clock(), books=books, trades={})

    def liquidation_equity(self, *, require_bounded=True):
        holdings = self.exchange.get_positions_and_cash()
        if not isinstance(holdings, Mapping):
            raise ExecutionFault("cash/position snapshot unavailable")
        equity = 0.0
        for iid in self.symbols:
            if iid not in holdings:
                raise ExecutionFault("cash/position snapshot unavailable")
            h = holdings[iid]
            q, cash = h["volume"], h["cash"]
            if (
                isinstance(q, bool)
                or not isinstance(q, int)
                or isinstance(cash, bool)
                or not isinstance(cash, (int, float))
                or not math.isfinite(cash)
            ):
                raise ExecutionFault("cash or position invalid")
            equity += cash
            if q:
                side = "ask" if q > 0 else "bid"
                book = self.one(iid, reducing_side=side)
                key = "bids" if q > 0 else "asks"
                levels = book.get("execution_" + key, book[key])
                _, available = ioc_plan(
                    book, side, abs(q), self.config["exit_slippage_ticks"]
                )
                if require_bounded and available < abs(q):
                    raise UnusableBook(
                        "insufficient bounded depth to value liquidation"
                    )
                remaining, value = abs(q), 0.0
                for price, volume in levels:
                    used = min(remaining, volume)
                    value += price * used
                    remaining -= used
                    if not remaining:
                        break
                if remaining:
                    raise UnusableBook("insufficient real depth to value liquidation")
                equity += value if q > 0 else -value
                equity -= abs(q) * self.config["fee_per_lot"]
        return equity


class Session:
    def __init__(
        self,
        policy,
        kind,
        exchange,
        feed,
        config,
        journal,
        clock,
        sleep,
        started_at=None,
        terminal_quantity=None,
    ):
        self.policy, self.kind, self.feed, self.config, self.journal = (
            policy,
            kind,
            feed,
            config,
            journal,
        )
        self.clock, self.sleep = clock, sleep
        self.start = clock() if started_at is None else started_at
        self.executor = Executor(
            exchange,
            config["symbols"],
            feed,
            config,
            journal,
            clock,
            sleep,
            terminal_quantity=terminal_quantity,
        )
        self.executor.entry_deadline = (
            self.start + config["session_seconds"] - config["closeout_seconds"]
        )
        self.executor.reduction_deadline = self.start + config["session_seconds"]
        self.baseline = self.peak = None
        self.risk_halt = False
        self.initialized = False
        self.pending = None
        self.cycles, self.last_equity = 0, None
        self.startup_cancelled = False
        self.stop_requested = False
        self.fatal_error = None
        self.holding_guard = HoldingGuard(config) if kind == "pair" else None
        self.pair_exit_reason = None
        if self.feed.journal is None:
            self.feed.journal = journal

    def _hard_fault(self):
        return bool(getattr(self.executor, "hard_fault", False))

    def _cancel_only(self, phase):
        """Unknown outcomes permit cancellation and observation, never inserts."""
        self.pending = None
        self.stop_requested = True
        try:
            self.executor.cancel_all()
        except Exception as exc:
            self.journal.emit("cancel_cleanup_error", phase=phase, error=str(exc))
        try:
            return self.executor.audit(phase)
        except Exception as exc:
            self.journal.emit("audit_cleanup_error", phase=phase, error=str(exc))
            return None

    def _reduce(self, phase):
        if self._hard_fault():
            return self._cancel_only(phase)
        if self.clock() >= self.executor.reduction_deadline:
            return self._cancel_only("deadline_" + phase)
        return self.executor.flatten()

    def _pair_observe(self, positions, reason=None, equity=None):
        """Return True when observation/latched reduction consumed this step."""
        if self.holding_guard is None or not any(positions.values()):
            if self.holding_guard:
                self.holding_guard.reset()
            return False
        if not self.executor.account_consistent:
            raise ExecutionFault("pair observation requires reconciled inventory")
        stopping = (
            self.risk_halt
            or self.executor.halted
            or self.journal.failed
            or self.clock() >= self.executor.entry_deadline
        )
        was_paused = self.holding_guard.blocked_since is not None
        action = self.holding_guard.evaluate(
            self.clock(),
            positions,
            getattr(self.policy, "active", None),
            reason=reason,
            equity=equity,
            baseline=self.baseline,
            peak=self.peak,
            stopping=stopping,
        )
        if equity is not None:
            self.last_equity = equity
            if self.peak is not None:
                self.peak = max(self.peak, equity)
        if action == "resume":
            if was_paused:
                self.journal.emit("pair_market_resumed", positions=positions)
            return False
        if action == "wait":
            self.pending = None
            self.journal.emit(
                "pair_market_observation",
                reason=reason,
                positions=positions,
                elapsed=self.clock() - self.holding_guard.blocked_since,
                liquidation_equity=equity,
                entry_allowed=False,
            )
            return True
        self.pending = None
        self.pair_exit_reason = action
        if action == "loss_limit":
            self.risk_halt = True
        self.policy._exit(action)
        self.journal.emit(
            "pair_exit_requested",
            reason=action,
            market_reason=reason,
            positions=positions,
            liquidation_equity=equity,
        )
        self._reduce(action)
        return True

    def _monitor_pair(self, positions):
        """Risk-only decision on existing inventory; cannot introduce a new entry."""
        frame = self.feed.holding_frame()
        equity = self.feed.liquidation_equity(require_bounded=False)
        self.last_equity = equity
        if self.baseline is not None:
            self.peak = max(self.peak, equity)
            if (
                equity - self.baseline <= -self.config["max_session_loss"]
                or self.peak - equity >= self.config["max_drawdown"]
            ):
                self.risk_halt = True
        self.holding_guard.reset()
        if self._pair_observe(positions, equity=equity):
            return
        decision = self.policy.monitor_cycle_position(frame, positions)
        self.pending = None
        self.journal.emit(
            "pair_holding_decision",
            reason=decision["reason"],
            diagnostics=decision["diagnostics"],
            positions=positions,
            liquidation_equity=equity,
            entry_allowed=False,
        )
        if not any(decision["targets"].values()):
            self.pair_exit_reason = decision["reason"]
            self.journal.emit(
                "pair_exit_requested", reason=decision["reason"], positions=positions
            )
            self._reduce(decision["reason"])

    def step(self, delayed=False):
        elapsed = self.clock() - self.start
        self.cycles += 1
        try:
            if self._hard_fault():
                self._cancel_only("hard_fault")
                return
            if not self.startup_cancelled:
                self.executor.cancel_all()
                self.startup_cancelled = True
            positions = self.executor.audit("cycle")
            if self.pair_exit_reason:
                if any(positions.values()):
                    self._reduce(self.pair_exit_reason)
                    return
                self.pair_exit_reason = None
                self.holding_guard.reset()
            if not self.initialized:
                if any(positions.values()):
                    self.pending = None
                    # Startup must also drain public ticks: price-band checks
                    # require their reference even before the first new entry.
                    try:
                        self.feed.frame()
                    except UnusableBook as exc:
                        self.journal.emit("startup_market_blocked", reason=str(exc))
                    if elapsed >= min(
                        self.config["initial_cleanup_seconds"],
                        self.config["session_seconds"],
                    ):
                        self.risk_halt = True
                        self.fatal_error = (
                            "inherited positions remain at startup cleanup deadline"
                        )
                        self._cancel_only("startup_deadline")
                    else:
                        self._reduce("startup")
                    return
            self.initialized = True
            frame = self.feed.frame()
            equity = self.feed.liquidation_equity()
            self.last_equity = equity
            if self.baseline is None:
                self.baseline = self.peak = equity
            self.peak = max(self.peak, equity)
            if (
                equity - self.baseline <= -self.config["max_session_loss"]
                or self.peak - equity >= self.config["max_drawdown"]
            ):
                self.risk_halt = True
            elapsed = self.clock() - self.start  # Feed/account reads may be slow.
            stopping = (
                self.risk_halt
                or self.executor.halted
                or self.journal.failed
                or elapsed
                >= self.config["session_seconds"] - self.config["closeout_seconds"]
            )
            if stopping:
                self.pending = None
                positions = self._reduce("closeout")
                if positions is not None and not any(positions.values()):
                    self.stop_requested = True
                return
            if self._pair_observe(positions, equity=equity):
                return
            if delayed and self.pending is not None:
                decision, reference = self.pending
                self.executor.apply(decision["targets"], self.kind, reference)
                if self._hard_fault():
                    self._cancel_only("hard_fault_after_delayed_execution")
                    return
                positions = self.executor.positions()
                elapsed = self.clock() - self.start
                if self.clock() >= self.executor.entry_deadline:
                    self.pending = None
                    self._reduce("closeout_after_delayed_execution")
                    return
            decision = self.policy.decide(frame, positions, elapsed)
            if self.clock() >= self.executor.entry_deadline:
                self.pending = None
                self._reduce("closeout_after_policy")
                return
            self.journal.emit(
                "decision",
                elapsed=elapsed,
                reason=decision["reason"],
                targets=decision["targets"],
                diagnostics=decision.get("diagnostics", {}),
                liquidation_equity=equity,
                pnl_change=equity - self.baseline,
                books=frame["books"],
            )
            if delayed:
                self.pending = (decision, frame["books"])
            else:
                self.executor.apply(decision["targets"], self.kind, frame["books"])
            if self._hard_fault():
                self._cancel_only("hard_fault_after_execution")
        except UnusableBook as exc:
            self.pending = None
            self.journal.emit("market_blocked", reason=str(exc))
            if self.kind == "pair" and self.initialized:
                try:
                    actual = self.executor.positions()
                    if (
                        any(actual.values())
                        and self.config.get("pair_independent_holding", False)
                        and self.config.get("relation_mode") == "cycle"
                    ):
                        try:
                            self._monitor_pair(actual)
                            return
                        except UnusableBook as monitoring_error:
                            self.journal.emit(
                                "pair_monitor_unavailable", reason=str(monitoring_error)
                            )
                    value = None
                    if any(actual.values()):
                        try:
                            value = self.feed.liquidation_equity()
                        except UnusableBook:
                            pass
                    if self._pair_observe(actual, reason=str(exc), equity=value):
                        return
                except ExecutionFault as error:
                    self.risk_halt = True
                    self.fatal_error = str(error)
                    self.journal.emit("execution_halt", error=str(error))
                    self._cancel_only("observation_account_fault")
                    return
            # Reset policy learning/entry state across every data gap, including
            # brief wall-only intervals. Never execute this invalid-frame output.
            try:
                actual = self.executor.positions()
                self.policy.decide(
                    {"now": self.clock(), "books": {}, "trades": {}}, actual, elapsed
                )
            except Exception as invalidation_error:
                self.risk_halt = True
                self.fatal_error = str(invalidation_error)
                self.journal.emit(
                    "policy_invalidation_error", error=str(invalidation_error)
                )
            # Invalid entry features must not stop one-sided risk reduction.
            try:
                self._reduce("market_blocked")
            except UnusableBook as reduction_error:
                self.journal.emit("liquidation_blocked", reason=str(reduction_error))
            except ExecutionFault as cleanup_error:
                self.risk_halt = True
                self.journal.emit("execution_halt", error=str(cleanup_error))
                self._cancel_only("market_cleanup_fault")
        except ExecutionFault as exc:
            self.pending = None
            self.risk_halt = True
            self.executor.halted = True
            self.fatal_error = str(exc)
            self.journal.emit(
                "execution_halt", error=str(exc), hard_fault=self._hard_fault()
            )
            if self._hard_fault():
                self._cancel_only("execution_fault")
            else:
                try:
                    positions = self._reduce("execution_halt")
                    if positions is not None and not any(positions.values()):
                        self.stop_requested = True
                except Exception as cleanup_error:
                    self.journal.emit("cleanup_error", error=str(cleanup_error))
                    self._cancel_only("cleanup_fault")
        except Exception as exc:
            self.pending = None
            self.risk_halt = True
            self.executor.halted = True
            self.executor.hard_fault = True
            self.fatal_error = str(exc)
            self.journal.emit("execution_halt", error=str(exc))
            self._cancel_only("unclassified_fault")

    def finish(self, grace=5.0):
        self.risk_halt = True
        self.pending = None
        deadline = min(self.clock() + max(0.0, grace), self.executor.reduction_deadline)
        self.executor.reduction_deadline = deadline
        positions = None
        try:
            while not self._hard_fault() and self.clock() < deadline:
                try:
                    positions = self._reduce("shutdown")
                except Exception as exc:
                    self.journal.emit("final_liquidation_error", error=str(exc))
                if (
                    positions is not None
                    and not any(positions.values())
                    and not self._hard_fault()
                    and getattr(self.executor, "_pending", None) is None
                ):
                    break
                if self.clock() >= deadline:
                    break
                self.sleep(0.2)
            # Cancel -> confirm -> private fill drain -> final actual account.
            self.executor.cancel_all()
            positions = self.executor.audit("final_after_cancellation")
        except Exception as exc:
            self.journal.emit("final_reconciliation_error", error=str(exc))
            positions = None
        reconciled_positions = positions
        final_snapshot_agrees = False
        try:
            all_positions = self.executor.exchange.get_positions()
            if (
                not isinstance(all_positions, Mapping)
                or any(i not in all_positions for i in self.config["symbols"])
                or any(
                    isinstance(q, bool) or not isinstance(q, int)
                    for q in all_positions.values()
                )
            ):
                raise ExecutionFault("final account positions malformed")
            positions = {i: all_positions[i] for i in self.config["symbols"]}
            final_snapshot_agrees = (
                reconciled_positions is not None and positions == reconciled_positions
            )
            if reconciled_positions is not None and not final_snapshot_agrees:
                self.journal.emit(
                    "final_position_mismatch",
                    reconciled=reconciled_positions,
                    observed=positions,
                )
            unmanaged = {
                i: q
                for i, q in all_positions.items()
                if i not in self.config["symbols"] and q
            }
        except Exception as exc:
            unmanaged = None
            self.journal.emit("final_account_error", error=str(exc))
        try:
            self.last_equity = self.feed.liquidation_equity()
        except Exception:
            self.last_equity = None
        confirmed_flat = (
            final_snapshot_agrees
            and positions is not None
            and not any(positions.values())
            and not self._hard_fault()
            and getattr(self.executor, "_pending", None) is None
            and getattr(self.executor, "account_consistent", True)
            and unmanaged == {}
        )
        summary = dict(
            final_positions=positions,
            unmanaged_positions=unmanaged,
            flat=confirmed_flat,
            reconciled_positions=reconciled_positions,
            final_snapshot_agrees=final_snapshot_agrees,
            flat_scope="observed account positions, no unresolved IOC or hard fault",
            unresolved_ioc=getattr(self.executor, "_pending", None),
            hard_fault=self._hard_fault(),
            fault_reason=getattr(self.executor, "fault_reason", None),
            liquidation_equity=self.last_equity,
            pnl_change=(
                self.last_equity - self.baseline
                if self.last_equity is not None and self.baseline is not None
                else None
            ),
            pnl_scope="selected instruments, baseline after inherited-position cleanup",
            cycles=self.cycles,
            execution_halted=self.executor.halted,
            fatal_error=self.fatal_error,
            journal_failed=self.journal.failed,
        )
        self.journal.emit("session_end", **summary)
        return summary


def main(policy_class, kind, folder, argv=None):
    parser = argparse.ArgumentParser(
        description="Active Optibook strategy; demo never connects."
    )
    parser.add_argument("--mode", choices=["demo", "replay", "live"], default="demo")
    parser.add_argument(
        "--config",
        type=Path,
        help="Optional JSON object overriding documented defaults",
    )
    parser.add_argument(
        "--duration",
        type=float,
        help="Total session seconds including warmup and closeout",
    )
    parser.add_argument(
        "--replay",
        help="Full-depth JSONL(.gz) path/glob, one chronological recording session",
    )
    parser.add_argument("--replay-tick-size", type=float, default=0.1)
    parser.add_argument(
        "--fill-fraction",
        type=float,
        default=0.5,
        help="Offline accessible share of displayed volume",
    )
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--price-data-dir", type=Path, default=MARKET_DIR)
    parser.add_argument(
        "--log-dir",
        "--output-dir",
        dest="output_dir",
        type=Path,
        default=RUNS_DIR / kind,
        help="Parent directory for new run folders",
    )
    args = parser.parse_args(argv)
    config = dict(DEFAULTS)
    config_path = (
        args.config if args.config is not None else Path(folder) / "config.json"
    )
    if args.config is not None or config_path.exists():
        try:
            supplied = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            parser.error(f"cannot read config: {exc}")
        if not isinstance(supplied, dict):
            parser.error("config must contain a JSON object")
        config.update(supplied)
    if args.duration is not None:
        config["session_seconds"] = args.duration
    try:
        validate_config(config)
    except (ValueError, TypeError) as exc:
        parser.error(str(exc))
    if not 0 < args.fill_fraction <= 1:
        parser.error("fill-fraction must be in (0,1]")
    if args.mode == "replay" and not args.replay:
        parser.error("--replay is required in replay mode")
    if kind == "pair":
        # Keep a short engineering smoke run constructible. The common 120s
        # closeout guard still disables every entry in such a short session.
        config.setdefault(
            "entry_cutoff_seconds", min(120.0, config["session_seconds"] / 2)
        )
        config.setdefault(
            "liquidation_buffer_seconds", min(60.0, config["entry_cutoff_seconds"] / 2)
        )
    try:
        policy = policy_class(config)
    except (ValueError, TypeError) as exc:
        parser.error(f"invalid policy configuration: {exc}")
    journal = Journal(
        args.output_dir,
        strategy=kind,
        mode=args.mode,
        config=config,
        replay_source=args.replay,
        replay_tick_size=args.replay_tick_size,
        fill_fraction=args.fill_fraction,
        seed=args.seed,
    )
    journal.emit(
        "settings",
        mode=args.mode,
        strategy_kind=kind,
        config=config,
        offline_latency="signals execute on the next supplied snapshot",
        offline_fill_fraction=args.fill_fraction,
    )
    print(f"Mode: {args.mode}; journal: {journal.path}", flush=True)
    exchange = session = None
    summary = {"flat": False, "final_positions": None}
    fatal_error = None
    try:
        if args.mode == "live":
            from optibook.synchronous_client import Exchange
            from stock_market_making.recording.shared_market_recording import (
                RecordingExchange,
            )

            exchange = RecordingExchange(
                Exchange(max_nr_trade_history=10000), args.price_data_dir
            )
            exchange.connect()
            connected_at = time.monotonic()
            feed = Feed(exchange, config["symbols"], config, journal=journal)
            if any(i not in feed.instruments for i in config["symbols"]):
                raise ValueError(
                    "configured symbols not currently tradable; check names/opening time"
                )
            exchange.start_recording()
            journal.storage.link_market(exchange.recorder.directory)
            session = Session(
                policy,
                kind,
                exchange,
                feed,
                config,
                journal,
                time.monotonic,
                time.sleep,
                started_at=connected_at,
            )
            # Own the sole account connection and cancel inherited orders once.
            session.executor.cancel_all()
            session.startup_cancelled = True
            all_positions = exchange.get_positions()
            if any(q for i, q in all_positions.items() if i not in config["symbols"]):
                raise ValueError(
                    "unmanaged positions outside the selected pair; no new trades started"
                )
            while (
                exchange.is_connected()
                and not session.stop_requested
                and time.monotonic() - session.start < config["session_seconds"]
            ):
                try:
                    session.step()
                finally:
                    try:
                        exchange.sample_market_data()
                    finally:
                        time.sleep(config["loop_seconds"])
        else:
            print(
                "Offline functional simulation: results are not a live profitability estimate.",
                flush=True,
            )
            frames = (
                demo_frames(
                    config["symbols"], config["session_seconds"], seed=args.seed
                )
                if args.mode == "demo"
                else read_frames(args.replay, config["symbols"], args.replay_tick_size)
            )
            clock = SimClock()
            exchange = ReplayExchange(
                config["symbols"], clock, config["fee_per_lot"], args.fill_fraction
            )
            first_epoch = None
            for raw in frames:
                if first_epoch is None:
                    first_epoch = raw["epoch"]
                data_elapsed = raw["epoch"] - first_epoch
                if data_elapsed > config["session_seconds"]:
                    break
                clock.now = max(clock.now, data_elapsed)
                exchange.advance(raw)
                if session is None:
                    feed = Feed(
                        exchange,
                        config["symbols"],
                        config,
                        clock.monotonic,
                        lambda: first_epoch + clock.now,
                        journal=journal,
                    )
                    session = Session(
                        policy,
                        kind,
                        exchange,
                        feed,
                        config,
                        journal,
                        clock.monotonic,
                        clock.sleep,
                        terminal_quantity=exchange.ioc_terminal_quantity,
                    )
                try:
                    session.step(delayed=True)
                finally:
                    clock.sleep(config["loop_seconds"])
                if session.stop_requested:
                    break
    except KeyboardInterrupt:
        print("Stopping entries; attempting bounded liquidation.", flush=True)
    except Exception as exc:
        fatal_error = str(exc)
        journal.emit("fatal_error", error=str(exc))
        print(f"Strategy stopped: {exc}", file=sys.stderr)
    finally:
        try:
            if session is not None and exchange.is_connected():
                # Entries stop before the session limit; reductions get at most
                # the explicit shutdown grace. An in-flight RPC is not preempted.
                remaining = max(
                    0.0, session.executor.reduction_deadline - session.clock()
                )
                summary = session.finish(
                    min(config["shutdown_grace_seconds"], remaining)
                )
            elif session is not None:
                journal.emit(
                    "session_end",
                    flat=False,
                    final_positions=None,
                    reason="connection lost; cannot confirm final account",
                )
        finally:
            try:
                if exchange is not None:
                    exchange.disconnect()
            finally:
                journal.close()
    fatal_error = fatal_error or summary.get("fatal_error")
    if fatal_error is not None:
        summary["fatal_error"] = fatal_error
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False), flush=True)
    return 0 if summary["flat"] and not journal.failed and fatal_error is None else 2
