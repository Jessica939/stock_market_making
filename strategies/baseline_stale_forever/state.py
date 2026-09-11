"""Crash marker and clean-shutdown ownership checkpoint for baseline-stale."""
import copy
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
from datetime import datetime, timezone


class StateError(RuntimeError):
    pass


class StateStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.lock = None
        self.data = None

    def acquire(self):
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
            raise StateError('baseline-stale state is already in use') from exc

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
                raise ValueError('last run ended with unconfirmed execution; reconcile the account manually')
            positions, cash = data['positions'], data['cash']
            if set(positions) != {'mm', 'pair'} or set(cash) != {'mm', 'pair'}:
                raise ValueError('invalid owner map')
            symbols = set(config['symbols'])
            for values in (*positions.values(), *cash.values()):
                if set(values) != symbols:
                    raise ValueError('invalid instrument map')
                if any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or not math.isfinite(value) for value in values.values()):
                    raise ValueError('invalid account value')
            if any(not isinstance(value, int)
                   for values in positions.values() for value in values.values()):
                raise ValueError('positions must be integer lots')
            if any(positions['pair'].values()):
                raise ValueError('recoverable checkpoint contains stale inventory')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise StateError(f'Cannot resume {self.path}: {exc}') from exc
        self.data = data
        return copy.deepcopy(data)

    def archive_for_adoption(self):
        """Preserve the prior checkpoint before an explicit ownership reset."""
        if not self.path.exists():
            return None
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
        archive = self.path.with_name(self.path.name + '.before-adopt-' + stamp)
        shutil.copy2(self.path, archive)
        return archive

    def write(self, data):
        if self.lock is None:
            raise StateError('state lock is not held')
        payload = json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2)
        name = None
        try:
            with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=self.path.parent,
                                             prefix=self.path.name + '.', suffix='.tmp',
                                             delete=False) as stream:
                name = stream.name
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if name and os.path.exists(name):
                os.unlink(name)
        self.data = copy.deepcopy(data)

    def invalidate(self, config):
        data = copy.deepcopy(self.data or {})
        data.update(version=1, config=config, recoverable=False)
        self.write(data)

    def checkpoint(self, engine):
        actual = engine.account.audit()
        orders = {s: engine.account.orders(s) for s in engine.symbols}
        safe = (engine.raw.is_connected() and not engine.account.halted
                and not engine.stale.executor.unresolved
                and not any(engine.account.positions['pair'].values())
                and not any(orders.values()) and not engine.journal.failed)
        self.write(dict(version=1, config=engine.config, recoverable=bool(safe),
                        positions=engine.account.positions, cash=engine.account.cash,
                        actual_positions=actual))


