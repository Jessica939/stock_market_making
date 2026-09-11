"""Move legacy local files with a persisted path/hash audit; default is preview."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def checked(path):
    path = Path(path).resolve()
    if not path.is_relative_to(ROOT) or path == ROOT:
        raise ValueError(f'Path outside project: {path}')
    return path


def relative(path):
    return checked(path).relative_to(ROOT).as_posix()


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def describe_log(path, strategy, target):
    first = settings = None
    count = malformed = 0
    with path.open(encoding='utf-8-sig') as stream:
        for line in stream:
            if not line.strip():
                continue
            count += 1
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError('not an object')
            except ValueError:
                malformed += 1
                continue
            if first is None:
                first = row
            if settings is None and (row.get('type') == 'settings' or row.get('action') == 'settings'):
                settings = row
    first = first or {}
    started = first.get('observed_at_utc', first.get('recorded_at'))
    source = 'first_log_record' if started else 'filename_utc_seconds'
    if not started:
        stamp = path.stem.split('_')[1].rstrip('Z')
        started = datetime.strptime(stamp, '%Y%m%dT%H%M%S').replace(tzinfo=timezone.utc).isoformat()
    config = None
    if settings is not None:
        config = settings.get('config')
        if config is None:
            config = {k: v for k, v in settings.items()
                      if k not in ('type', 'action', 'observed_at_utc', 'recorded_at')}
    return dict(schema_version=1, run_id=target.parent.name, strategy=strategy,
                mode=(settings or {}).get('mode', 'unknown'), started_at_utc=started,
                config=config, event_log=target.name, market_recording=None,
                migration=dict(original_log=relative(path), timestamp_source=source,
                               config_source='logged_settings_only' if settings else 'not_recorded',
                               market_link_status='not_recorded_in_legacy_log',
                               nonempty_lines=count, malformed_lines=malformed))


def plan():
    files, manifests, directories = [], [], set()
    market = checked(ROOT / 'price_data')
    for session in sorted(market.glob('philips_*')):
        if not session.is_dir():
            continue
        target = checked(ROOT / 'data/market' / session.name)
        if target.exists():
            raise FileExistsError(target)
        for path in sorted(session.rglob('*')):
            checked(path)
            if path.is_symlink():
                raise ValueError(f'Symlink cannot be migrated: {path}')
            if path.is_file():
                files.append((path, checked(target / path.relative_to(session))))
            elif path.is_dir():
                directories.add(path)
        directories.add(session)
    directories.add(market)
    for folder, strategy, pattern in [('logs', 'baseline', 'trading_*.jsonl'),
                                      ('logs/hybrid', 'hybrid', '*.jsonl'),
                                      ('logs/cleanup', 'cleanup', '*.jsonl'),
                                      ('strategies/pair/runs', 'pair', '*.jsonl')]:
        parent = checked(ROOT / folder)
        directories.add(parent)
        for path in sorted(parent.glob(pattern)):
            checked(path)
            if path.is_symlink():
                raise ValueError(f'Symlink cannot be migrated: {path}')
            run_id = 'run_' + path.stem.removeprefix('trading_').removeprefix('run_')
            directory = checked(ROOT / 'data/runs' / strategy / run_id)
            if directory.exists():
                raise FileExistsError(directory)
            target = directory / (path.name if strategy == 'baseline' else 'events.jsonl')
            files.append((path, target))
            manifests.append((directory / 'manifest.json', describe_log(path, strategy, target)))
    rows = []
    for source, target in files:
        checked(source)
        checked(target)
        if target.exists():
            raise FileExistsError(target)
        stat = source.stat()
        rows.append(dict(source=relative(source), destination=relative(target),
                         bytes=stat.st_size, mtime_ns=stat.st_mtime_ns, sha256=digest(source)))
    if len({r['destination'] for r in rows}) != len(rows):
        raise ValueError('Duplicate destination')
    return rows, manifests, directories


def write_report(path, data):
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    rows, manifests, directories = plan()
    print(json.dumps(dict(files=len(rows), bytes=sum(r['bytes'] for r in rows),
                          runs=dict(Counter(m['strategy'] for _, m in manifests))), indent=2))
    if not args.apply or not rows:
        return
    # Recheck the entire source inventory before starting any move.
    for row in rows:
        source = checked(ROOT / row['source'])
        if source.stat().st_mtime_ns != row['mtime_ns'] or digest(source) != row['sha256']:
            raise RuntimeError(f'Source changed during planning: {source}')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    report_path = checked(ROOT / 'data/migrations' / f'legacy_{stamp}.json')
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = dict(status='in_progress', migrated_at_utc=stamp, files=rows,
                  manifests=[relative(p) for p, _ in manifests])
    write_report(report_path, report)
    try:
        for row in rows:
            source, target = checked(ROOT / row['source']), checked(ROOT / row['destination'])
            if source.stat().st_mtime_ns != row['mtime_ns'] or digest(source) != row['sha256']:
                raise RuntimeError(f'Source changed before move: {source}')
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise FileExistsError(target)
            # File-only rename on the same filesystem; never recursively move a directory.
            source.rename(target)
        for path, manifest in manifests:
            manifest['migration']['audit'] = '../../../migrations/' + report_path.name
            with checked(path).open('x', encoding='utf-8') as stream:
                json.dump(manifest, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
        for row in rows:
            target = checked(ROOT / row['destination'])
            if target.stat().st_size != row['bytes'] or digest(target) != row['sha256']:
                raise RuntimeError(f'Post-move verification failed: {target}')
            if checked(ROOT / row['source']).exists():
                raise RuntimeError(f'Old path still exists: {row["source"]}')
        for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
            directory = checked(directory)
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()  # Empty directories only.
        report['status'] = 'verified'
    except BaseException as exc:
        report['status'] = 'incomplete'
        report['error'] = str(exc)
        raise
    finally:
        write_report(report_path, report)
        print(f'Migration audit: {report_path}')


if __name__ == '__main__':
    main()
