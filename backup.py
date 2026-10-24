"""Verified SQLite snapshots; automatic copies never overwrite manual backups."""
import sqlite3
import sys
import threading
import time
from contextlib import closing
from datetime import datetime, timezone, timedelta
from pathlib import Path
from uuid import uuid4

INTERVAL = 4 * 60 * 60
RETENTION = 30 * 24 * 60 * 60
_state = {'enabled': False, 'last_backup': '', 'error': '', 'directory': ''}
_lock = threading.Lock()


def snapshot(source_path, destination_path):
    source_path, destination_path = Path(source_path), Path(destination_path)
    if source_path.resolve() == destination_path.resolve():
        raise ValueError('备份文件不能覆盖正在使用的数据库')
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination_path.with_name(destination_path.name + '.' + uuid4().hex + '.tmp')
    try:
        with closing(sqlite3.connect(source_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=15)) as source:
            with closing(sqlite3.connect(temporary)) as destination:
                source.backup(destination)
                if destination.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise RuntimeError('备份完整性检查失败')
        temporary.replace(destination_path)
    finally:
        temporary.unlink(missing_ok=True)


def automatic(source_path, directory, now=None):
    """Return the newest snapshot; prune only our expired files after success."""
    now = time.time() if now is None else now
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    copies = sorted(directory.glob('auto-*.db'), key=lambda p: p.stat().st_mtime)
    if copies and now - copies[-1].stat().st_mtime < INTERVAL:
        return copies[-1]
    stamp = datetime.fromtimestamp(now, timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S')
    target = directory / ('auto-' + stamp + '-' + uuid4().hex[:8] + '.db')
    snapshot(source_path, target)
    for old in copies:
        if now - old.stat().st_mtime > RETENTION:
            old.unlink()
    return target


def status():
    with _lock:
        return dict(_state)


def start(source_path):
    directory = Path(source_path).resolve().parent.parent / 'backups' / 'automatic'
    with _lock:
        if _state['enabled']:
            return
        _state.update(enabled=True, directory=str(directory))

    def run_once():
        try:
            target = automatic(source_path, directory)
            stamp = datetime.fromtimestamp(target.stat().st_mtime, timezone(timedelta(hours=8))).isoformat(timespec='seconds')
            with _lock:
                _state.update(last_backup=stamp, error='')
        except Exception:
            # Keep account data and previous backups intact; show failure in the UI.
            with _lock:
                _state['error'] = '自动备份失败，请检查备份目录权限和磁盘空间；软件将自动重试'

    run_once()

    def worker():
        while True:
            time.sleep(60)
            run_once()

    threading.Thread(target=worker, daemon=True, name='automatic-backup').start()


if __name__ == '__main__':
    snapshot(sys.argv[1], sys.argv[2])
