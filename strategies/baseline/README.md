# Baseline B exit guard v3

For the new B-only directional cycle experiment, use
[`../b_cycle/run.py`](../b_cycle/README.md). It trades at most 2 B lots when the
weighted forecast covers executable spread and holds for about 15 seconds.
A is reference-only in that mode. This baseline entry point still runs the
original two-sided market maker; do not run both on the same account.

New logs are under `data/runs/baseline/<run_id>/`; market recordings are under
`data/market/<recording_id>/`. Each run's `manifest.json` links both and captures
the settings. The analysis cell searches both new and legacy logs. See
`../../DATA_LAYOUT.md` for directory overrides and historical data handling.

Run from `your_optiver_workspace/stock_market_making`:

```bash
python strategies/baseline/run.py --live
```

This connects and sends simulated-exchange orders. Only run one team client.
Check the startup line says `baseline_b_exit_guard_v3`. Stop the previous
process before replacing files and restarting. Exit cancels orders; positions remain.

[`strategy.py`](strategy.py) is now the source of truth. `run.py` remains the
compatible trading entry point used by the baseline and hybrid strategies. The
old notebook is retained as a historical/reference copy only.

The 180-second cycle and 15-second forecast remain enabled. The v2 experiment
sets `CYCLE_PRICE_SHIFT_ENABLED=False` in `strategy.py`: the cycle no longer moves
B's center or fair value. Its weighted, capped, inventory-damped signal is logged
as `cycle_risk_shift` and still drives the same size reductions and adverse spread
protection. `cycle_shift` is the actual price shift and is zero in v2. This is a
relative-value risk indicator, not a standalone absolute B price forecast.
The signal fitting remains unchanged. The absolute cycle price shift stays off;
the risk signal now protects both inventory-increasing and reducing quotes.

B protection is
configured in `B_PROTECTION` in `strategy.py`:

- Without an active cycle signal, B only quotes toward flat. At flat it has no
  resting orders. The executor checks fresh position capacity before inserting.
- With an active signal, inventory-increasing quotes are capped at 2 lots and
  placed at least 3 ticks from the B-book/inventory center, or farther if already
  wider. B has a separate 10-lot absolute inventory cap; at the cap it only reduces.
- If the weighted, inventory-damped cycle shift is adverse by at least 0.5 tick,
  the increasing side gets another tick of distance and its capped size is halved.
  At 1 tick it is suppressed rather than opening against a strong signal.
- For active-cycle B inventory of at most 10 lots, reducing quotes are also kept
  at least 3 ticks from the center, plus one tick when adverse. A reducing quote
  against a strong signal is suppressed only for the first 2 seconds of a newly
  observed position episode, then resumes at the protected passive price.
- Reducing sizes remain capped at the observed position. These are client-side
  checks, not an exchange atomic reduce-only order type. Inherited inventory,
  inactive-signal fallback, and positions above 10 lots are not delayed.
- A keeps its existing cycle-free inventory-aware quoting.

A cancellation rejection saying `Could not find order id to delete` is benign
only after a fresh outstanding-order read confirms the order is absent. Positions
are reread before replacement; other failures still block replacement. This avoids
resetting the cycle fit for confirmed cancellation/fill races.

Logs include the strategy version, protection settings and per-quote protection
decisions. Markout timeout is explicitly 90 seconds, covering the 60-second horizon
plus 30-second grace. Session-end or unavailable books can still leave missing marks.

These are conservative experimental settings, not a demonstrated profitable fit.
The old log cannot determine new fills or new PnL after prices and sizes change.

The deployment zip is rooted at `your_optiver_workspace` contents. Extract into
that directory, not into `stock_market_making`. Baseline uses the shared passive
quoting interface in `stock_market_making/strategies/common/quoting.py`, so deploy
the complete `stock_market_making/` package. No separate sibling `common/`
directory is required. No credentials or historical logs are included.
