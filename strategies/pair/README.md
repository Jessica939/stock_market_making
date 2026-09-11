# Pair execution and holding revision

The B-only 15-second directional cycle strategy from the full-market analysis
has its own [B cycle entry point](../b_cycle/README.md). This Pair entry point
continues to trade two legs and is not the B-only experiment.

## Latest: path lag observation and one bounded extension

Both pair configs now set cycle_path_grace_seconds=5,
cycle_extension_seconds=15, max_hold_seconds=60. The entry forecast remains 45s.
Adverse deviation from the frozen expected path first returns
observe_cycle_path_lag (existing inventory only). Persistent lag reaches
cycle_path_lag_timeout after 5 seconds. Recovery clears the lag timer; favorable
overshoot is no longer treated as a path break. Actual adverse-move and
liquidation-loss stops still take priority; recovery never clears latched exit.

At the planned 45s exit, an extension may be granted ONCE, only if the entry-frozen
model predicts further favorable movement exceeding cycle_exit_edge_ticks and
there is no excessive path lag or triggered stop. Its deadline is original exit
plus 15s, bounded by first entry plus 60s. No rolling extension or refitting the
deadline. Missing risk data still uses the short observation fallback, and session
closeout and account loss limits can force an earlier exit.

`analysis/pair_review_20260910/compare_horizons.py` reproduces the 45/60/90 second
hypothetical comparison in horizon_comparison.json: seven signals have both 45s
and 60s coverage, six improve at 60s and one worsens. 90s is not uniformly better
and has less coverage, so no unconditional 90s hold is enabled. These overlapping
signal-price counterfactuals are not additive profits or actual executable fills.

Latest validation: 36 tests pass; all 48 runs on 12 complete recordings finish
flat. Four variants in replay_controls.json: previous controls -205.9; three-second
controls +15.9; independent_45s (new one-sided lag semantics, no grace/extension)
+25.3; latest controls +66.5. The increment is concentrated in two recordings;
this development sample does not establish out-of-sample or live profitability.
Numbers in the revision history below describe earlier implementations and are
superseded by this paragraph. No live exchange was connected.

## Independent holding monitoring (latest revision)

Both pair configs enable `pair_independent_holding`. Entry checks remain strict.
If entry spread/cooldown or bounded liquidation-depth checks fail while a confirmed
pair is held, a separate risk frame can keep monitoring it. It requires fresh,
synchronized, two-sided prices with normal cores; boundary-touch/reference faults
still invalidate monitoring. A wide spread alone no longer forces liquidation.
Displayed full-depth valuation is diagnostic for loss checks, not a promise that
an exit order can execute at that valuation. Actual orders retain price bands and
slippage limits. If monitoring itself fails, the bounded observation rules below
remain the fallback.

Held cycle decisions use the weights frozen at entry, including remaining forecast,
adverse basis movement, path deviation, liquidation loss and the 45-second deadline.
A subsequent refit becoming unavailable no longer alone cancels that held forecast.
Stop/exit intent remains latched; monitoring never opens or replenishes inventory.
Account loss/drawdown, unmatched inventory and session deadlines remain enforced.

Historical horizon inspection (`analysis/pair_review_20260910/horizon_check.json`)
found fresh synchronized 45–46 second exit snapshots for 7 of 11 entry signals.
All seven hypothetical round trips were positive using signal bid/ask entry and
raw displayed exit depth. Four lacked target-time coverage. This is not actual
fill PnL, not an executable-order simulation (boundary/price-band filtering was not
applied), and not additive independent trading profit: some entry windows overlap.
Intraperiod losses remain material. Complete records from incomplete gzip streams
were retained for that diagnostic only, with files flagged in the artifact.

Latest comparison: 12 complete recordings, same replay assumptions, previous-control
ablation -205.9; three-second/five-lot controls +15.9; independent holding/five-lot
controls +26.3. One session worsens from +28.2 to +21.6. Thus the change is not a
uniform improvement or proof of live profitability. All 36 simulated runs finish
flat. 34 offline tests pass. The comparison script names all three variants.

The 180-second cycle and 45-second forecast are unchanged. Both config.json and
config.fast_recovery.json now cap each submitted order at 5 lots (previously 20).
Before opening a pair, the executor also requires displayed exit depth on both
legs, using at most 35% of that bounded depth. This is a current snapshot check,
not a guarantee of future liquidity or maximum loss.

Confirmed, matched inventory may observe a temporary market/valuation failure:

- pair_market_grace_seconds = 3: maximum market observation window.
- pair_valuation_grace_seconds = 1: maximum continuous missing valuation window.
- pair_recovery_seconds = 1: continuous healthy period required to resume.

No new entry is sent during observation. Brief healthy frames do not reset the
overall deadline. A known loss/drawdown breach, unmatched/untracked inventory,
session closeout or holding deadline bypasses observation. Exit intent remains
latched until flat even when executable books temporarily disappear. These
limits bound client decisions, not synchronous RPC duration or actual fill time.
Cycle-model-specific exit rules remain in force.

IOC reconciliation now logs observation, confirmed full/partial/zero settlement,
or unknown status. The local documented live API has no terminal quantity query:
live partial/zero outcomes STILL halt all new inserts when unproven. No automatic
retry or guessed hedge is enabled. Pending order ID, quantities and residual
positions remain in the journal/session summary for reconciliation. This remains
a live execution limitation, not a solved zero-fill recovery mechanism.

An explicitly injected terminal-quantity adapter supports proven partial/zero
outcomes. Only the offline simulator supplies this adapter; both replay control
variants use the same proof. Delayed private fills must still match positions.
Timeouts, empty outstanding orders and stable positions are never terminal proof.

Validation:

```text
python -B -m unittest discover -s tests -p "test_*.py" -q
python -B analysis/pair_review_20260910/replay_controls.py
```

34 tests pass, including full/zero/partial fills, late reports, lost acknowledgments,
contradictory terminal evidence, asymmetric legs, exit capacity, observation loss
checks, stale exit books and recovery deadlines; existing hybrid/state/storage
tests also pass. No live exchange was connected.

The replay comparison covers 12 complete market recordings. Two gzip recordings
(20260909T060802 and 20260910T091154) fail end-of-stream validation and are excluded;
original files remain untouched. Results are in
../../analysis/pair_review_20260910/replay_controls.json.

At 50% displayed liquidity, previous-control ablation totals -205.9 and independent
holding controls total +26.3. These are development
sample results, not held-out profitability evidence: size, holding controls and
exit-depth admission change together. Previous-controls is an ablation of the
current engine, not a reconstruction of historical live code. Replay assumes
synchronous matching with authoritative terminal proof unavailable in live mode,
does not model actual latency/queue competition or unseen updates, and cannot
establish that live IOC faults have been resolved. Parameters are provisional.

Existing live startup instructions remain `python strategies/pair/run.py --mode live`;
this revision does not launch trading or reconnect an account automatically.
