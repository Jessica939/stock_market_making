from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd


DATA_ROOT = Path(r"D:\Jessica\optiver\data\market")
OUT_DIR = Path(__file__).resolve().parent
MAX_LAG = 20


def load_sessions() -> list[dict]:
    sessions: list[dict] = []
    columns = [
        "sample_id",
        "observed_at_utc",
        "instrument_id",
        "book_timestamp",
        "status",
        "best_bid",
        "best_ask",
        "mid",
        "spread",
    ]
    for path in sorted(DATA_ROOT.glob("philips_*/prices.csv")):
        raw = pd.read_csv(path, usecols=columns)
        raw = raw[(raw["status"] == "ok") & raw["instrument_id"].isin(["PHILIPS_A", "PHILIPS_B"])]
        raw["observed_at_utc"] = pd.to_datetime(raw["observed_at_utc"], utc=True)
        raw["book_timestamp"] = pd.to_datetime(raw["book_timestamp"], utc=True)
        values = ["best_bid", "best_ask", "mid", "spread", "observed_at_utc", "book_timestamp"]
        paired = raw.pivot_table(index="sample_id", columns="instrument_id", values=values, aggfunc="last")
        paired.columns = [f"{field}_{symbol[-1]}" for field, symbol in paired.columns]
        needed = [f"{field}_{symbol}" for field in ["best_bid", "best_ask", "mid"] for symbol in "AB"]
        paired = paired.dropna(subset=needed).sort_index().reset_index()
        if len(paired) < 3:
            continue
        paired["time"] = paired[["observed_at_utc_A", "observed_at_utc_B"]].max(axis=1)
        paired["dA"] = paired["mid_A"].diff()
        paired["dB"] = paired["mid_B"].diff()
        paired["session"] = path.parent.name
        dt = paired["time"].diff().dt.total_seconds().dropna()
        sessions.append(
            {
                "name": path.parent.name,
                "path": str(path),
                "frame": paired,
                "samples": int(len(paired)),
                "duration_seconds": float((paired["time"].iloc[-1] - paired["time"].iloc[0]).total_seconds()),
                "median_step_seconds": float(dt.median()),
                "a_change_rate": float(paired["dA"].fillna(0).ne(0).mean()),
                "b_change_rate": float(paired["dB"].fillna(0).ne(0).mean()),
            }
        )
    return sessions


def corr_at_lag(a: np.ndarray, b: np.ndarray, lag: int) -> tuple[float, int]:
    if lag > 0:
        x, y = a[:-lag], b[lag:]
    elif lag < 0:
        x, y = a[-lag:], b[:lag]
    else:
        x, y = a, b
    good = np.isfinite(x) & np.isfinite(y)
    x, y = x[good], y[good]
    if len(x) < 3 or np.std(x) == 0 or np.std(y) == 0:
        return float("nan"), int(len(x))
    return float(np.corrcoef(x, y)[0, 1]), int(len(x))


def pooled_lag_curve(frames: list[pd.DataFrame], xcol: str, ycol: str) -> list[dict]:
    rows = []
    for lag in range(0, MAX_LAG + 1):
        xs, ys = [], []
        for frame in frames:
            a = frame[xcol].to_numpy(dtype=float)[1:]
            b = frame[ycol].to_numpy(dtype=float)[1:]
            if lag:
                xs.append(a[:-lag])
                ys.append(b[lag:])
            else:
                xs.append(a)
                ys.append(b)
        x, y = np.concatenate(xs), np.concatenate(ys)
        good = np.isfinite(x) & np.isfinite(y)
        x, y = x[good], y[good]
        corr = float(np.corrcoef(x, y)[0, 1]) if len(x) >= 3 and np.std(x) and np.std(y) else float("nan")
        rows.append({"lag_samples": lag, "corr": corr, "n": int(len(x))})
    return rows


def session_lag_results(sessions: list[dict]) -> list[dict]:
    rows = []
    for session in sessions:
        frame = session["frame"]
        a = frame["dA"].to_numpy(dtype=float)[1:]
        b = frame["dB"].to_numpy(dtype=float)[1:]
        curve = []
        for lag in range(0, MAX_LAG + 1):
            corr, n = corr_at_lag(a, b, lag)
            curve.append({"lag_samples": lag, "corr": corr, "n": n})
        positive = [row for row in curve if row["lag_samples"] > 0 and np.isfinite(row["corr"])]
        best = max(positive, key=lambda row: row["corr"], default={"lag_samples": None, "corr": None})
        rows.append(
            {
                "session": session["name"],
                "samples": session["samples"],
                "step_seconds": session["median_step_seconds"],
                "corr_lag_0": curve[0]["corr"],
                "best_positive_lag": best["lag_samples"],
                "best_positive_corr": best["corr"],
                "curve": curve,
            }
        )
    return rows


