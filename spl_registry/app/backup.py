"""Database backups: consistent snapshots, validation of uploaded backups, and restore.

Snapshots use SQLite's online backup API, so they are consistent even while the app is in use.
Before any restore, the current database is saved to /data/backups as a safety copy.
"""
import re
import sqlite3
from datetime import datetime, timezone

from . import db
from .config import DATA_DIR

BACKUP_DIR = DATA_DIR / 'backups'
SAFETY_PREFIX = 'before-restore-'
KEEP_SAFETY_COPIES = 10
MAX_UPLOAD_BYTES = 500 * 1024 * 1024
REQUIRED_TABLES = {'assessors', 'people', 'bundles', 'inductions', 'sessions', 'records', 'outbox', 'audit_log'}
NAME_RE = re.compile(r'^[A-Za-z0-9_.-]+\.db$')


class BackupError(ValueError):
    pass


def _stamp():
    return datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')


def snapshot(dest):
    """Copy the live database to `dest` (a path) as one consistent file."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(dest)
    try:
        with db._lock:
            db.conn().backup(target)
    finally:
        target.close()
    return dest


def summary_of(c):
    """Counts that let someone tell two databases apart before restoring."""
    one = lambda sql: c.execute(sql).fetchone()[0]  # noqa: E731
    return {
        'people': one('SELECT COUNT(*) FROM people'),
        'records': one("SELECT COUNT(*) FROM records WHERE status != 'cancelled'"),
        'complete': one("SELECT COUNT(*) FROM records WHERE status = 'complete'"),
        'assessors': one('SELECT COUNT(*) FROM assessors WHERE active = 1'),
        'inductions': one('SELECT COUNT(*) FROM inductions WHERE active = 1'),
        'latest_record': one('SELECT MAX(COALESCE(assessor_signed_at, created_at)) FROM records'),
        'last_activity': one('SELECT MAX(at) FROM audit_log'),
    }


def current_summary():
    return summary_of(db.conn())


def validate(path):
    """Check an uploaded file is an SPL Registry database. Returns its summary."""
    with open(path, 'rb') as f:
        if f.read(16) != b'SQLite format 3\x00':
            raise BackupError('That file is not a database backup from this app (.db file expected).')
    try:
        c = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    except sqlite3.Error:
        raise BackupError('The file could not be opened as a database.')
    try:
        if c.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise BackupError('The backup file is damaged (integrity check failed).')
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        missing = REQUIRED_TABLES - tables
        if missing:
            raise BackupError('This is not an SPL Registry backup (missing: ' + ', '.join(sorted(missing)) + ').')
        if not c.execute('SELECT COUNT(*) FROM assessors WHERE active = 1').fetchone()[0]:
            raise BackupError('The backup has no active assessor, so nobody could log in after restoring it.')
        return summary_of(c)
    except sqlite3.Error as e:
        raise BackupError(f'The backup could not be read: {e}')
    finally:
        c.close()


def save_upload(fileobj):
    """Stream an upload to the backups folder. Returns the saved path."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    path = BACKUP_DIR / f'upload-{_stamp()}.db'
    size = 0
    with open(path, 'wb') as out:
        while chunk := fileobj.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                out.close()
                path.unlink(missing_ok=True)
                raise BackupError('The file is too large to be a backup from this app.')
            out.write(chunk)
    return path


def restore(path):
    """Replace the live database with the backup at `path`, after saving a safety copy.
    Returns the safety copy's file name."""
    validate(path)
    stamp, n = _stamp(), 1
    safety = BACKUP_DIR / f'{SAFETY_PREFIX}{stamp}.db'
    while safety.exists():  # never overwrite an existing copy (it may be the one being restored)
        n += 1
        safety = BACKUP_DIR / f'{SAFETY_PREFIX}{stamp}-{n}.db'
    snapshot(safety)
    source = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    try:
        with db._lock:
            source.backup(db.conn())
            db._migrate(db.conn())  # an older backup may predate newer columns
    finally:
        source.close()
    prune()
    return safety.name


def safety_copies():
    if not BACKUP_DIR.exists():
        return []
    files = sorted(BACKUP_DIR.glob(f'{SAFETY_PREFIX}*.db'), reverse=True)
    return [{'name': p.name, 'size': p.stat().st_size,
             'at': datetime.strptime(p.stem[len(SAFETY_PREFIX):][:15], '%Y%m%d-%H%M%S')
             .replace(tzinfo=timezone.utc).isoformat(timespec='seconds')} for p in files]


def backup_path(name):
    """Resolve a file name in the backups folder, refusing anything else."""
    if not NAME_RE.match(name or ''):
        raise BackupError('Unknown backup.')
    p = BACKUP_DIR / name
    if not p.is_file():
        raise BackupError('That backup no longer exists.')
    return p


def prune():
    for p in sorted(BACKUP_DIR.glob(f'{SAFETY_PREFIX}*.db'), reverse=True)[KEEP_SAFETY_COPIES:]:
        p.unlink(missing_ok=True)
    for p in BACKUP_DIR.glob('upload-*.db'):  # uploads older than a day were never restored
        if (datetime.now().timestamp() - p.stat().st_mtime) > 86400:
            p.unlink(missing_ok=True)
