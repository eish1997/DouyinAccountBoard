"""Fail-closed synthetic environment. No application imports before install()."""
import json
import os
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parent
MARKER = '.synthetic-environment.json'
PROTECTED = tuple(PROJECT / name for name in (
    'data', 'backups', 'logs', 'desktop-profile', 'desktop', 'artifacts', 'optional'))


def inside(path, root):
    return path == root or root in path.parents


def validate_root(value):
    if not value:
        raise ValueError('Explicit synthetic test root required')
    path = Path(value).absolute()
    resolved = path.resolve()
    if path != resolved or any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)):
        raise ValueError('Test root must not use links or junctions')
    if not inside(resolved, PROJECT / '.synthetic-tests') or resolved == PROJECT / '.synthetic-tests':
        raise ValueError('Test root must be a dedicated child of .synthetic-tests')
    return resolved


def create_root(value):
    root = validate_root(value)
    root.mkdir(parents=True, exist_ok=False)
    (root / MARKER).write_text(json.dumps({'kind': 'synthetic-only', 'version': 1}), encoding='utf-8')
    return root


def checked_root():
    root = validate_root(os.environ.get('DOUYIN_BOARD_TEST_ROOT'))
    if json.loads((root / MARKER).read_text(encoding='utf-8')) != {'kind': 'synthetic-only', 'version': 1}:
        raise ValueError('Invalid synthetic environment marker')
    return root


def checked_database(value):
    root = checked_root()
    path = Path(value).resolve()
    if not inside(path, root) or path == root:
        raise ValueError('Database outside synthetic test root')
    return path


def install(root):
    """Defense in depth for reviewed Python tests, not an OS security sandbox."""
    root = validate_root(root)
    os.environ['DOUYIN_BOARD_TEST_ROOT'] = str(root)
    checked_root()
    os.environ['DOUYIN_BOARD_DATA_DIR'] = str(root / 'data')
    os.environ['DOUYIN_BOARD_WORKER_TOKEN'] = ''
    os.environ['DOUYIN_BOARD_COLLECTION_ENABLED'] = '0'
    os.environ['TMP'] = os.environ['TEMP'] = str(root)
    import tempfile
    tempfile.tempdir = str(root)

    def audit(event, args):
        if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0])).resolve()
            if any(inside(path, p) for p in PROTECTED):
                raise PermissionError('Protected project data access denied')
            if path.suffix.lower() in ('.db', '.sqlite', '.sqlite3') and not inside(path, root):
                raise PermissionError('Database file outside synthetic environment')
        elif event == 'sqlite3.connect':
            value = str(args[0])
            if value != ':memory:':
                if value.startswith('file:'):
                    from urllib.parse import urlsplit, unquote
                    value = unquote(urlsplit(value).path)
                    if os.name == 'nt' and value.startswith('/'):
                        value = value[1:]
                checked_database(value)
        elif event in ('socket.bind', 'socket.connect'):
            address = args[1]
            if not isinstance(address, tuple) or address[0] not in ('127.0.0.1', '::1'):
                raise PermissionError('Only loopback networking is allowed')
        elif event in ('subprocess.Popen', 'os.system'):
            raise PermissionError('Child processes require a separately reviewed isolated runner')
    sys.addaudithook(audit)


def synthetic_seed():
    return {'computers': [dict(id='synth-pc-1', name='合成电脑', shadow_account='', ip='', operator='', category='')],
            'accounts': [dict(id='synth-account-1', computer_id='synth-pc-1', slot=1,
                              name='合成账号', status='正常', note='', douyin_id='synthetic_001',
                              homepage='', work_count=None, last_collected=None)]}
