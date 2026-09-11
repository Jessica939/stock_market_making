"""Account reconciliation before unlocking a disconnected run."""
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


def cancel_outstanding_orders(exchange):
    """Cancel every visible A/B order and confirm both order books are clear."""
    cancelled = []
    for iid in (A, B):
        orders = exchange.get_outstanding_orders(iid)
        if not isinstance(orders, dict):
            raise ValueError(f'{iid} outstanding-order snapshot is invalid')
        for order_id in tuple(orders):
            if type(order_id) is not int or order_id < 0:
                raise ValueError(f'{iid} has an invalid order id')
            response = exchange.delete_order(iid, order_id=order_id)
            remaining = exchange.get_outstanding_orders(iid)
            if not isinstance(remaining, dict):
                raise ValueError(f'{iid} cancellation could not be confirmed')
            if order_id in remaining:
                reason = getattr(response, 'error_reason', 'order remains outstanding')
                raise ValueError(f'{iid} order {order_id} cancellation failed: {reason}')
            cancelled.append(dict(instrument=iid, order_id=order_id,
                                  response_success=getattr(response, 'success', None)))
    for iid in (A, B):
        remaining = exchange.get_outstanding_orders(iid)
        if not isinstance(remaining, dict) or remaining:
            raise ValueError(f'{iid} still has orders after cancellation')
    return cancelled


def audit_account(exchange, baseline):
    if not exchange.is_connected():
        raise ValueError('reconciliation requires a fresh connected session')
    # A new connection after the old session ended, and fresh account queries.
    # Never infer IOC completion from an empty private-fill poll.
    for iid in (A, B):
        orders = exchange.get_outstanding_orders(iid)
        if not isinstance(orders, dict) or orders:
            raise ValueError(f'{iid} still has orders or its order snapshot is invalid')
    actual = exchange.get_positions()
    if (not isinstance(actual, dict)
            or any(type(actual.get(iid)) is not int or abs(actual[iid]) > 100 for iid in (A, B))):
        raise ValueError('invalid/out-of-limit A/B positions during reconciliation')
    previous_baseline = baseline
    # Recovery explicitly adopts the fresh account snapshot. Any difference
    # from the old baseline is retained below for audit, not treated as owned
    # inventory of the new run.
    baseline = actual[B]
    if not exchange.is_connected():
        raise ValueError('connection lost during reconciliation')
    adopted = baseline != previous_baseline
    return dict(actual_positions=actual, baseline_B=baseline,
                previous_baseline_B=previous_baseline if adopted else None,
                baseline_change_B=baseline-previous_baseline if adopted else 0,
                owned_B=0, safe_to_start=True,
                reconciliation=('adopted_current_B_as_new_baseline' if adopted
                                else 'new_session_account_at_original_B_baseline'))