def execution_study(frames: list[pd.DataFrame]) -> list[dict]:
    rows = []
    for horizon in range(1, MAX_LAG + 1):
        mid_pnls, cross_pnls, future_moves, signs = [], [], [], []
        for frame in frames:
            d_a = frame["dA"].to_numpy(dtype=float)
            d_b = frame["dB"].to_numpy(dtype=float)
            signal = np.sign(d_a)
            # The cleanest stale-B setup: A changed during this sample, B did not.
            idx = np.flatnonzero((signal != 0) & (d_b == 0))
            idx = idx[idx + horizon < len(frame)]
            if not len(idx):
                continue
            s = signal[idx]
            now_mid = frame["mid_B"].to_numpy(dtype=float)[idx]
            future_mid = frame["mid_B"].to_numpy(dtype=float)[idx + horizon]
            entry = np.where(s > 0, frame["best_ask_B"].to_numpy(dtype=float)[idx], frame["best_bid_B"].to_numpy(dtype=float)[idx])
            exit_ = np.where(s > 0, frame["best_bid_B"].to_numpy(dtype=float)[idx + horizon], frame["best_ask_B"].to_numpy(dtype=float)[idx + horizon])
            move = future_mid - now_mid
            mid_pnls.extend((s * move).tolist())
            cross_pnls.extend((s * (exit_ - entry)).tolist())
            future_moves.extend(move.tolist())
            signs.extend(s.tolist())
        mid = np.asarray(mid_pnls)
        cross = np.asarray(cross_pnls)
        moves = np.asarray(future_moves)
        sig = np.asarray(signs)
        nonzero = moves != 0
        rows.append(
            {
                "horizon_samples": horizon,
                "signals": int(len(mid)),
                "mean_mid_markout": float(np.mean(mid)) if len(mid) else None,
                "median_mid_markout": float(np.median(mid)) if len(mid) else None,
                "mid_positive_rate": float(np.mean(mid > 0)) if len(mid) else None,
                "direction_accuracy_when_B_moves": float(np.mean(sig[nonzero] == np.sign(moves[nonzero]))) if np.any(nonzero) else None,
                "b_move_rate": float(np.mean(nonzero)) if len(mid) else None,
                "mean_cross_spread_pnl": float(np.mean(cross)) if len(cross) else None,
                "cross_spread_win_rate": float(np.mean(cross > 0)) if len(cross) else None,
            }
        )
    return rows


def train_test_check(sessions: list[dict]) -> dict:
    split = max(1, len(sessions) // 2)
    train = sessions[:split]
    test = sessions[split:]
    train_curve = pooled_lag_curve([x["frame"] for x in train], "dA", "dB")
    positive = [row for row in train_curve if row["lag_samples"] > 0]
    selected = max(positive, key=lambda row: row["corr"])["lag_samples"]
    test_curve = pooled_lag_curve([x["frame"] for x in test], "dA", "dB") if test else []
    return {
        "train_sessions": [x["name"] for x in train],
        "test_sessions": [x["name"] for x in test],
        "selected_lag_samples": selected,
        "train_corr": train_curve[selected]["corr"],
        "test_corr": test_curve[selected]["corr"] if test_curve else None,
    }


def main() -> None:
    sessions = load_sessions()
    frames = [x["frame"] for x in sessions]
    a_leads_b = pooled_lag_curve(frames, "dA", "dB")
    b_leads_a = pooled_lag_curve(frames, "dB", "dA")
    per_session = session_lag_results(sessions)
    execution = execution_study(frames)
    summary_sessions = [{key: value for key, value in x.items() if key != "frame"} for x in sessions]
    result = {
        "method": {
            "price": "top-of-book mid",
            "lag_definition": "Corr(dA[t], dB[t+k]); k>0 means A leads B",
            "signal": "dA != 0 and simultaneous dB == 0",
            "execution": "marketable entry at B ask/bid and marketable exit at future B bid/ask; fees excluded",
        },
        "sessions": summary_sessions,
        "pooled_a_leads_b": a_leads_b,
        "pooled_b_leads_a": b_leads_a,
        "per_session_a_leads_b": per_session,
        "train_test": train_test_check(sessions),
        "execution_study": execution,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "metrics.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    best_a = max(a_leads_b[1:], key=lambda row: row["corr"])
    best_b = max(b_leads_a[1:], key=lambda row: row["corr"])
    best_mid = max(execution, key=lambda row: row["mean_mid_markout"])
    best_cross = max(execution, key=lambda row: row["mean_cross_spread_pnl"])
    total_samples = sum(x["samples"] for x in sessions)
    median_step = float(np.median([x["median_step_seconds"] for x in sessions]))
    print(json.dumps({
        "session_count": len(sessions),
        "total_samples": total_samples,
        "median_step_seconds": median_step,
        "corr_lag_0": a_leads_b[0],
        "best_a_leads_b": best_a,
        "best_b_leads_a": best_b,
        "train_test": result["train_test"],
        "best_mid_markout": best_mid,
        "best_cross_spread": best_cross,
    }, indent=2))


if __name__ == "__main__":
    main()
