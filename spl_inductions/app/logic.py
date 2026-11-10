"""Induction bundles, checklist progress, expiry and register status."""
import calendar
import json
from datetime import datetime, timedelta, timezone

from . import db

KINDS = ('check', 'demo', 'ack', 'info')


class BundleError(ValueError):
    pass


# ----------------------------------------------------------------- bundles
def validate_bundle(data):
    if not isinstance(data, dict) or data.get('format') != 1:
        raise BundleError('Not an induction bundle (expected "format": 1). Upload output/inductions.json.')
    inds = data.get('inductions')
    if not isinstance(inds, list) or not inds:
        raise BundleError('The bundle has no inductions.')
    seen = set()
    for ind in inds:
        for key in ('id', 'version', 'title', 'type', 'location', 'sections'):
            if key not in ind:
                raise BundleError(f'An induction is missing "{key}".')
        if ind['id'] in seen:
            raise BundleError(f'Duplicate induction id {ind["id"]}.')
        seen.add(ind['id'])
        for s in ind['sections']:
            for it in s.get('items', []):
                if it.get('kind') not in KINDS or 'id' not in it or 'text' not in it:
                    raise BundleError(f'{ind["id"]}: malformed checklist item.')
    return inds


def import_bundle(raw_bytes, assessor_id):
    try:
        data = json.loads(raw_bytes.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise BundleError('The file is not valid JSON.')
    inds = validate_bundle(data)
    bundle_id = db.run('INSERT INTO bundles (hash, generated_at, uploaded_by, uploaded_at, json) VALUES (?,?,?,?,?)',
                       data.get('hash', ''), data.get('generated_at'), assessor_id, db.now(), json.dumps(data))
    db.run('UPDATE inductions SET active = 0')
    new, changed = [], []
    for ind in inds:
        existing = db.one('SELECT 1 FROM inductions WHERE id = ? AND version = ?', ind['id'], ind['version'])
        if existing:
            db.run('UPDATE inductions SET active = 1 WHERE id = ? AND version = ?', ind['id'], ind['version'])
            continue
        had_any = db.one('SELECT 1 FROM inductions WHERE id = ?', ind['id'])
        (changed if had_any else new).append(ind['id'])
        db.run('INSERT INTO inductions (id, version, bundle_id, title, type, location, refresh_months, '
               'self_service, active, json) VALUES (?,?,?,?,?,?,?,?,1,?)',
               ind['id'], ind['version'], bundle_id, ind['title'], ind['type'], ind['location'],
               int(ind.get('refresh_months', 12)), 1 if ind.get('self_service') else 0, json.dumps(ind))
    return bundle_id, new, changed, len(inds)


def induction(ind_id, version=None):
    if version:
        row = db.one('SELECT * FROM inductions WHERE id = ? AND version = ?', ind_id, version)
    else:
        row = db.one('SELECT * FROM inductions WHERE id = ? AND active = 1', ind_id)
    return (row, json.loads(row['json'])) if row else (None, None)


def active_inductions():
    return [(r, json.loads(r['json'])) for r in
            db.q('SELECT * FROM inductions WHERE active = 1 ORDER BY type, location, title')]


# ----------------------------------------------------------------- checklist
def items(ind):
    for s in ind['sections']:
        for it in s['items']:
            yield it


def ids_of(ind, kind):
    return [it['id'] for it in items(ind) if it['kind'] == kind]


def progress(ind, session_ticks, record):
    """Which required items are still outstanding for one participant."""
    demo = db.loads(record['demo_ticks'])
    ack = db.loads(record['ack_ticks'])
    missing = {
        'check': [i for i in ids_of(ind, 'check') if i not in session_ticks],
        'demo': [i for i in ids_of(ind, 'demo') if i not in demo],
        'ack': [i for i in ids_of(ind, 'ack') if i not in ack],
    }
    total = sum(len(ids_of(ind, k)) for k in ('check', 'demo', 'ack'))
    done = total - sum(len(v) for v in missing.values())
    return {'missing': missing, 'done': done, 'total': total,
            'complete': done == total}


# ----------------------------------------------------------------- dates
def parse(ts):
    return datetime.fromisoformat(ts) if ts else None


def add_months(d, months):
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    return d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))


def utcnow():
    return datetime.now(timezone.utc)


# ----------------------------------------------------------------- status
def sources_changed(record_snapshot, current_ind):
    """True when the SWP or RA behind an induction has changed since the record."""
    if not record_snapshot or not current_ind:
        return False
    old = {s['ref']: s.get('hash') for s in record_snapshot.get('sources', [])}
    new = {s['ref']: s.get('hash') for s in current_ind.get('sources', [])}
    return any(ref in new and new[ref] != h for ref, h in old.items())


def status(record, settings, current_ind=None):
    """(label, css class, list of flags) for a record."""
    st = record['status']
    flags = []
    if st in ('complete', 'awaiting_confirmation'):
        snap = db.loads(record['snapshot']) if record['snapshot'] else None
        if sources_changed(snap, current_ind):
            flags.append('SWP updated since induction')
    if st == 'complete':
        exp = parse(record['expires_at'])
        if exp is None:
            return 'Current', 'ok', flags
        n = utcnow()
        if exp <= n:
            return 'Expired', 'bad', flags
        if exp <= n + timedelta(days=settings.expiry_warning_days):
            return 'Expiring', 'warn', flags
        return 'Current', 'ok', flags
    if st == 'awaiting_confirmation':
        exp = parse(record['confirm_expires_at'])
        if exp and exp <= utcnow():
            return 'Confirmation link expired', 'bad', flags
        return 'Awaiting confirmation', 'warn', flags
    if st == 'inductee_signed':
        return 'Ready for sign-off', 'info', flags
    if st == 'joined':
        return 'In progress', 'info', flags
    if st == 'superseded':
        return 'Superseded', 'muted', flags
    return 'Cancelled', 'muted', flags


def grants_access(ind):
    """What an induction grants access to (older bundles called it room_access)."""
    if not ind:
        return []
    return list(ind.get('grants_access') or ind.get('room_access') or [])
