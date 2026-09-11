"""Durable ownership checkpoints. Only settled, order-free checkpoints resume."""
import copy
import json
import math
import os
from pathlib import Path
import tempfile


class StateError(RuntimeError):
    pass


class StateStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.lock = None
        self.data = None

    def acquire(self):
        """Hold an OS lock before connecting; process death releases the lock."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = open(str(self.path) + '.lock', 'a+b')
        try:
            if os.fstat(self.lock.fileno()).st_size == 0:
                self.lock.write(b'0')
                self.lock.flush()
            self.lock.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.close()
            raise StateError('Hybrid state is in use by another process') from exc

    def close(self):
        if self.lock is not None:
            self.lock.close()
            self.lock = None

    def load(self, config):
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if data['version'] != 1 or data['config'] != config:
                raise ValueError('state version/configuration mismatch')
            if data['recoverable'] is not True:
                raise ValueError('last run has unconfirmed orders or execution; manual reconciliation required')
            symbols = config['symbols']
            for field in ('positions', 'cash'):
                if set(data[field]) != {'mm', 'pair'}:
                    raise ValueError('invalid ownership map')
                for owner, values in data[field].items():
                    if set(values) != set(symbols):
                        raise ValueError('invalid instrument map')
                    for value in values.values():
                        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                            raise ValueError('invalid account number')
                        if field == 'positions' and (not isinstance(value, int) or abs(value) > config[owner + '_position_limit']):
                            raise ValueError('invalid or over-limit owned position')
            for symbol in symbols:
                if abs(sum(data['positions'][o][symbol] for o in ('mm', 'pair'))) > config['position_limit']:
                    raise ValueError('aggregate position exceeds limit')
            for key in ('saved_at', 'monotonic', 'baseline', 'peak', 'cooldown_until'):
                value = data[key]
                if value is None and key in ('baseline', 'peak', 'cooldown_until'):
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError('invalid ' + key)
            if any(data['positions']['pair'].values()) and not isinstance(data['active'], dict):
                raise ValueError('pair inventory has no saved policy state')
            if data['active'] is not None:
                for key in ('opened_at', 'exit_at', 'cycle_origin'):
                    value = data['active'][key]
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                        raise ValueError('invalid pair clock')
            if type(data['stopping']) is not bool:
                raise ValueError('invalid stop state')
            data['closing_reason'], data['stop_reason']
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise StateError(f'Cannot resume {self.path}: {exc}') from exc
        self.data = data
        return copy.deepcopy(data)

    def write(self, data):
        if self.lock is None:
            raise StateError('State lock must be acquired before writing')
        payload = json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2)
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                             prefix=self.path.name + '.', suffix='.tmp', delete=False) as stream:
                name = stream.name
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            if os.name != 'nt':
                fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        finally:
            if name and os.path.exists(name):
                os.unlink(name)
        self.data = copy.deepcopy(data)

    def invalidate(self):
        data = copy.deepcopy(self.data or {'version': 1})
        data['recoverable'] = False
        self.write(data)

    def checkpoint(self, engine, *, final=False):
        account = engine.account
        actual = account.audit()
        orders = {s: account.orders(s) for s in engine.symbols}
        safe = (account.raw.is_connected() and not account.halted
                and not engine.pair_executor.unresolved and not engine.journal.failed
                and not any(orders.values()))
        # Clearing session/keyboard stop is intentional; risk stops remain latched.
        stopping = engine.stopping
        if final and engine.stop_reason in ('keyboard_interrupt', 'session_deadline',
                                           'entry_deadline', 'runner_exit', 'stop_requested'):
            stopping = False
        cooldown = engine.pair_policy.cooldown_until
        self.write(dict(version=1, config=engine.config, recoverable=bool(safe),
                        saved_at=engine.wall(), monotonic=engine.clock(),
                        positions=account.positions, cash=account.cash, actual_positions=actual,
                        active=engine.pair_policy.active,
                        cooldown_until=cooldown if math.isfinite(cooldown) else None,
                        closing_reason=engine.pair_policy.closing_reason,
                        baseline=engine.baseline, peak=engine.peak,
                        stopping=stopping, stop_reason=engine.stop_reason if stopping else None,
                        orders=[dict(instrument=s, order_id=oid, **entry)
                                for (s, oid), entry in account.registry.items()],
                        seen_trades=[[s, tid, list(sig)] for (s, tid), sig in account.seen.items()]))


def restore_policy(engine, data):
    downtime = engine.wall() - data['saved_at']
    if downtime < 0:
        raise StateError('Wall clock moved backwards; cannot restore holding timers')
    shift = engine.clock() - data['monotonic'] - downtime
    active = copy.deepcopy(data['active'])
    if active is not None:
        for key in ('opened_at', 'exit_at', 'cycle_origin'):
            active[key] += shift
    engine.pair_policy.active = active
    engine.pair_policy.cooldown_until = (float('-inf') if data['cooldown_until'] is None
                                         else data['cooldown_until'] + shift)
    engine.pair_policy.closing_reason = data['closing_reason']
    engine.baseline, engine.peak = data['baseline'], data['peak']
    engine.stopping, engine.stop_reason = data['stopping'], data['stop_reason']
    # Market models warm up from fresh data. Existing pair inventory follows its
    # original expiry and the existing model-unavailable exit rule.
