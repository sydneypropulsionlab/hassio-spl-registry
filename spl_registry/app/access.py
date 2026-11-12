"""Who may use what: the rules behind the access API (used by the tap-card add-on).

A person may use a resource when they have a completed, unexpired, non-cancelled record for an
induction that grants it, and current records for every induction it `requires`. Expiry can be
extended by the access_grace_days option (default 0: access ends on the expiry date).
"""
import json
import re
from datetime import timedelta

from . import db, logic


def slug(s):
    return re.sub(r'[^a-z0-9]+', '-', str(s).lower()).strip('-')


def resources_of(ind):
    """[{id, name}] for an induction; older bundles only had grants_access names."""
    if ind.get('resources'):
        return ind['resources']
    return [{'id': slug(n), 'name': n} for n in logic.grants_access(ind)]


def catalogue():
    """Active inductions and every resource they grant."""
    inds = {row['id']: ind for row, ind in logic.active_inductions()}
    res = {}
    for ind in inds.values():
        for r in resources_of(ind):
            entry = res.setdefault(r['id'], {'id': r['id'], 'name': r['name'], 'locations': [], 'inductions': []})
            if ind['location'] not in entry['locations']:
                entry['locations'].append(ind['location'])
            entry['inductions'].append({'id': ind['id'], 'title': ind['title'], 'type': ind['type'],
                                        'requires': ind.get('requires', [])})
    return inds, res


def _latest_complete(person_id):
    latest = {}
    for r in db.q("SELECT * FROM records WHERE person_id = ? AND status = 'complete' "
                  'ORDER BY COALESCE(assessor_signed_at, created_at), id', person_id):
        latest[r['induction_id']] = r
    return latest


def _state(rec, grace, now):
    """('current'|'expired'|'not_inducted', expires_at, access_until) for one record."""
    if not rec:
        return 'not_inducted', None, None
    if not rec['expires_at']:
        return 'current', None, None
    exp = logic.parse(rec['expires_at'])
    until = exp + timedelta(days=grace)
    return ('current' if until > now else 'expired'), rec['expires_at'], until.isoformat(timespec='seconds')


def person_access(person, settings, inds=None, res=None, now=None):
    """Inductions (with status) and the resources this person may use right now."""
    if inds is None:
        inds, res = catalogue()
    now = now or logic.utcnow()
    grace = settings.access_grace_days
    latest = _latest_complete(person['id'])
    states = {iid: _state(latest.get(iid), grace, now) for iid in set(inds) | set(latest)}

    inductions = []
    for iid, ind in inds.items():
        st, exp, until = states[iid]
        missing = [r for r in ind.get('requires', []) if states.get(r, ('not_inducted',))[0] != 'current']
        inductions.append({'id': iid, 'title': ind['title'], 'type': ind['type'], 'location': ind['location'],
                           'status': st, 'expires_at': exp, 'record_id': latest[iid]['id'] if iid in latest else None,
                           'requires': ind.get('requires', []), 'requires_missing': missing,
                           'grants': [r['id'] for r in resources_of(ind)]})

    def limit(*dates):  # earliest expiry wins; None means no expiry
        ds = [d for d in dates if d]
        return min(ds) if ds else None

    allowed = {}
    for i in inductions:
        if i['status'] != 'current' or i['requires_missing']:
            continue
        exp = limit(i['expires_at'], *[states[r][1] for r in i['requires']])
        until = limit(states[i['id']][2], *[states[r][2] for r in i['requires']])
        for rid in i['grants']:
            best = allowed.get(rid)
            # keep whichever granting induction gives the longest access (None = no expiry)
            if best is None or (best['access_until'] is not None and (until is None or until > best['access_until'])):
                allowed[rid] = {'id': rid, 'name': res[rid]['name'], 'expires_at': exp, 'access_until': until,
                                'via': i['id']}
    return inductions, allowed


def check(unikey, resource_id, settings):
    """Decision for one person and one resource, with a reason a reader can show or log."""
    inds, res = catalogue()
    out = {'unikey': unikey, 'resource': resource_id, 'allowed': False, 'checked_at': db.now()}
    if resource_id not in res:
        return {**out, 'reason': 'unknown_resource', 'message': f'No induction grants access to "{resource_id}".'}
    out['resource_name'] = res[resource_id]['name']
    person = db.one('SELECT * FROM people WHERE unikey = ?', unikey)
    if not person:
        return {**out, 'reason': 'unknown_person', 'message': f'No inductions recorded for UniKey {unikey}.'}
    out['person'] = {'name': person['name'], 'unikey': person['unikey']}
    inductions, allowed = person_access(person, settings, inds, res)
    if resource_id in allowed:
        a = allowed[resource_id]
        ind = inds[a['via']]
        msg = f'{ind["title"]} current' + (f' until {a["access_until"][:10]}' if a['access_until'] else ' (no expiry)')
        return {**out, 'allowed': True, 'reason': 'current', 'message': msg,
                'induction': {'id': ind['id'], 'title': ind['title']},
                'expires_at': a['expires_at'], 'access_until': a['access_until']}
    # Not allowed: explain using the most relevant granting induction.
    granting = [i for i in inductions if resource_id in i['grants']]
    best = (next((i for i in granting if i['status'] == 'current'), None) or
            next((i for i in granting if i['status'] == 'expired'), None) or granting[0])
    base = {**out, 'induction': {'id': best['id'], 'title': best['title']}, 'expires_at': best['expires_at']}
    if best['status'] == 'current':
        names = ', '.join(inds[r]['title'] if r in inds else r for r in best['requires_missing'])
        return {**base, 'reason': 'prerequisite_missing', 'requires_missing': best['requires_missing'],
                'message': f'{best["title"]} is current, but {names} is missing or expired.'}
    if best['status'] == 'expired':
        return {**base, 'reason': 'expired', 'message': f'{best["title"]} expired on {best["expires_at"][:10]}.'}
    return {**base, 'reason': 'not_inducted', 'message': f'No current {best["title"]}.'}


def everyone(settings):
    """Every UniKey with at least one resource they may use: the list the card add-on caches."""
    inds, res = catalogue()
    people = []
    for p in db.q('SELECT * FROM people WHERE unikey IS NOT NULL ORDER BY unikey'):
        _, allowed = person_access(p, settings, inds, res)
        if allowed:
            people.append({'unikey': p['unikey'], 'name': p['name'],
                           'resources': [{k: a[k] for k in ('id', 'expires_at', 'access_until')}
                                         for a in sorted(allowed.values(), key=lambda a: a['id'])]})
    return {'grace_days': settings.access_grace_days, 'people': people}


def fingerprint(data):
    import hashlib
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:20]
