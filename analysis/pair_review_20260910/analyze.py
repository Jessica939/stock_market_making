"""Reconcile pair episodes from private fills; no exchange access or simulations."""
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = Path(__file__).resolve().parent
ENTRY_REASONS = {'cycle_long_A_short_B', 'cycle_short_A_long_B'}


def time(row):
    return datetime.fromisoformat(row['recorded_at'])


def main():
    runs, all_episodes = [], []
    for path in sorted((ROOT / 'data/runs/pair').glob('*/events.jsonl')):
        rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
        settings = next(r for r in rows if r['type'] == 'settings')
        summary = next(r for r in reversed(rows) if r['type'] == 'session_end')
        entries = [(i, r) for i, r in enumerate(rows)
                   if r['type'] == 'decision' and r['reason'] in ENTRY_REASONS]
        counts = Counter(r['type'] for r in rows)
        episodes = []
        for n, (i, entry) in enumerate(entries):
            part = rows[i:entries[n + 1][0] if n + 1 < len(entries) else len(rows)]
            fills = [r for r in part if r['type'] == 'fill']
            assert len({(r['instrument'], r['trade_id']) for r in fills}) == len(fills)
            positions, cash = Counter(), 0.
            for fill in fills:
                direction = 1 if fill['side'] == 'bid' else -1
                positions[fill['instrument']] += direction * fill['volume']
                cash -= direction * fill['price'] * fill['volume']
            closed = bool(fills) and not any(positions.values())
            paired = len({f['instrument'] for f in fills}) == 2
            reduce_index = next((k for k, r in enumerate(part)
                                 if r['type'] == 'order_attempt' and r['reducing']), len(part))
            triggers = [r for r in part[:reduce_index]
                        if r['type'] in ('decision', 'market_blocked', 'pair_unmatched')]
            trigger = triggers[-1] if triggers else {}
            episode = dict(run=path.parent.name,
                           entry_time_beijing=time(entry).astimezone(timezone(timedelta(hours=8))).isoformat(),
                           size=entry['diagnostics']['pair_size'], direction=entry['reason'],
                           paired=paired, closed=closed, fill_cash_change=cash,
                           realized_pnl=cash if closed else None, remaining_positions=dict(positions),
                           hold_seconds=(time(fills[-1]) - time(fills[0])).total_seconds() if closed else None,
                           exit_trigger_type=trigger.get('type'),
                           exit_trigger_reason=trigger.get('reason'),
                           forecast_horizon_seconds=entry['diagnostics']['cycle']['horizon_seconds'],
                           estimated_net_edge_per_pair=entry['diagnostics']['estimated_net_edge'],
                           in_sample_r2=entry['diagnostics']['cycle']['fit_r2'])
            episodes.append(episode)
        # Fully closed sessions reconcile independently to the runner's post-cleanup baseline.
        if summary['flat']:
            assert abs(sum(e['fill_cash_change'] for e in episodes) - summary['pnl_change']) < 1e-6
        blocked_fraction = counts['market_blocked'] / max(1, counts['market_blocked'] + counts['decision'])
        run = dict(run=path.parent.name, source=path.relative_to(ROOT).as_posix(),
                   started_beijing=time(settings).astimezone(timezone(timedelta(hours=8))).isoformat(),
                   duration_seconds=(time(summary) - time(settings)).total_seconds(),
                   logged_pnl_change=summary['pnl_change'], final_positions=summary['final_positions'],
                   confirmed_flat=summary['flat'], counts=dict(counts), config=settings['config'],
                   blocked_fraction_of_decision_or_block_events=blocked_fraction,
                   decision_reasons=dict(Counter(r['reason'] for r in rows if r['type'] == 'decision')),
                   episodes=episodes, session_end=summary)
        runs.append(run)
        all_episodes.extend(episodes)
    paired_closed = [e for e in all_episodes if e['paired'] and e['closed']]
    closed = [e for e in all_episodes if e['closed']]
    totals = dict(logged_pnl_including_final_inventory_mark=sum(r['logged_pnl_change'] for r in runs),
                  entry_attempts=len(all_episodes), closed_paired_episodes=len(paired_closed),
                  winning_closed_pairs=sum(e['realized_pnl'] > 0 for e in paired_closed),
                  closed_episode_realized_pnl=sum(e['realized_pnl'] for e in closed),
                  paired_closed_realized_pnl=sum(e['realized_pnl'] for e in paired_closed),
                  median_closed_pair_hold_seconds=median(e['hold_seconds'] for e in paired_closed),
                  longest_closed_pair_hold_seconds=max(e['hold_seconds'] for e in paired_closed),
                  closed_pairs_exited_on_market_block=sum(e['exit_trigger_type'] == 'market_blocked' for e in paired_closed))
    (OUTPUT / 'metrics.json').write_text(json.dumps(dict(totals=totals, runs=runs), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(totals, indent=2))
    for run in runs:
        print(run['run'], 'blocked_fraction', round(run['blocked_fraction_of_decision_or_block_events'], 4))


if __name__ == '__main__':
    main()
