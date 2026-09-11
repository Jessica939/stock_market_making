"""Pair-specific lifecycle layered on the shared execution session."""
from ..common.execution import ExecutionFault
from ..common.market import UnusableBook
from ..common.runner import Session
from .holding import HoldingGuard


class PairSession(Session):
    """Add bounded pair holding and cycle monitoring to a generic session."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.holding_guard = HoldingGuard(self.config)
        self.pair_exit_reason = None

    def before_frame(self, positions):
        if not self.pair_exit_reason:
            return False
        if any(positions.values()):
            self._reduce(self.pair_exit_reason)
            return True
        self.pair_exit_reason = None
        self.holding_guard.reset()
        return False

    def observe_inventory(self, positions, *, reason=None, equity=None):
        """Return True when observation or latched reduction consumes this step."""
        if not any(positions.values()):
            self.holding_guard.reset()
            return False
        if not self.executor.account_consistent:
            raise ExecutionFault('pair observation requires reconciled inventory')
        stopping = (self.risk_halt or self.executor.halted or self.journal.failed or
                    self.clock() >= self.executor.entry_deadline)
        was_paused = self.holding_guard.blocked_since is not None
        action = self.holding_guard.evaluate(
            self.clock(), positions, getattr(self.policy, 'active', None), reason=reason,
            equity=equity, baseline=self.baseline, peak=self.peak, stopping=stopping)
        if equity is not None:
            self.last_equity = equity
            if self.peak is not None:
                self.peak = max(self.peak, equity)
        if action == 'resume':
            if was_paused:
                self.journal.emit('pair_market_resumed', positions=positions)
            return False
        if action == 'wait':
            self.pending = None
            self.journal.emit(
                'pair_market_observation', reason=reason, positions=positions,
                elapsed=self.clock() - self.holding_guard.blocked_since,
                liquidation_equity=equity, entry_allowed=False)
            return True
        self.pending = None
        self.pair_exit_reason = action
        if action == 'loss_limit':
            self.risk_halt = True
        self.policy._exit(action)
        self.journal.emit('pair_exit_requested', reason=action, market_reason=reason,
                          positions=positions, liquidation_equity=equity)
        self._reduce(action)
        return True

    def _monitor_pair(self, positions):
        """Make a risk-only decision which cannot introduce new inventory."""
        frame = self.feed.holding_frame()
        equity = self.feed.liquidation_equity(require_bounded=False)
        self.last_equity = equity
        if self.baseline is not None:
            self.peak = max(self.peak, equity)
            if (equity - self.baseline <= -self.config['max_session_loss'] or
                    self.peak - equity >= self.config['max_drawdown']):
                self.risk_halt = True
        self.holding_guard.reset()
        if self.observe_inventory(positions, equity=equity):
            return
        decision = self.policy.monitor_cycle_position(frame, positions)
        self.pending = None
        self.journal.emit('pair_holding_decision', reason=decision['reason'],
                          diagnostics=decision['diagnostics'], positions=positions,
                          liquidation_equity=equity, entry_allowed=False)
        if not any(decision['targets'].values()):
            self.pair_exit_reason = decision['reason']
            self.journal.emit('pair_exit_requested', reason=decision['reason'],
                              positions=positions)
            self._reduce(decision['reason'])

    def handle_unusable_market(self, error, elapsed):
        if not self.initialized:
            return False
        try:
            actual = self.executor.positions()
            if (any(actual.values()) and self.config.get('pair_independent_holding', False)
                    and self.config.get('relation_mode') == 'cycle'):
                try:
                    self._monitor_pair(actual)
                    return True
                except UnusableBook as monitoring_error:
                    self.journal.emit('pair_monitor_unavailable', reason=str(monitoring_error))
            value = None
            if any(actual.values()):
                try:
                    value = self.feed.liquidation_equity()
                except UnusableBook:
                    pass
            return self.observe_inventory(actual, reason=str(error), equity=value)
        except ExecutionFault as account_error:
            self.risk_halt = True
            self.fatal_error = str(account_error)
            self.journal.emit('execution_halt', error=str(account_error))
            self._cancel_only('observation_account_fault')
            return True
