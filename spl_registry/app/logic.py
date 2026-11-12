"""Registry bundles, checklist progress, expiry and register status."""
import calendar
import json
from datetime import datetime, timedelta, timezone

from . import db

KINDS = ('check', 'demo', 'ack', 'info')
CHECKLIST_KINDS = ('check', 'ack', 'pass', 'num', 'select', 'text', 'rich', 'photo', 'info')
# Outcome keywords from early bundles, before pages named their own results
OLD_OUTCOMES = {'go-nogo': ['Go', 'No-go'], 'pass-fail': ['Pass', 'Fail'], 'complete': ['Complete']}


def outcome_words(chk):
    """A checklist's results: [when nothing failed, when anything failed], or [the only result]."""
    o = chk.get('outcome')
    if isinstance(o, str):
        return OLD_OUTCOMES.get(o, ['Complete'])
    return list(o) if o else ['Complete']


class BundleError(ValueError):
    pass


# ----------------------------------------------------------------- bundles
def validate_bundle(data):
    """Check a bundle. Format 1 (inductions.json) has inductions; format 2 (registry.json) has
    inductions and checklists. Returns (inductions, checklists)."""
    if not isinstance(data, dict) or data.get('format') not in (1, 2):
        raise BundleError('Not a registry bundle (expected "format": 1 or 2). Upload registry.json from the wiki.')
    inds = data.get('inductions') or []
    chks = data.get('checklists') or [] if data.get('format') == 2 else []
    if not isinstance(inds, list) or not isinstance(chks, list):
        raise BundleError('The bundle is malformed.')
    if not inds and not chks:
        raise BundleError('The bundle has no inductions or checklists.')
    seen = set()
    for kind, records, kinds, required in (
            ('induction', inds, KINDS, ('id', 'version', 'title', 'type', 'location', 'sections')),
            ('checklist', chks, CHECKLIST_KINDS, ('id', 'version', 'title', 'sections'))):
        for rec in records:
            for key in required:
                if key not in rec:
                    raise BundleError(f'A{"n" if kind == "induction" else ""} {kind} is missing "{key}".')
            if rec['id'] in seen:
                raise BundleError(f'Duplicate ID {rec["id"]}.')
            seen.add(rec['id'])
            for s in rec['sections']:
                for it in s.get('items', []):
                    if it.get('kind') not in kinds or 'id' not in it or 'text' not in it:
                        raise BundleError(f'{rec["id"]}: malformed checklist item.')
            if kind == 'checklist':
                o = rec.get('outcome')
                ok = (o is None or (isinstance(o, str) and o in OLD_OUTCOMES) or (isinstance(o, list) and 1 <= len(o) <= 2
                                                          and all(isinstance(w, str) and w.strip() for w in o)))
                if not ok:
                    raise BundleError(f'{rec["id"]}: outcome must be one or two words, not {o!r}.')
    return inds, chks


def import_bundle(raw_bytes, assessor_id):
    """Store a bundle and make its inductions and checklists the active ones.
    Returns (bundle id, new ids, changed ids, number of inductions, number of checklists)."""
    try:
        data = json.loads(raw_bytes.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise BundleError('The file is not valid JSON.')
    inds, chks = validate_bundle(data)
    bundle_id = db.run('INSERT INTO bundles (hash, generated_at, uploaded_by, uploaded_at, json) VALUES (?,?,?,?,?)',
                       data.get('hash', ''), data.get('generated_at'), assessor_id, db.now(), json.dumps(data))
    new, changed = [], []
    db.run('UPDATE inductions SET active = 0')
    for ind in inds:
        if db.one('SELECT 1 FROM inductions WHERE id = ? AND version = ?', ind['id'], ind['version']):
            db.run('UPDATE inductions SET active = 1 WHERE id = ? AND version = ?', ind['id'], ind['version'])
            continue
        had_any = db.one('SELECT 1 FROM inductions WHERE id = ?', ind['id'])
        (changed if had_any else new).append(ind['id'])
        db.run('INSERT INTO inductions (id, version, bundle_id, title, type, location, refresh_months, '
               'self_service, active, json) VALUES (?,?,?,?,?,?,?,?,1,?)',
               ind['id'], ind['version'], bundle_id, ind['title'], ind['type'], ind['location'],
               int(ind.get('refresh_months', 12)), 1 if ind.get('self_service') else 0, json.dumps(ind))
    db.run('UPDATE checklists SET active = 0')
    for chk in chks:
        if db.one('SELECT 1 FROM checklists WHERE id = ? AND version = ?', chk['id'], chk['version']):
            db.run('UPDATE checklists SET active = 1 WHERE id = ? AND version = ?', chk['id'], chk['version'])
            continue
        had_any = db.one('SELECT 1 FROM checklists WHERE id = ?', chk['id'])
        (changed if had_any else new).append(chk['id'])
        db.run('INSERT INTO checklists (id, version, bundle_id, title, category, location, outcome, active, json) '
               'VALUES (?,?,?,?,?,?,?,1,?)', chk['id'], chk['version'], bundle_id, chk['title'],
               chk.get('category') or '', chk.get('location') or '', ' / '.join(outcome_words(chk)), json.dumps(chk))
    return bundle_id, new, changed, len(inds), len(chks)


def counts(n_ind, n_chk):
    """'3 inductions', '3 inductions and 1 checklist'."""
    words = [f'{n_ind} induction{"" if n_ind == 1 else "s"}']
    if n_chk:
        words.append(f'{n_chk} checklist{"" if n_chk == 1 else "s"}')
    return ' and '.join(words)


def latest_bundle_id():
    """The bundle the app is using now; pages note it so they can tell when a newer one arrived."""
    return db.one('SELECT MAX(id) AS id FROM bundles')['id'] or 0


def active_record_ids():
    """IDs of every active induction and checklist (to spot ones a new bundle would retire)."""
    return ({r['id'] for r in db.q('SELECT id FROM inductions WHERE active = 1')}
            | {r['id'] for r in db.q('SELECT id FROM checklists WHERE active = 1')})


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
