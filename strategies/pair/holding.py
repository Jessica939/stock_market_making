"""Bounded observation of a confirmed pair, never permission to add inventory."""
class HoldingGuard:
    def __init__(self, config):
        self.config = config
        self.reset()

    def reset(self):
        self.blocked_since = self.missing_since = self.healthy_since = None

    def evaluate(self, now, positions, held, *, reason, equity, baseline, peak, stopping=False):
        if not any(positions.values()):
            self.reset()
            return 'resume'
        a, b = self.config['symbols']
        if positions[a] != -positions[b] or held is None:
            return 'unmatched_or_untracked_inventory'
        if (abs(positions[a]) > held['size'] or
                (1 if positions[a] > 0 else -1) != held['direction']):
            return 'unexpected_inventory_change'
        if stopping:
            return 'risk_or_session_stop'
        planned = held.get('exit_at', float('inf'))
        if self.config.get('cycle_extension_seconds', 0) and self.config.get('pair_independent_holding', False):
            planned = held.get('original_exit_at', planned) + self.config['cycle_extension_seconds']
        deadline = min(planned,
                       held['opened_at'] + self.config['max_hold_seconds'])
        if now >= deadline:
            return 'holding_deadline'
        if equity is not None and baseline is not None:
            if (equity - baseline <= -self.config['max_session_loss'] or
                    peak - equity >= self.config['max_drawdown']):
                return 'loss_limit'
        if reason is None and self.blocked_since is None:
            return 'resume'
        if self.blocked_since is None:
            self.blocked_since = now
        if equity is None:
            if self.missing_since is None:
                self.missing_since = now
            if now - self.missing_since >= self.config.get('pair_valuation_grace_seconds', 0):
                return 'valuation_timeout'
        else:
            self.missing_since = None
        if now - self.blocked_since >= self.config.get('pair_market_grace_seconds', 0):
            return 'market_timeout'
        if reason is not None:
            self.healthy_since = None
            return 'wait'
        if self.healthy_since is None:
            self.healthy_since = now
        if now - self.healthy_since < self.config.get('pair_recovery_seconds', 0):
            return 'wait'
        self.reset()
        return 'resume'
