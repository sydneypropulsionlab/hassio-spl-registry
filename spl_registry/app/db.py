"""SQLite storage. One file in the data folder, so Home Assistant backups include it."""
import json
import sqlite3
import threading
from datetime import datetime, timezone

from .config import DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS assessors (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    pw_hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bundles (
    id INTEGER PRIMARY KEY,
    hash TEXT NOT NULL,
    generated_at TEXT,
    uploaded_by INTEGER REFERENCES assessors(id),
    uploaded_at TEXT NOT NULL,
    json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inductions (
    id TEXT NOT NULL,
    version TEXT NOT NULL,
    bundle_id INTEGER NOT NULL REFERENCES bundles(id),
    title TEXT NOT NULL,
    type TEXT NOT NULL,
    location TEXT NOT NULL,
    refresh_months INTEGER NOT NULL,
    self_service INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 0,
    json TEXT NOT NULL,
    PRIMARY KEY (id, version)
);
CREATE TABLE IF NOT EXISTS checklists (
    id TEXT NOT NULL,
    version TEXT NOT NULL,
    bundle_id INTEGER NOT NULL REFERENCES bundles(id),
    title TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT 'complete',
    active INTEGER NOT NULL DEFAULT 0,
    json TEXT NOT NULL,
    PRIMARY KEY (id, version)
);
-- one filled-in checklist. Draft until submitted; after that it never changes except for amendments.
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    checklist_id TEXT NOT NULL,
    checklist_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    assessor_id INTEGER REFERENCES assessors(id),
    submitted_by TEXT,
    started_at TEXT NOT NULL,
    submitted_at TEXT,
    outcome TEXT,
    answers TEXT NOT NULL DEFAULT '{}',
    comments TEXT,
    snapshot TEXT,
    amendments TEXT NOT NULL DEFAULT '[]',
    signed_name TEXT,
    signature TEXT,
    outcome_label TEXT
);
CREATE INDEX IF NOT EXISTS runs_checklist ON runs(checklist_id);
-- photos: on a checklist item (item_id) or on the run as a whole (item_id NULL)
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    item_id TEXT,
    filename TEXT NOT NULL,
    mime TEXT NOT NULL,
    size INTEGER NOT NULL,
    width INTEGER,
    height INTEGER,
    sha256 TEXT NOT NULL,
    taken_at TEXT,
    data BLOB NOT NULL,
    thumb BLOB NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT
);
CREATE INDEX IF NOT EXISTS attachments_run ON attachments(run_id);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL,
    induction_id TEXT NOT NULL,
    induction_version TEXT NOT NULL,
    assessor_id INTEGER NOT NULL REFERENCES assessors(id),
    created_at TEXT NOT NULL,
    join_until TEXT NOT NULL,
    closed_at TEXT,
    check_ticks TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS sessions_code ON sessions(code);
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY,
    session_id INTEGER REFERENCES sessions(id),
    person_id INTEGER NOT NULL REFERENCES people(id),
    induction_id TEXT NOT NULL,
    induction_version TEXT NOT NULL,
    assessor_id INTEGER REFERENCES assessors(id),
    status TEXT NOT NULL,
    participant_token TEXT NOT NULL UNIQUE,
    check_ticks TEXT NOT NULL DEFAULT '{}',
    demo_ticks TEXT NOT NULL DEFAULT '{}',
    ack_ticks TEXT NOT NULL DEFAULT '{}',
    inductee_name TEXT,
    inductee_signature TEXT,
    inductee_signed_at TEXT,
    assessor_signed_at TEXT,
    refresh_months INTEGER,
    expires_at TEXT,
    notes TEXT,
    snapshot TEXT,
    confirm_token_hash TEXT,
    confirm_sent_at TEXT,
    confirm_expires_at TEXT,
    confirmed_at TEXT,
    supersedes INTEGER REFERENCES records(id),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS records_person ON records(person_id);
CREATE INDEX IF NOT EXISTS records_confirm ON records(confirm_token_hash);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    to_addr TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT
);
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    prefix TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    created_by INTEGER REFERENCES assessors(id),
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    uses INTEGER NOT NULL DEFAULT 0,
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity TEXT,
    entity_id TEXT,
    details TEXT
);
"""

_lock = threading.RLock()
_conn = None


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def conn():
    global _conn
    with _lock:
        if _conn is None:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(DATA_DIR / 'registry.db', check_same_thread=False,
                                    isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.execute('PRAGMA journal_mode=WAL')
            _conn.execute('PRAGMA foreign_keys=ON')
            _conn.executescript(SCHEMA)
            _migrate(_conn)
        return _conn


MIGRATIONS = {
    # records entered by hand (e.g. transferred from the old Google Form) rather than run in the app
    'records': [("entry_method", "TEXT NOT NULL DEFAULT 'app'"),
                ('assessor_text', 'TEXT'),
                ('entered_by', 'INTEGER REFERENCES assessors(id)')],
    'outbox': [('cc', 'TEXT'), ('attachments', 'TEXT')],
    'people': [('unikey', 'TEXT')],
    'runs': [('signed_name', 'TEXT'), ('signature', 'TEXT'), ('outcome_label', 'TEXT')],
}


def _migrate(c):
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in c.execute(f'PRAGMA table_info({table})')}
        if not have:  # table not created yet
            continue
        for name, decl in cols:
            if name not in have:
                c.execute(f'ALTER TABLE {table} ADD COLUMN {name} {decl}')
    if {r[1] for r in c.execute('PRAGMA table_info(people)')} >= {'unikey'}:
        c.execute('CREATE UNIQUE INDEX IF NOT EXISTS people_unikey ON people(unikey) WHERE unikey IS NOT NULL')


def reset_for_tests():
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None


def q(sql, *args):
    return conn().execute(sql, args).fetchall()


def one(sql, *args):
    return conn().execute(sql, args).fetchone()


def run(sql, *args):
    with _lock:
        cur = conn().execute(sql, args)
        return cur.lastrowid


def audit(actor, action, entity=None, entity_id=None, **details):
    run('INSERT INTO audit_log (at, actor, action, entity, entity_id, details) VALUES (?,?,?,?,?,?)',
        now(), actor, action, entity, None if entity_id is None else str(entity_id),
        json.dumps(details) if details else None)


def loads(s, default=None):
    if s is None:
        return {} if default is None else default
    return json.loads(s)
