# Baseline cycle risk only v2

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
Check the startup line says `baseline_cycle_risk_only_v2`. Stop the previous
process before replacing files and restarting. Exit cancels orders; positions remain.

The notebook's tagged definition cells remain the source loaded by `run.py`.
Use the Python entry point for trading.

The 180-second cycle and 15-second forecast remain enabled. The v2 experiment
sets `CYCLE_PRICE_SHIFT_ENABLED=False` in the notebook: the cycle no longer moves
B's center or fair value. Its weighted, capped, inventory-damped signal is logged
as `cycle_risk_shift` and still drives the same size reductions and adverse spread
protection. `cycle_shift` is the actual price shift and is zero in v2. This is a
relative-value risk indicator, not a standalone absolute B price forecast.
The reduction rules, signal fitting, and other protection parameters are unchanged
from v1. Reducing-side prices also lose the cycle overlay, as part of disabling
that overlay on both sides; no separate exit delay is introduced.

B protection is
configured in `B_PROTECTION` in the notebook:

- Without an active cycle signal, B only quotes toward flat. At flat it has no
  resting orders. The executor checks fresh position capacity before inserting.
- With an active signal, inventory-increasing quotes are capped at 2 lots and
  placed at least 3 ticks from the B-book/inventory center, or farther if already wider.
- If the weighted, inventory-damped cycle shift is adverse by at least 0.5 tick,
  the increasing side gets another tick of distance and its capped size is halved.
- Reducing quotes retain their prices and are capped at the observed position.
  These are client-side checks, not an exchange atomic reduce-only order type.
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
that directory, not into `stock_market_making`. It includes the current logger and
baseline runtime dependencies; no credentials or historical logs are included.
