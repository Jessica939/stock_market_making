"""Local run metadata; market recordings remain independently reusable."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MARKET_DIR = PROJECT_ROOT / 'data' / 'market'
RUNS_DIR = PROJECT_ROOT / 'data' / 'runs'


class RunStorage:
    def __init__(self, strategy, *, directory=None, mode='live', config=None, **metadata):
        stamp = datetime.now(timezone.utc)
        self.run_id = stamp.strftime('run_%Y%m%dT%H%M%S_%fZ_') + uuid4().hex[:8]
        self.directory = (Path(directory) if directory is not None else RUNS_DIR / strategy).resolve() / self.run_id
        self.directory.mkdir(parents=True, exist_ok=False)
        self.path = self.directory / 'manifest.json'
        self.data = dict(schema_version=1, run_id=self.run_id, strategy=strategy,
                         mode=mode, started_at_utc=stamp.isoformat(), config=config or {},
                         event_log=None, market_recording=None, **metadata)
        self.update()

    def relative_path(self, path):
        try:
            return Path(os.path.relpath(Path(path).resolve(), self.directory)).as_posix()
        except ValueError:  # An explicitly selected different Windows drive.
            return str(Path(path).resolve())

    def update(self, **fields):
        data = dict(self.data, **fields)
        payload = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
        temporary = self.path.with_suffix('.json.tmp')
        temporary.write_text(payload, encoding='utf-8')
        temporary.replace(self.path)
        self.data = data

    def link_events(self, path):
        self.update(event_log=self.relative_path(path))

    def link_market(self, directory):
        self.update(market_recording=dict(recording_id=Path(directory).name,
                                         path=self.relative_path(directory)))


def hybrid_state_path(account='default', *, root=PROJECT_ROOT):
    """Keep existing default-account checkpoints/locks at their original path."""
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', account):
        raise ValueError('account must contain only letters, numbers, underscores or hyphens')
    root = Path(root)
    target = root / 'state' / account / 'hybrid.json'
    legacy = root / 'state' / 'hybrid.json'
    if account == 'default' and (legacy.exists() or Path(str(legacy) + '.lock').exists()):
        if target.exists() or Path(str(target) + '.lock').exists():
            raise ValueError('Both legacy and new hybrid state paths exist; select --state-file explicitly')
        return legacy
    return target
