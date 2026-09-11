"""Read-only account reconciliation before unlocking a disconnected run."""
import json
from pathlib import Path

A, B = 'PHILIPS_A', 'PHILIPS_B'


def previous_baseline(prior, log_dir):
    baseline = prior.get('baseline_B')
    if type(baseline) is int and abs(baseline) <= 100:
        return baseline
    run_id = prior.get('run_id')
    if not isinstance(run_id, str) or Path(run_id).name != run_id:
        raise ValueError('previous B baseline unavailable; original run log is required')
    # v1 overwrote the baseline on a disconnected finish. Recover only from
    # the exact run referenced by the state, never the latest unrelated log.
    path = Path(log_dir)/run_id/'events.jsonl'
    found = set()
    try:
        with path.open(encoding='utf-8') as stream:
            for line in stream:
                row = json.loads(line)
                if row.get('type') == 'stale_risk_stop' or row.get('risk_stopped') is True:
                    raise ValueError('original run was risk-stopped; automatic unlocking is disabled')
                if row.get('type') in ('inventory_baseline', 'settings'):
                    value = row.get('baseline_B')
                    if type(value) is int and abs(value) <= 100:
                        found.add(value)
    except (OSError, ValueError) as exc:
        raise ValueError(f'cannot recover original B baseline from {path}: {exc}') from exc
    if len(found) != 1:
        raise ValueError('original run has missing/conflicting B baselines; cannot unlock')
    return found.pop()


def audit_account(exchange, baseline):
    if not exchange.is_connected():
        raise ValueError('reconciliation requires a fresh connected session')
    # A new connection after the old session ended, and fresh account queries.
    # No insertion, cancellation, liquidation or inference from empty fill polls.
    for iid in (A, B):
        orders = exchange.get_outstanding_orders(iid)
        if not isinstance(orders, dict) or orders:
            raise ValueError(f'{iid} still has orders or its order snapshot is invalid')
    actual = exchange.get_positions()
    if (not isinstance(actual, dict)
            or any(type(actual.get(iid)) is not int or abs(actual[iid]) > 100 for iid in (A, B))):
        raise ValueError('invalid/out-of-limit A/B positions during reconciliation')
    if actual[B] != baseline:
        raise ValueError(f'B actual={actual[B]}, original baseline={baseline}, '
                         f'residual={actual[B]-baseline}; no orders sent, state remains locked')
    if not exchange.is_connected():
        raise ValueError('connection lost during reconciliation')
    return dict(actual_positions=actual, baseline_B=baseline, owned_B=0,
                safe_to_start=True, reconciliation='new_session_account_at_original_B_baseline')
