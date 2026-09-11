# Hybrid ownership recovery

Run from `stock_market_making`:

```bash
python strategies/hybrid/run.py --check
python strategies/hybrid/run.py --live
```

The live runner uses `state/default/hybrid.json` for new installations. Existing
`state/hybrid.json` files or locks keep the legacy location; if both locations
exist, select `--state-file` explicitly. `--account NAME` chooses a local state
namespace, not exchange credentials. Keep this file across
deployments and use the same file for every restart of the same account. An
optional `--state-file PATH` selects another account's file; paths supplied on
the command line are relative to the working directory. Do not share a file
across accounts. An OS lock prevents two processes using the same state file.

The checkpoint records MM/pair positions and cash separately, pair entry/exit
timers, order ownership, processed fills, and the PnL baseline/high-water mark.
On restart, configuration must match and actual exchange positions must equal
the sum of saved ownership. Outstanding orders and positions outside A/B block
startup. No inventory is automatically reassigned or flattened at startup.

Normal shutdown still cancels MM orders and attempts to close pair positions.
It then reconciles fills and atomically saves an order-free checkpoint. MM
inventory can therefore resume without a flat account. Session duration restarts;
pair holding time includes downtime. Market models warm up with fresh data, so
an inherited pair may exit under the existing model-unavailable rule. Loss and
drawdown stop flags remain latched; restarting does not reset those risk limits.

Before a trading step or shutdown sends requests, the checkpoint is durably
marked unconfirmed. Runtime checkpoints with outstanding orders, unresolved IOC
outcomes, or account faults cannot automatically resume. A crash in that window
requires manual reconciliation even if net positions match: offsetting MM/pair
fills cannot be reconstructed from net inventory. A settled, order-free runtime
checkpoint can resume after interruption. State write failure blocks execution.

For the first deployment, nonzero inventory without a saved checkpoint still
blocks startup. Historical logs are not automatically trusted as final account
state. Do not delete/edit the checkpoint to bypass a mismatch; preserve it and
reconcile the account first. This feature cannot recover attribution retroactively
for a run that did not save it.

The baseline loader still requires the sibling `common/trade_logger.py` directory
from the original workspace. This change does not modify baseline/cycle pricing.

Offline regression checks (no Optibook connection):

```bash
python -B -m unittest discover -s tests -p test_hybrid_state.py -v
```
