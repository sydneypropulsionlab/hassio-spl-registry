"""Pull the registry bundle (inductions and checklists) from the SPL wiki.

The wiki's splregistry plugin publishes the bundle (built from approved induction pages) as a
media file. This module downloads it on a schedule and imports it when its hash changes.

An automatic pull never retires inductions on its own: if a new bundle drops an induction that is
currently active (an induction or checklist; for example someone removed a page's tag), the bundle is held until an assessor
confirms it on the Wiki and bundles page.
"""
import base64
import json
import threading
import time
import urllib.error
import urllib.request

from . import db, logic
from .config import DATA_DIR

STATE_FILE = DATA_DIR / 'wiki_sync.json'
PENDING_FILE = DATA_DIR / 'wiki_sync_pending.json'
_lock = threading.Lock()


def enabled(settings):
    return bool(settings.bundle_url)


def state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(**changes):
    s = state()
    s.update(changes)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(s, indent=2))
    return s


def pending():
    """The held bundle, if any: {'raw': str, 'hash': ..., 'retired': [...]}."""
    try:
        return json.loads(PENDING_FILE.read_text())
    except (OSError, ValueError):
        return None


def _clear_pending():
    try:
        PENDING_FILE.unlink()
    except FileNotFoundError:
        pass


def fetch(settings, etag=None):
    """Download the bundle. Returns (raw bytes or None when unchanged, etag)."""
    req = urllib.request.Request(settings.bundle_url, headers={'Accept': 'application/json',
                                                               'User-Agent': 'spl-registry'})
    if settings.bundle_token:
        req.add_header('Authorization', f'Bearer {settings.bundle_token}')
    elif settings.bundle_username:
        cred = f'{settings.bundle_username}:{settings.bundle_password}'.encode()
        req.add_header('Authorization', 'Basic ' + base64.b64encode(cred).decode())
    if etag:
        req.add_header('If-None-Match', etag)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            ctype = r.headers.get('Content-Type', '')
            raw = r.read()
            if 'html' in ctype.lower():
                raise RuntimeError('The wiki returned a web page instead of the bundle. Check the URL, and the '
                                   'login: the wiki answers with its login page when it does not accept it.')
            return raw, r.headers.get('ETag')
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None, etag
        if e.code in (401, 403):
            raise RuntimeError(f'The wiki refused access ({e.code}). Check bundle_username and bundle_password '
                               '(or bundle_token), and that this account can read the bundle.') from None
        if e.code == 404:
            raise RuntimeError('No bundle at bundle_url (404). It appears once an induction page has been '
                               'approved on the wiki.') from None
        raise RuntimeError(f'The wiki answered {e.code} {e.reason}.') from None
    except urllib.error.URLError as e:
        raise RuntimeError(f'Could not reach the wiki: {e.reason}') from None


def _latest_hash():
    row = db.one('SELECT hash FROM bundles ORDER BY id DESC LIMIT 1')
    return row['hash'] if row else None


def _import(raw, actor_id, actor_name, source):
    bid, new, changed, n_ind, n_chk = logic.import_bundle(raw, actor_id)
    db.audit(actor_name, 'bundle_synced', 'bundle', bid, new=new, changed=changed, inductions=n_ind,
             checklists=n_chk, source=source)
    return bid, new, changed, n_ind, n_chk


def check(settings, actor_id=None, actor_name='wiki sync', force=False):
    """Fetch and, if the bundle changed, import it. Returns a result dict for the UI and the log."""
    if not enabled(settings):
        return {'status': 'off', 'message': 'Wiki sync is off (no bundle_url set).'}
    with _lock:
        st = state()
        now = db.now()
        try:
            raw, etag = fetch(settings, None if force else st.get('etag'))
        except RuntimeError as e:
            _save_state(checked_at=now, status='error', message=str(e))
            return {'status': 'error', 'message': str(e)}
        if raw is None:
            s = _save_state(checked_at=now, status='unchanged', message='No change on the wiki.')
            return {'status': 'unchanged', 'message': s['message']}
        try:
            data = json.loads(raw.decode('utf-8'))
            inds, chks = logic.validate_bundle(data)
        except (UnicodeDecodeError, ValueError) as e:
            msg = f'The wiki bundle is not valid: {e}'
            _save_state(checked_at=now, status='error', message=msg)
            return {'status': 'error', 'message': msg}
        h = data.get('hash', '')
        if h and h == _latest_hash():
            _clear_pending()
            s = _save_state(checked_at=now, etag=etag, status='unchanged', remote_hash=h,
                            message=f'Up to date with the wiki (bundle {h}).')
            return {'status': 'unchanged', 'message': s['message']}
        retired = sorted(logic.active_record_ids() - {r['id'] for r in inds + chks})
        if retired:
            PENDING_FILE.write_text(json.dumps({'raw': raw.decode('utf-8'), 'hash': h, 'retired': retired,
                                                'generated_at': data.get('generated_at'), 'held_at': now}))
            msg = (f'Bundle {h} from the wiki would retire {", ".join(retired)}. It is held until an assessor '
                   'confirms it on the Wiki and bundles page.')
            _save_state(checked_at=now, etag=etag, status='held', remote_hash=h, message=msg)
            return {'status': 'held', 'message': msg, 'retired': retired}
        bid, new, changed, n_ind, n_chk = _import(raw, actor_id, actor_name, 'wiki')
        _clear_pending()
        msg = (f'Imported bundle {h} from the wiki: {logic.counts(n_ind, n_chk)} '
               f'({len(new)} new, {len(changed)} updated).')
        _save_state(checked_at=now, etag=etag, status='imported', remote_hash=h, message=msg, imported_at=now)
        return {'status': 'imported', 'message': msg, 'new': new, 'changed': changed}


def confirm_pending(actor_id, actor_name):
    """Import the held bundle, retiring the inductions it drops."""
    with _lock:
        p = pending()
        if not p:
            return {'status': 'error', 'message': 'There is no held bundle to confirm.'}
        bid, new, changed, n_ind, n_chk = _import(p['raw'].encode('utf-8'), actor_id, actor_name, 'wiki (confirmed)')
        db.audit(actor_name, 'inductions_retired', 'bundle', bid, retired=p['retired'])
        _clear_pending()
        now = db.now()
        msg = (f'Imported bundle {p["hash"]} from the wiki: {logic.counts(n_ind, n_chk)}. '
               f'Retired: {", ".join(p["retired"])}.')
        _save_state(checked_at=now, status='imported', message=msg, imported_at=now)
        return {'status': 'imported', 'message': msg}


def start_background(settings, log=print):
    """Poll the wiki every bundle_poll_minutes in a daemon thread."""
    if not enabled(settings) or settings.bundle_poll_minutes <= 0:
        return None

    def loop():
        time.sleep(5)
        while True:
            try:
                r = check(settings)
                if r['status'] in ('imported', 'held', 'error'):
                    log(f'wiki sync: {r["message"]}')
            except Exception as e:  # never let the thread die
                log(f'wiki sync failed: {e!r}')
            time.sleep(settings.bundle_poll_minutes * 60)

    t = threading.Thread(target=loop, name='wiki-sync', daemon=True)
    t.start()
    return t
