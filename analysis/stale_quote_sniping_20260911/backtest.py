"""Causal research replay for B stale-quote sniping.

The model estimates the current A-B basis from observations strictly before the
decision.  It then treats ``A_mid - predicted_basis`` as B fair value and crosses
only when B's executable quote is far enough through that fair value.

This is deliberately a research replay, not a live order entry point.
"""
from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime
import glob
import gzip
import json
import math
import os
from pathlib import Path
from statistics import median
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from strategies.baseline.cycle_signal import _solve


SYMBOLS = ("PHILIPS_A", "PHILIPS_B")


def stamp(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def load_session(path: str) -> tuple[list[dict], bool]:
    """Load paired recorder samples, retaining a valid prefix of truncated gzip."""
    grouped: dict[object, dict] = {}
    truncated = False
    try:
        with gzip.open(path, "rt", encoding="utf-8-sig") as source:
            for line in source:
                row = json.loads(line)
                iid = row.get("instrument_id")
                if iid not in SYMBOLS:
                    continue
                key = row.get("sample_id")
                grouped.setdefault(key, {})[iid] = row
    except (EOFError, OSError):
        truncated = True
    frames = []
    for rows in grouped.values():
        if set(rows) != set(SYMBOLS):
            continue
        observed = max(stamp(rows[i]["observed_at_utc"]) for i in SYMBOLS)
        frames.append({"t": observed, "rows": rows})
    frames.sort(key=lambda x: x["t"])
    return frames, truncated


def usable(frame: dict, max_age: float = 1.0, max_pair_gap: float = 0.75) -> bool:
    book_times = []
    for iid in SYMBOLS:
        row = frame["rows"][iid]
        if row.get("status") != "ok" or not row.get("bids") or not row.get("asks"):
            return False
        try:
            bt = stamp(row["book_timestamp"])
            bid, ask = row["bids"][0][0], row["asks"][0][0]
        except (KeyError, TypeError, ValueError, IndexError):
            return False
        if not 0 <= frame["t"] - bt <= max_age or not 0 < bid < ask:
            return False
        book_times.append(bt)
    return abs(book_times[0] - book_times[1]) <= max_pair_gap


def mid(row: dict) -> float:
    return (row["bids"][0][0] + row["asks"][0][0]) / 2


def executable_price(row: dict, buy: bool, quantity: int, reserve: int = 200,
                     fill_fraction: float = 0.5, max_sweep_ticks: int = 10,
                     tick: float = 0.1) -> float | None:
    """Conservative VWAP: reserve displayed depth and cap sweep distance."""
    levels = row["asks" if buy else "bids"]
    touch = levels[0][0]
    reserve_left = reserve
    left = quantity
    notional = 0.0
    for price, displayed in levels:
        if abs(price - touch) > max_sweep_ticks * tick + 1e-9:
            break
        available = int(displayed * fill_fraction)
        removed = min(reserve_left, available)
        reserve_left -= removed
        available -= removed
        take = min(left, available)
        notional += take * price
        left -= take
        if left == 0:
            return notional / quantity
    return None


class CausalBasisModel:
    def __init__(self, period: float = 180.0, history: float = 720.0,
                 warmup: float = 180.0, refit: float = 5.0):
        self.period, self.history, self.warmup, self.refit = period, history, warmup, refit
        self.rows = deque()
        self.origin = None
        self.weights = None
        self.rmse = None
        self.r2 = None
        self.last_fit = -math.inf

    def x(self, t: float) -> list[float]:
        angle = 2 * math.pi * ((t - self.origin) % self.period) / self.period
        return [1.0, math.sin(angle), math.cos(angle)]

    def predict(self, t: float) -> float | None:
        if self.weights is None:
            return None
        return sum(a * b for a, b in zip(self.x(t), self.weights))

    def _fit(self, now: float) -> None:
        if len(self.rows) < 20 or self.rows[-1][0] - self.rows[0][0] < self.warmup:
            return
        matrix = [[0.0] * 3 for _ in range(3)]
        rhs = [0.0] * 3
        for t, y in self.rows:
            x = self.x(t)
            for i in range(3):
                rhs[i] += x[i] * y
                for j in range(3):
                    matrix[i][j] += x[i] * x[j]
        weights = _solve(matrix, rhs)
        if weights is None:
            return
        errors = []
        values = []
        for t, y in self.rows:
            fitted = sum(a * b for a, b in zip(self.x(t), weights))
            errors.append((y - fitted) ** 2)
            values.append(y)
        mse = sum(errors) / len(errors)
        mean = sum(values) / len(values)
        variance = sum((y - mean) ** 2 for y in values) / len(values)
        self.weights = weights
        self.rmse = math.sqrt(mse)
        self.r2 = 1 - mse / variance if variance > 1e-12 else -1.0
        self.last_fit = now

    def before_observation(self, now: float) -> None:
        while self.rows and now - self.rows[0][0] > self.history:
            self.rows.popleft()
        if now - self.last_fit >= self.refit:
            self._fit(now)

    def add(self, now: float, basis: float) -> None:
        if self.origin is None:
            self.origin = now
        if not self.rows or now - self.rows[-1][0] >= 1.0:
            self.rows.append((now, basis))


@dataclass(frozen=True)
class Params:
    edge_ticks: int
    hold_seconds: float
    latency_seconds: float = 0.25
    order_lots: int = 2
    cooldown_seconds: float = 1.0
    fee_per_lot: float = 0.0
    tick: float = 0.1


def run_session(path: str, params: Params) -> dict:
    frames, truncated = load_session(path)
    model = CausalBasisModel()
    position = 0
    entry = None
    pending = None
    cooldown_until = -math.inf
    pnl = 0.0
    trades = []
    opportunities = 0
    usable_frames = 0
    for frame in frames:
        now = frame["t"]
        if not usable(frame):
            continue
        usable_frames += 1
        a, b = (frame["rows"][s] for s in SYMBOLS)
        basis = mid(a) - mid(b)

        # Strict causality: predict/refit before adding this frame's A-B basis.
        model.before_observation(now)
        predicted_basis = model.predict(now)
        fv = mid(a) - predicted_basis if predicted_basis is not None else None

        if pending and now >= pending["ready"]:
            action = pending["action"]
            buy = action in ("enter_long", "exit_short")
            px = executable_price(b, buy, params.order_lots)
            if px is not None:
                if action.startswith("enter"):
                    sign = 1 if action == "enter_long" else -1
                    # Recheck with the frozen model and fresh A/book after latency.
                    live_fv = mid(a) - pending["predict"](now)
                    edge = sign * (live_fv - px)
                    if edge >= params.edge_ticks * params.tick:
                        position = sign * params.order_lots
                        entry = {"t": now, "px": px, "fv": live_fv, "sign": sign,
                                 "decision_t": pending["decision_t"], "edge": edge}
                else:
                    sign = entry["sign"]
                    trade_pnl = sign * (px - entry["px"]) * params.order_lots
                    trade_pnl -= 2 * params.fee_per_lot * params.order_lots
                    pnl += trade_pnl
                    trades.append({**entry, "exit_t": now, "exit_px": px,
                                   "hold": now - entry["t"], "pnl": trade_pnl,
                                   "exit_reason": pending["reason"]})
                    position = 0
                    entry = None
                    cooldown_until = now + params.cooldown_seconds
            pending = None

        if position and pending is None:
            sign = entry["sign"]
            exit_px = executable_price(b, buy=sign < 0, quantity=params.order_lots)
            corrected = (exit_px is not None and
                         sign * (exit_px - entry["fv"]) >= 0)
            timed_out = now - entry["t"] >= params.hold_seconds
            if corrected or timed_out:
                pending = {"action": "exit_long" if sign > 0 else "exit_short",
                           "ready": now + params.latency_seconds,
                           "reason": "fair_reached" if corrected else "hold_timeout"}

        if position == 0 and pending is None and fv is not None and now >= cooldown_until:
            buy_px = executable_price(b, True, params.order_lots)
            sell_px = executable_price(b, False, params.order_lots)
            long_edge = fv - buy_px if buy_px is not None else -math.inf
            short_edge = sell_px - fv if sell_px is not None else -math.inf
            edge = max(long_edge, short_edge)
            if edge >= params.edge_ticks * params.tick:
                opportunities += 1
                sign = 1 if long_edge >= short_edge else -1
                frozen_weights = tuple(model.weights)
                origin, period = model.origin, model.period
                def frozen_predict(t, w=frozen_weights, o=origin, p=period):
                    angle = 2 * math.pi * ((t - o) % p) / p
                    return w[0] + w[1] * math.sin(angle) + w[2] * math.cos(angle)
                pending = {"action": "enter_long" if sign > 0 else "enter_short",
                           "ready": now + params.latency_seconds,
                           "decision_t": now, "predict": frozen_predict}

        model.add(now, basis)

    # No invented terminal liquidity. Mark residual inventory at final executable
    # quote and report it separately; completed-trade PnL remains realized only.
    residual_mark = None
    if position and frames:
        last_b = frames[-1]["rows"]["PHILIPS_B"]
        px = executable_price(last_b, buy=position < 0, quantity=abs(position))
        if px is not None:
            residual_mark = entry["sign"] * (px - entry["px"]) * abs(position)
    wins = sum(t["pnl"] > 0 for t in trades)
    losses = sum(t["pnl"] < 0 for t in trades)
    return {
        "session": os.path.basename(os.path.dirname(path)),
        "date": os.path.basename(os.path.dirname(path)).split("_")[1][:8],
        "frames": len(frames), "usable_frames": usable_frames,
        "truncated": truncated, "opportunities": opportunities,
        "trades": len(trades), "wins": wins, "losses": losses,
        "pnl": pnl, "mean_trade": pnl / len(trades) if trades else None,
        "median_trade": median([t["pnl"] for t in trades]) if trades else None,
        "residual_position": position, "residual_mark": residual_mark,
        "trade_details": trades,
    }


def aggregate(results: list[dict]) -> dict:
    trades = [t for r in results for t in r["trade_details"]]
    pnls = [t["pnl"] for t in trades]
    return {
        "sessions": len(results),
        "usable_sessions": sum(r["usable_frames"] > 0 for r in results),
        "truncated_sessions": sum(r["truncated"] for r in results),
        "opportunities": sum(r["opportunities"] for r in results),
        "trades": len(trades),
        "wins": sum(x > 0 for x in pnls),
        "losses": sum(x < 0 for x in pnls),
        "flat": sum(x == 0 for x in pnls),
        "win_rate": sum(x > 0 for x in pnls) / len(pnls) if pnls else None,
        "pnl": sum(pnls),
        "mean_trade": sum(pnls) / len(pnls) if pnls else None,
        "median_trade": median(pnls) if pnls else None,
        "worst_trade": min(pnls) if pnls else None,
        "best_trade": max(pnls) if pnls else None,
        "residual_positions": sum(r["residual_position"] != 0 for r in results),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default=r"D:\Jessica\optiver\data\market")
    parser.add_argument("--output", default=str(Path(__file__).with_name("results.json")))
    args = parser.parse_args()
    paths = sorted(glob.glob(os.path.join(args.data, "*", "orderbooks_*.jsonl.gz")))
    train = [p for p in paths if "_20260909" in p]
    test = [p for p in paths if "_20260910" in p]
    grid = []
    for edge_ticks in (2, 3, 4, 5, 6, 8):
        for hold in (3.0, 5.0, 10.0, 15.0):
            params = Params(edge_ticks=edge_ticks, hold_seconds=hold)
            rows = [run_session(p, params) for p in train]
            grid.append({"params": asdict(params), "train": aggregate(rows)})
    # Select on total train PnL, then mean trade, then fewer trades (less turnover).
    chosen = max(grid, key=lambda x: (x["train"]["pnl"],
                                      x["train"]["mean_trade"] or -math.inf,
                                      -x["train"]["trades"]))
    params = Params(**chosen["params"])
    train_rows = [run_session(p, params) for p in train]
    test_rows = [run_session(p, params) for p in test]
    payload = {
        "method": {
            "fair_value": "A_mid minus 180-second harmonic forecast of A_mid-B_mid",
            "causality": "model fit excludes the decision frame; frozen through entry latency",
            "latency_seconds": params.latency_seconds,
            "fill_fraction": 0.5,
            "depth_reserve_lots": 200,
            "fees": params.fee_per_lot,
            "selection": "grid selected on 2026-09-09; 2026-09-10 held out",
        },
        "chosen": chosen,
        "train": aggregate(train_rows),
        "test": aggregate(test_rows),
        "train_sessions": [{k: v for k, v in r.items() if k != "trade_details"} for r in train_rows],
        "test_sessions": [{k: v for k, v in r.items() if k != "trade_details"} for r in test_rows],
        "grid": grid,
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps({"chosen": chosen, "test": payload["test"]}, indent=2))


if __name__ == "__main__":
    main()
