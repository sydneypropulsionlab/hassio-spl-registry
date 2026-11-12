"""SPL Registry: inductions and checklists, with the records they produce.

Inductions: assessors log in, start a session for an induction, and show its QR code.
Inductees scan it, enter their details and tick their acknowledgements on their own phone
while the assessor ticks covered and demonstrated items. Both sign, the inductee confirms
by email, and the record is locked.

Checklists (preflights, QC checks, inspections): someone fills one in, with photos and
comments, and submits it. The submitted record never changes; corrections are amendments.
"""
import csv
import hashlib
import io
import json
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path
from time import monotonic
from zoneinfo import ZoneInfo

import qrcode
import qrcode.image.svg
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from starlette.background import BackgroundTask
from starlette.middleware.sessions import SessionMiddleware

from . import access, api, backup, certificate, checklists, db, diff, logic, mailer, security, wikisync
from .config import load_settings

HERE = Path(__file__).resolve().parent
settings = load_settings()


@asynccontextmanager
async def lifespan(_app):
    wikisync.start_background(settings)  # polls the wiki for new induction bundles, when bundle_url is set
    yield


app = FastAPI(title='SPL Registry', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=security.secret_key(), session_cookie='spl_reg',
                   max_age=12 * 3600, same_site='lax')
app.mount('/static', StaticFiles(directory=HERE / 'static'), name='static')
app.state.settings = settings



@app.middleware('http')
async def ingress_to_base_url(request, call_next):
    """Home Assistant's "Open Web UI" button and sidebar panel arrive through ingress (marked with the
    X-Ingress-Path header). The app itself lives at base_url, so send the browser there."""
    if request.headers.get('x-ingress-path') is not None:
        target = settings.base_url + '/'
        safe = target.replace('&', '&amp;').replace('"', '&quot;').replace('<', '&lt;')
        return HTMLResponse(f"""<!doctype html><meta charset="utf-8"><title>SPL Registry</title>
<body style="font:16px system-ui,sans-serif;padding:2rem;color:#1a1a1a">
<p>Opening SPL Registry at <a href="{safe}" target="_top">{safe}</a>…</p>
<script>window.top.location.href = {json.dumps(target)};</script></body>""")
    return await call_next(request)
app.include_router(api.router)
templates = Jinja2Templates(directory=HERE / 'templates')
# Fingerprint of the static files, added to their URLs so browsers and proxies fetch new copies after an update.
ASSET_VERSION = hashlib.sha256(b''.join(p.read_bytes() for p in sorted((HERE / 'static').iterdir())
                                        if p.is_file())).hexdigest()[:10]
templates.env.globals['asset_v'] = ASSET_VERSION

join_limiter = security.RateLimiter(limit=20, window=600)
login_limiter = security.RateLimiter(limit=10, window=600)
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
UNIKEY_RE = re.compile(r'^[a-z]{2,8}[0-9]{1,6}$')  # usually 4 letters then 4 digits, e.g. abcd1234
UNIKEY_HELP = 'Your University of Sydney UniKey, usually 4 letters then 4 digits (e.g. abcd1234).'
ROLES = ['Staff', 'Student', 'Researcher', 'Visitor', 'Contractor']


# ----------------------------------------------------------------- helpers
def local(ts, fmt='%d %b %Y %H:%M'):
    if not ts:
        return ''
    return logic.parse(ts).astimezone(ZoneInfo(settings.timezone)).strftime(fmt)


URL_RE = re.compile(r'https?://[^\s<>"]+[^\s<>".,;:!?)]')


def linkify(text):
    """Escape item text and turn web addresses into links that open in a new tab."""
    out, last = [], 0
    for m in URL_RE.finditer(text or ''):
        url = m.group(0)
        out += [escape(text[last:m.start()]),
                Markup('<a href="{0}" target="_blank" rel="noopener noreferrer">{0}</a>').format(url)]
        last = m.end()
    out.append(escape((text or '')[last:]))
    return Markup('').join(out)


templates.env.filters['local'] = local
templates.env.filters['linkify'] = linkify
templates.env.globals['grants_access'] = logic.grants_access
templates.env.globals['latest_bundle_id'] = logic.latest_bundle_id
templates.env.globals['outcome_words'] = logic.outcome_words
templates.env.filters['date'] = lambda ts: local(ts, '%d %b %Y')


def render(request, _template, status_code=200, **ctx):
    ctx.setdefault('me', current_assessor(request))
    ctx.setdefault('csrf', csrf_token(request))
    ctx['settings'] = settings
    ctx.setdefault('flash', request.session.pop('flash', None))
    return templates.TemplateResponse(request, _template, ctx, status_code=status_code)


def message(request, title, text, status_code=200, link=None):
    return render(request, 'message.html', status_code=status_code, title=title, text=text, link=link)


def current_assessor(request):
    aid = request.session.get('assessor_id')
    if not aid:
        return None
    return db.one('SELECT * FROM assessors WHERE id = ? AND active = 1', aid)


def csrf_token(request):
    tok = request.session.get('csrf')
    if not tok:
        tok = security.new_token()
        request.session['csrf'] = tok
    return tok


class Redirect(Exception):
    def __init__(self, url):
        self.url = url


@app.exception_handler(Redirect)
async def _redirect(request, exc):
    return RedirectResponse(exc.url, status_code=303)


def require(request, csrf=None):
    me = current_assessor(request)
    if not me:
        raise Redirect('/login')
    if csrf is not None and csrf != request.session.get('csrf'):
        raise HTTPException(400, 'Form expired. Go back, reload the page and try again.')
    return me


def actor(me):
    return f'assessor:{me["name"]}'


def qr_svg(url):
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    return img.to_string(encoding='unicode')


def session_row(sid):
    s = db.one('SELECT * FROM sessions WHERE id = ?', sid)
    if not s:
        raise HTTPException(404, 'Session not found')
    return s


def session_open_for_joining(s):
    return not s['closed_at'] and logic.parse(s['join_until']) > logic.utcnow()


def participants(sid):
    return db.q('SELECT r.*, p.name AS person_name, p.email AS person_email, p.unikey AS person_unikey, p.role AS person_role '
                'FROM records r JOIN people p ON p.id = r.person_id WHERE r.session_id = ? '
                "AND r.status != 'cancelled' ORDER BY r.id", sid)


def record_with_person(rid):
    r = db.one('SELECT r.*, p.name AS person_name, p.email AS person_email, p.unikey AS person_unikey, p.role AS person_role, '
               'COALESCE(a.name, r.assessor_text) AS assessor_name, e.name AS entered_by_name '
               'FROM records r JOIN people p ON p.id = r.person_id '
               'LEFT JOIN assessors a ON a.id = r.assessor_id LEFT JOIN assessors e ON e.id = r.entered_by '
               'WHERE r.id = ?', rid)
    if not r:
        raise HTTPException(404, 'Record not found')
    return r


def send_confirmation(record, ind, me):
    token = security.new_token()
    expires = logic.utcnow() + timedelta(days=settings.confirm_days)
    db.run('UPDATE records SET confirm_token_hash = ?, confirm_sent_at = ?, confirm_expires_at = ? WHERE id = ?',
           security.token_hash(token), db.now(), expires.isoformat(timespec='seconds'), record['id'])
    link = f'{settings.base_url}/c/{token}'
    body = (f'Hi {record["person_name"]},\n\n'
            f'{me["name"]} has signed off your {ind["title"]} ({ind["location"]}).\n\n'
            f'Please confirm your induction record by opening this link within {settings.confirm_days} days:\n\n'
            f'{link}\n\n'
            'Your induction is not complete until you confirm it. If you were not inducted, '
            'do not click the link and let SPL staff know.\n\n'
            'Sydney Propulsion Lab, University of Sydney')
    status = mailer.send(settings, record['person_email'], f'Confirm your induction: {ind["title"]}', body)
    db.audit(actor(me), 'confirmation_sent', 'record', record['id'], to=record['person_email'], status=status)
    return status


# ----------------------------------------------------------------- public: home, setup, login
@app.get('/', response_class=HTMLResponse)
def home(request: Request):
    if current_assessor(request):
        return RedirectResponse('/dashboard', status_code=303)
    if not db.one('SELECT 1 FROM assessors'):
        return RedirectResponse('/setup', status_code=303)
    return render(request, 'home.html')


@app.get('/setup', response_class=HTMLResponse)
def setup_form(request: Request):
    if db.one('SELECT 1 FROM assessors'):
        return RedirectResponse('/login', status_code=303)
    return render(request, 'setup.html')


@app.post('/setup')
def setup(request: Request, name: str = Form(...), email: str = Form(...),
          password: str = Form(...), csrf: str = Form(...)):
    if db.one('SELECT 1 FROM assessors'):
        return RedirectResponse('/login', status_code=303)
    if csrf != request.session.get('csrf'):
        raise HTTPException(400, 'Form expired. Reload and try again.')
    if len(password) < 10 or not EMAIL_RE.match(email):
        return render(request, 'setup.html', error='Enter a valid email and a password of at least 10 characters.',
                      status_code=400)
    aid = db.run('INSERT INTO assessors (name, email, pw_hash, created_at) VALUES (?,?,?,?)',
                 name.strip(), email.strip().lower(), security.hash_password(password), db.now())
    db.audit(f'assessor:{name.strip()}', 'first_assessor_created', 'assessor', aid)
    request.session['assessor_id'] = aid
    return RedirectResponse('/dashboard', status_code=303)


@app.post('/setup/restore')
async def setup_restore_upload(request: Request, file: UploadFile, csrf: str = Form(...)):
    """On a fresh install (no assessors yet), restore a backup instead of creating the first account."""
    if db.one('SELECT 1 FROM assessors'):
        return RedirectResponse('/login', status_code=303)
    if csrf != request.session.get('csrf'):
        return render(request, 'setup.html', restore_error='Form expired. Try again.', status_code=400)
    try:
        path = backup.save_upload(file.file)
        try:
            incoming = backup.validate(path)
        except backup.BackupError:
            path.unlink(missing_ok=True)
            raise
    except backup.BackupError as e:
        return render(request, 'setup.html', restore_error=str(e), status_code=400)
    request.session['pending_restore'] = path.name
    return render(request, 'backup_confirm.html', incoming=incoming, source=file.filename or 'uploaded file',
                  backup_name=path.name, setup=True)


@app.post('/setup/restore/confirm')
def setup_restore(request: Request, name: str = Form(...), confirm: str = Form(''), csrf: str = Form(...)):
    if db.one('SELECT 1 FROM assessors'):
        return RedirectResponse('/login', status_code=303)
    if csrf != request.session.get('csrf') or name != request.session.get('pending_restore') or confirm != 'RESTORE':
        return render(request, 'setup.html', restore_error='Restore not confirmed. Upload the backup again and '
                      'type RESTORE to confirm.', status_code=400)
    try:
        path = backup.backup_path(name)
        backup.restore(path)
    except backup.BackupError as e:
        return render(request, 'setup.html', restore_error=str(e), status_code=400)
    path.unlink(missing_ok=True)
    request.session.clear()
    db.audit('setup', 'database_restored_at_setup', 'backup', name)
    n = db.one('SELECT COUNT(*) FROM assessors WHERE active = 1')[0]
    return message(request, 'Backup restored',
                   f'Everything from the backup is back, including {n} assessor account{"s" if n != 1 else ""}. '
                   'Log in with your usual email and password.', link='/login')


@app.get('/login', response_class=HTMLResponse)
def login_form(request: Request):
    return render(request, 'login.html')


@app.post('/login')
def login(request: Request, email: str = Form(...), password: str = Form(...), csrf: str = Form(...)):
    ip = request.client.host if request.client else '?'
    if not login_limiter.allow(ip):
        return render(request, 'login.html', error='Too many attempts. Wait ten minutes and try again.',
                      status_code=429)
    if csrf != request.session.get('csrf'):
        return render(request, 'login.html', error='Form expired. Try again.', status_code=400)
    a = db.one('SELECT * FROM assessors WHERE email = ? AND active = 1', email.strip().lower())
    if not a or not security.verify_password(password, a['pw_hash']):
        return render(request, 'login.html', error='Email or password is incorrect.', status_code=401)
    request.session.clear()
    request.session['assessor_id'] = a['id']
    db.audit(actor(a), 'login', 'assessor', a['id'])
    return RedirectResponse('/dashboard', status_code=303)


@app.post('/logout')
def logout(request: Request):
    request.session.clear()
    return RedirectResponse('/', status_code=303)


# ----------------------------------------------------------------- assessor: dashboard
@app.get('/dashboard', response_class=HTMLResponse)
def dashboard(request: Request):
    me = require(request)
    now = db.now()
    open_sessions = db.q("SELECT s.*, i.title FROM sessions s JOIN inductions i ON i.id = s.induction_id "
                         "AND i.version = s.induction_version WHERE s.closed_at IS NULL ORDER BY s.id DESC")
    awaiting = db.q("SELECT r.*, p.name AS person_name, i.title FROM records r JOIN people p ON p.id = r.person_id "
                    "JOIN inductions i ON i.id = r.induction_id AND i.version = r.induction_version "
                    "WHERE r.status IN ('awaiting_confirmation', 'inductee_signed') ORDER BY r.id DESC LIMIT 50")
    soon = (logic.utcnow() + timedelta(days=settings.expiry_warning_days)).isoformat(timespec='seconds')
    # a person's latest completed record for each induction (an older one that was refreshed doesn't count)
    latest = ("r.status = 'complete' AND NOT EXISTS (SELECT 1 FROM records n WHERE n.person_id = r.person_id "
              "AND n.induction_id = r.induction_id AND n.status = 'complete' AND n.id > r.id)")
    expiring = db.q("SELECT r.*, p.name AS person_name, i.title FROM records r JOIN people p ON p.id = r.person_id "
                    "JOIN inductions i ON i.id = r.induction_id AND i.version = r.induction_version "
                    f"WHERE {latest} AND r.expires_at IS NOT NULL AND r.expires_at <= ? "
                    "ORDER BY r.expires_at LIMIT 100", soon)
    expired = [r for r in expiring if r['expires_at'] <= now]
    expiring = [r for r in expiring if r['expires_at'] > now]
    stats = {
        'current': db.one(f"SELECT COUNT(*) AS n FROM records r WHERE {latest} "
                          "AND (r.expires_at IS NULL OR r.expires_at > ?)", now)['n'],
        'people': db.one("SELECT COUNT(DISTINCT person_id) AS n FROM records WHERE status = 'complete'")['n'],
        'inductions': db.one('SELECT COUNT(*) AS n FROM inductions WHERE active = 1')['n'],
        'month': db.one("SELECT COUNT(*) AS n FROM records WHERE status = 'complete' "
                        "AND COALESCE(confirmed_at, assessor_signed_at, created_at) >= ?",
                        (logic.utcnow() - timedelta(days=30)).isoformat(timespec='seconds'))['n'],
    }
    recent_runs = db.q(f"{RUN_LIST_SQL} WHERE r.status = 'submitted' ORDER BY r.submitted_at DESC LIMIT 8")
    return render(request, 'dashboard.html', me=me, open_sessions=open_sessions, awaiting=awaiting,
                  recent_runs=recent_runs, outcome_label=checklists.outcome_label,
                  expired=expired, expiring=expiring, stats=stats, now=now,
                  wiki_enabled=wikisync.enabled(settings), wiki_state=wikisync.state(),
                  wiki_pending=wikisync.pending())


# ----------------------------------------------------------------- assessor: inductions and bundles
def query_string(request):
    return ('?' + request.url.query) if request.url.query else ''


@app.get('/inductions')
def inductions_redirect(request: Request):
    return RedirectResponse('/registry', status_code=303)


@app.get('/checklists')
def checklists_redirect(request: Request):
    q = request.url.query
    return RedirectResponse('/registry?view=checklists' + (('&' + q) if q else '') + '#records', status_code=303)


@app.get('/register')
def register_redirect(request: Request):
    return RedirectResponse('/registry' + query_string(request), status_code=303)


@app.get('/wiki', response_class=HTMLResponse)
def wiki_page(request: Request):
    me = require(request)
    bundles = db.q('SELECT b.id, b.hash, b.generated_at, b.uploaded_at, a.name AS by_name FROM bundles b '
                   'LEFT JOIN assessors a ON a.id = b.uploaded_by ORDER BY b.id DESC LIMIT 20')
    n_ind = len(logic.active_inductions())
    n_chk = len(checklists.active_checklists())
    return render(request, 'wiki.html', me=me, bundles=bundles, counts=logic.counts(n_ind, n_chk),
                  result=request.session.pop('bundle_result', None), wiki_enabled=wikisync.enabled(settings),
                  wiki_url=settings.bundle_url, wiki_state=wikisync.state(), wiki_pending=wikisync.pending(),
                  wiki_poll=settings.bundle_poll_minutes)


@app.get('/registry', response_class=HTMLResponse)
def registry_page(request: Request, view: str = 'inductions', location: str = '', type: str = '',
                  category: str = '', checklist: str = '', outcome: str = '', q: str = '', induction: str = '',
                  status: str = ''):
    """Everything in one place: start an induction or checklist, drafts, and the records."""
    me = require(request)
    view = 'checklists' if view == 'checklists' else 'inductions'
    active_inds = logic.active_inductions()
    active_chks = checklists.active_checklists()
    # inductions first in each location (spaces before equipment), then checklists
    start = ([{'rtype': 'induction', 'row': r, 'rec': i, 'location': r['location'] or '',
               'sort': (0, r['type'] != 'space', i['title'].lower())} for r, i in active_inds]
             + [{'rtype': 'checklist', 'row': r, 'rec': c, 'location': r['location'] or '',
                 'sort': (1, (r['category'] or '').lower(), c['title'].lower())} for r, c in active_chks])
    start.sort(key=lambda x: (x['location'] == '', x['location'].lower(), x['sort']))
    groups = []   # by location, in that order; records with no location last
    for s_ in start:
        if not groups or groups[-1][0] != s_['location']:
            groups.append((s_['location'], []))
        groups[-1][1].append(s_)
    start_categories = checklists.categories([{'category': r['type']} for r, _ in active_inds]
                                             + [{'category': r['category']} for r, _ in active_chks])
    drafts = db.q(f"{RUN_LIST_SQL} WHERE r.status = 'draft' ORDER BY r.id DESC")
    ctx = dict(me=me, view=view, start=start, groups=groups, start_categories=start_categories,
               wiki_enabled=wikisync.enabled(settings), wiki_pending=wikisync.pending(),
               has_both=bool(active_inds) and bool(active_chks), drafts=drafts)
    if view == 'checklists':
        where, args = run_filters(category, checklist, outcome, q)
        every = db.q('SELECT id, title, category FROM checklists GROUP BY id ORDER BY title')
        ctx.update(recent=db.q(f'{RUN_LIST_SQL} WHERE {where} ORDER BY r.submitted_at DESC LIMIT 200', *args),
                   categories=checklists.categories(every), all_checklists=every, category=category,
                   checklist=checklist, outcome=outcome, outcomes=checklists.OUTCOME_FILTERS, q=q,
                   outcome_label=checklists.outcome_label)
    else:
        every = db.q('SELECT id, title, type, location FROM inductions GROUP BY id ORDER BY title')
        ctx.update(rows=induction_records(q, induction, location, category or type, status), q=q,
                   induction=induction, location=location, category=category or type, status=status,
                   all_inductions=every, locations=sorted({r['location'] for r in every if r['location']}),
                   ind_categories=sorted({(r['type'] or '').lower() for r in every} - {''}),
                   statuses=INDUCTION_STATUS_FILTERS)
    return render(request, 'registry.html', **ctx)


@app.post('/inductions/sync')
def sync_bundle(request: Request, csrf: str = Form(...), next: str = Form('/wiki')):
    """Update Registry: fetch the wiki's bundle now (imports it unless it would retire something)."""
    me = require(request, csrf)
    r = wikisync.check(settings, me['id'], actor(me), force=True)
    if next in ('/registry', '/dashboard'):
        request.session['flash'] = r['message']
        return RedirectResponse(next, status_code=303)
    request.session['bundle_result'] = {'error': r['message']} if r['status'] in ('error', 'off') else {'ok': r['message']}
    return RedirectResponse('/wiki', status_code=303)


_last_page_check = [0.0]
PAGE_CHECK_SECONDS = 15


@app.post('/wiki/check')
def wiki_check(request: Request):
    """Called by the Dashboard, Registry and Wiki pages as they load: imports a new bundle from the wiki
    if it only adds or changes things; one that would retire something is held and reported."""
    me = require(request, request.headers.get('x-csrf-token', ''))
    if not wikisync.enabled(settings):
        return JSONResponse({'status': 'off', 'bundle': logic.latest_bundle_id()})
    now = monotonic()
    if now - _last_page_check[0] >= PAGE_CHECK_SECONDS:
        _last_page_check[0] = now
        r = wikisync.check(settings, me['id'], actor(me))
    else:
        r = {'status': 'recent', 'message': ''}
    if r['status'] == 'imported':
        request.session['flash'] = 'Registry updated from the wiki. ' + r['message']
    out = {'status': r['status'], 'message': r.get('message', ''), 'bundle': logic.latest_bundle_id()}
    p = wikisync.pending()
    if p:
        out['held'] = {'retired': p['retired'], 'hash': p.get('hash', '')}
    return JSONResponse(out)


@app.post('/inductions/sync/confirm')
def sync_confirm(request: Request, csrf: str = Form(...)):
    me = require(request, csrf)
    r = wikisync.confirm_pending(me['id'], actor(me))
    request.session['bundle_result'] = {'error': r['message']} if r['status'] == 'error' else {'ok': r['message']}
    return RedirectResponse('/wiki', status_code=303)


@app.post('/inductions/upload')
async def upload_bundle(request: Request, file: UploadFile, csrf: str = Form(...)):
    me = require(request, csrf)
    raw = await file.read()
    try:
        bid, new, changed, n_ind, n_chk = logic.import_bundle(raw, me['id'])
    except logic.BundleError as e:
        request.session['bundle_result'] = {'error': str(e)}
        return RedirectResponse('/wiki', status_code=303)
    db.audit(actor(me), 'bundle_uploaded', 'bundle', bid, new=new, changed=changed, inductions=n_ind,
             checklists=n_chk)
    request.session['bundle_result'] = {'ok': f'Bundle loaded: {logic.counts(n_ind, n_chk)} '
                                               f'({len(new)} new, {len(changed)} updated).'}
    return RedirectResponse('/wiki', status_code=303)


@app.get('/inductions/{ind_id}', response_class=HTMLResponse)
def induction_preview(request: Request, ind_id: str):
    me = require(request)
    row, ind = logic.induction(ind_id)
    if not row:
        raise HTTPException(404, 'Induction not found')
    return render(request, 'induction_preview.html', me=me, ind=ind)


# ----------------------------------------------------------------- assessor: checklists
def run_or_404(rid):
    r = checklists.run_row(rid)
    if not r:
        raise HTTPException(404, 'Checklist record not found')
    return r


RUN_LIST_SQL = ('SELECT r.*, c.title, c.category FROM runs r JOIN checklists c ON c.id = r.checklist_id '
                'AND c.version = r.checklist_version')


def run_filters(category, checklist, outcome, q=''):
    where, args = ["r.status = 'submitted'"], []
    if q.strip():
        where.append("(LOWER(c.title) LIKE ? OR LOWER(COALESCE(r.submitted_by, '')) LIKE ? "
                     "OR LOWER(COALESCE(r.signed_name, '')) LIKE ?)")
        args += [f'%{q.strip().lower()}%'] * 3
    if category:
        where.append('LOWER(c.category) = ?')
        args.append(category.lower())
    if checklist:
        where.append('r.checklist_id = ?')
        args.append(checklist)
    if outcome:
        where.append('r.outcome = ?')
        args.append(outcome)
    return ' AND '.join(where), args


@app.post('/checklists/{chk_id}/start')
def start_run(request: Request, chk_id: str, csrf: str = Form(...)):
    me = require(request, csrf)
    row, _ = checklists.checklist(chk_id)
    if not row:
        raise HTTPException(404, 'Checklist not found')
    rid = db.run('INSERT INTO runs (checklist_id, checklist_version, assessor_id, started_at) VALUES (?,?,?,?)',
                 row['id'], row['version'], me['id'], db.now())
    db.audit(actor(me), 'checklist_started', 'run', rid, checklist=row['id'], version=row['version'])
    return RedirectResponse(f'/records/checklist/{rid}', status_code=303)


def render_run(request, me, r, errors=None, photo_errors=None, status_code=200):
    _, chk = checklists.checklist(r['checklist_id'], r['checklist_version'])
    answers = db.loads(r['answers'])
    tpl = 'run_form.html' if r['status'] == 'draft' else 'run.html'
    started_by = db.one('SELECT name FROM assessors WHERE id = ?', r['assessor_id']) if r['assessor_id'] else None
    history = [(h, db.loads(h['details'])) for h in
               db.q("SELECT * FROM audit_log WHERE entity = 'run' AND entity_id = ? ORDER BY id", str(r['id']))]
    return render(request, tpl, status_code=status_code, me=me, r=r, chk=chk, answers=answers,
                  photos=checklists.photos(r['id']), errors=errors or {}, photo_errors=photo_errors or [],
                  amendments=json.loads(r['amendments'] or '[]'), limits_text=checklists.limits_text,
                  can_fail=checklists.can_fail, fmt_num=checklists.fmt_num,
                  outcome_label=checklists.outcome_label, started_by=started_by['name'] if started_by else '',
                  history=history,
                  failed=checklists.failed_items(chk, answers) if r['status'] != 'draft' else [])


@app.get('/records/checklist/{rid}', response_class=HTMLResponse)
def run_page(request: Request, rid: int):
    me = require(request)
    return render_run(request, me, run_or_404(rid))


@app.post('/records/checklist/{rid}')
async def run_save(request: Request, rid: int):
    """Save a draft (answers and any photos chosen), delete a photo, or submit."""
    form = await request.form()
    me = require(request, form.get('csrf', ''))
    r = run_or_404(rid)
    if r['status'] != 'draft':
        raise HTTPException(400, 'This checklist has been submitted; add an amendment instead.')
    _, chk = checklists.checklist(r['checklist_id'], r['checklist_version'])
    action = form.get('action', 'save')
    answers = checklists.read_answers(chk, form)
    comments = checklists.clean_rich(form.get('comments', ''))[:checklists.RICH_MAX]
    signature = form.get('signature', '') if chk.get('signature_required') else ''
    if not signature.startswith('data:image/png;base64,') or len(signature) > 400000:
        signature = ''
    signed_name = ' '.join(form.get('signed_name', '').split())[:120]
    db.run('UPDATE runs SET answers = ?, comments = ?, signature = ?, signed_name = ? WHERE id = ?',
           json.dumps(answers), comments, signature or None, signed_name or None, rid)

    # photos chosen in the form: photo_<item id>, or photo__run for the checklist as a whole
    item_ids = {it['id'] for it in checklists.answerable(chk)}
    photo_errors = []
    for key in form.keys():
        if not key.startswith('photo_'):
            continue
        item = key[len('photo_'):]
        item = None if item == '_run' else item
        if item is not None and item not in item_ids:
            continue
        for f in form.getlist(key):
            if not getattr(f, 'filename', None):
                continue
            raw = await f.read()
            try:
                aid = checklists.add_photo(rid, item, raw, f.filename, me['name'])
                db.audit(actor(me), 'photo_added', 'run', rid, item=item, attachment=aid)
            except checklists.PhotoError as e:
                photo_errors.append(str(e))

    if action.startswith('delete:'):
        aid = action.split(':', 1)[1]
        if aid.isdigit() and db.one('SELECT 1 FROM attachments WHERE id = ? AND run_id = ?', int(aid), rid):
            db.run('DELETE FROM attachments WHERE id = ?', int(aid))
            db.audit(actor(me), 'photo_removed', 'run', rid, attachment=int(aid))
    if action != 'submit':
        if photo_errors:
            return render_run(request, me, run_or_404(rid), photo_errors=photo_errors, status_code=400)
        saved_at = local(db.now(), '%H:%M')
        if action == 'save_close':
            request.session['flash'] = (f'Draft of {chk["title"]} saved at {saved_at}. Continue it from '
                                        '"Checklists in progress" on this page.')
            return RedirectResponse('/registry', status_code=303)
        if action == 'photo':
            request.session['flash'] = 'Photo added and draft saved.'
        elif action.startswith('delete:'):
            request.session['flash'] = 'Photo removed.'
        else:
            request.session['flash'] = f'Draft saved at {saved_at}.'
        anchor = re.sub(r'[^A-Za-z0-9_.-]', '', form.get('anchor', ''))
        return RedirectResponse(f'/records/checklist/{rid}' + (f'#i-{anchor}' if anchor else ''), status_code=303)

    counts = {k: len(v) for k, v in checklists.photos(rid).items()}
    answers, errors, outcome, outcome_label = checklists.evaluate(chk, answers, counts)
    if chk.get('signature_required'):
        if not signed_name:
            errors['_signature'] = 'Type your name.'
        elif not signature:
            errors['_signature'] = 'Draw your signature.'
    if errors or photo_errors:
        return render_run(request, me, run_or_404(rid), errors=errors, photo_errors=photo_errors, status_code=400)
    now = db.now()
    db.run("UPDATE runs SET status = 'submitted', answers = ?, outcome = ?, outcome_label = ?, submitted_at = ?, "
           'submitted_by = ?, snapshot = ? WHERE id = ?', json.dumps(answers), outcome, outcome_label, now, me['name'],
           json.dumps(chk), rid)
    failed = [it['id'] for it in checklists.failed_items(chk, answers)]
    db.audit(actor(me), 'checklist_submitted', 'run', rid, checklist=r['checklist_id'], version=r['checklist_version'],
             outcome=outcome_label, failed=failed)
    label = outcome_label
    request.session['flash'] = f'{chk["title"]} submitted: {label}.'
    return RedirectResponse(f'/records/checklist/{rid}', status_code=303)


@app.get('/runs/{rid}')
def run_redirect(rid: int):
    """Checklist records used to live at /runs/<id>."""
    return RedirectResponse(f'/records/checklist/{rid}', status_code=301)


@app.post('/records/checklist/{rid}/delete')
def run_delete(request: Request, rid: int, reason: str = Form(''), csrf: str = Form(...)):
    me = require(request, csrf)
    r = run_or_404(rid)
    if r['status'] == 'draft':
        raise HTTPException(400, 'Discard a draft instead.')
    reason = ' '.join(reason.split())
    if len(reason) < 3:
        return message(request, 'Reason needed', 'Give a reason for deleting the record. It is kept in the audit log.',
                       400, link=f'/records/checklist/{rid}')
    # keep a short note of what was deleted (no answers or photos), so the audit log still makes sense
    db.audit(actor(me), 'checklist_deleted', 'run', rid, reason=reason, checklist=r['title'],
             submitted_by=r['submitted_by'], submitted=local(r['submitted_at']),
             outcome=checklists.outcome_label(r['outcome'], r['outcome_label'])[0])
    db.run('DELETE FROM attachments WHERE run_id = ?', rid)
    db.run('DELETE FROM runs WHERE id = ?', rid)
    request.session['flash'] = f'Checklist record {rid} ({r["title"]}) deleted.'
    return RedirectResponse('/registry?view=checklists#records', status_code=303)


@app.post('/records/checklist/{rid}/discard')
def run_discard(request: Request, rid: int, csrf: str = Form(...)):
    me = require(request, csrf)
    r = run_or_404(rid)
    if r['status'] != 'draft':
        raise HTTPException(400, 'A submitted checklist cannot be discarded.')
    db.run('DELETE FROM attachments WHERE run_id = ?', rid)
    db.run('DELETE FROM runs WHERE id = ?', rid)
    db.audit(actor(me), 'checklist_discarded', 'run', rid, checklist=r['checklist_id'])
    request.session['flash'] = f'Draft {r["title"]} discarded.'
    return RedirectResponse('/registry', status_code=303)


@app.post('/records/checklist/{rid}/amend')
def run_amend(request: Request, rid: int, reason: str = Form(''), note: str = Form(''), csrf: str = Form(...)):
    me = require(request, csrf)
    r = run_or_404(rid)
    if r['status'] != 'submitted':
        raise HTTPException(400, 'Only a submitted checklist can be amended.')
    reason, note = reason.strip()[:300], note.strip()[:2000]
    if not reason or not note:
        request.session['flash'] = 'An amendment needs both a reason and the correction itself.'
        return RedirectResponse(f'/records/checklist/{rid}#amend', status_code=303)
    amendments = json.loads(r['amendments'] or '[]')
    amendments.append({'by': me['name'], 'at': db.now(), 'reason': reason, 'note': note})
    db.run('UPDATE runs SET amendments = ? WHERE id = ?', json.dumps(amendments), rid)
    db.audit(actor(me), 'checklist_amended', 'run', rid, reason=reason, note=note)
    request.session['flash'] = 'Amendment added.'
    return RedirectResponse(f'/records/checklist/{rid}#amendments', status_code=303)


@app.get('/attachments/{aid}')
def attachment(request: Request, aid: int, thumb: int = 0):
    require(request)
    a = db.one(f'SELECT {"thumb" if thumb else "data"} AS body, mime, filename FROM attachments WHERE id = ?', aid)
    if not a:
        raise HTTPException(404, 'Photo not found')
    return Response(a['body'], media_type=a['mime'],
                    headers={'Cache-Control': 'private, max-age=86400',
                             'Content-Disposition': f'inline; filename="{a["filename"]}"'})


@app.get('/export/checklists.csv')
def export_runs(request: Request, category: str = '', checklist: str = '', outcome: str = '', q: str = ''):
    me = require(request)
    where, args = run_filters(category, checklist, outcome, q)
    out = []
    for r in db.q(f'{RUN_LIST_SQL} WHERE {where} ORDER BY r.submitted_at', *args):
        chk = json.loads(r['snapshot']) if r['snapshot'] else checklists.checklist(r['checklist_id'], r['checklist_version'])[1]
        answers = db.loads(r['answers'])
        failed = checklists.failed_items(chk, answers)
        n_photos = db.one('SELECT COUNT(*) AS n FROM attachments WHERE run_id = ?', r['id'])['n']
        out.append([r['id'], r['checklist_id'], r['title'], r['category'], r['checklist_version'], r['submitted_by'] or '',
                    local(r['submitted_at']), checklists.outcome_label(r['outcome'], r['outcome_label'])[0],
                    '; '.join(it['text'] for it in failed), n_photos, len(json.loads(r['amendments'] or '[]'))])
    db.audit(actor(me), 'export_checklists')
    return csv_response('checklist-records.csv', ['Record', 'Checklist ID', 'Checklist', 'Category', 'Checklist version',
                                                  'Submitted by', 'Submitted', 'Outcome', 'Failed items', 'Photos',
                                                  'Amendments'], out)


# ----------------------------------------------------------------- assessor: sessions
@app.post('/sessions')
def start_session(request: Request, induction_id: str = Form(...), csrf: str = Form(...)):
    me = require(request, csrf)
    row, ind = logic.induction(induction_id)
    if not row:
        raise HTTPException(404, 'Induction not found')
    while True:
        code = security.new_code()
        if not db.one('SELECT 1 FROM sessions WHERE code = ? AND closed_at IS NULL', code):
            break
    join_until = (logic.utcnow() + timedelta(minutes=settings.session_minutes)).isoformat(timespec='seconds')
    sid = db.run('INSERT INTO sessions (code, induction_id, induction_version, assessor_id, created_at, join_until) '
                 'VALUES (?,?,?,?,?,?)', code, row['id'], row['version'], me['id'], db.now(), join_until)
    db.audit(actor(me), 'session_started', 'session', sid, induction=row['id'], version=row['version'])
    return RedirectResponse(f'/sessions/{sid}', status_code=303)


@app.get('/sessions/{sid}', response_class=HTMLResponse)
def session_page(request: Request, sid: int):
    me = require(request)
    s = session_row(sid)
    _, ind = logic.induction(s['induction_id'], s['induction_version'])
    join_url = f'{settings.base_url}/j/{s["code"]}'
    return render(request, 'session.html', me=me, s=s, ind=ind, join_url=join_url, qr=qr_svg(join_url),
                  ticks=db.loads(s['check_ticks']), joinable=session_open_for_joining(s))


@app.get('/sessions/{sid}/participants', response_class=HTMLResponse)
def session_participants(request: Request, sid: int):
    me = require(request)
    s = session_row(sid)
    _, ind = logic.induction(s['induction_id'], s['induction_version'])
    ticks = db.loads(s['check_ticks'])
    people = [(r, logic.progress(ind, ticks, r), db.loads(r['demo_ticks']), logic.status(r, settings))
              for r in participants(sid)]
    return render(request, '_participants.html', me=me, s=s, ind=ind, people=people,
                  demo_items=[it for it in logic.items(ind) if it['kind'] == 'demo'])


@app.post('/sessions/{sid}/tick')
async def session_tick(request: Request, sid: int):
    me = require(request, request.headers.get('x-csrf-token', ''))
    s = session_row(sid)
    if s['closed_at']:
        raise HTTPException(400, 'Session is closed')
    data = await request.json()
    _, ind = logic.induction(s['induction_id'], s['induction_version'])
    if data.get('item') not in logic.ids_of(ind, 'check'):
        raise HTTPException(400, 'Unknown item')
    ticks = db.loads(s['check_ticks'])
    if data.get('value'):
        ticks[data['item']] = {'by': me['name'], 'at': db.now()}
    else:
        ticks.pop(data['item'], None)
    db.run('UPDATE sessions SET check_ticks = ? WHERE id = ?', json.dumps(ticks), sid)
    return JSONResponse({'ok': True})


@app.post('/records/{rid}/demo')
async def record_demo(request: Request, rid: int):
    me = require(request, request.headers.get('x-csrf-token', ''))
    r = record_with_person(rid)
    if r['status'] not in ('joined', 'inductee_signed'):
        raise HTTPException(400, 'Record is already signed off')
    data = await request.json()
    _, ind = logic.induction(r['induction_id'], r['induction_version'])
    if data.get('item') not in logic.ids_of(ind, 'demo'):
        raise HTTPException(400, 'Unknown item')
    ticks = db.loads(r['demo_ticks'])
    if data.get('value'):
        ticks[data['item']] = {'by': me['name'], 'at': db.now()}
    else:
        ticks.pop(data['item'], None)
    db.run('UPDATE records SET demo_ticks = ? WHERE id = ?', json.dumps(ticks), rid)
    return JSONResponse({'ok': True})


@app.post('/sessions/{sid}/close')
def close_session(request: Request, sid: int, csrf: str = Form(...)):
    me = require(request, csrf)
    session_row(sid)
    db.run('UPDATE sessions SET closed_at = ? WHERE id = ? AND closed_at IS NULL', db.now(), sid)
    db.audit(actor(me), 'session_closed', 'session', sid)
    return RedirectResponse('/dashboard', status_code=303)


@app.post('/sessions/{sid}/extend')
def extend_session(request: Request, sid: int, csrf: str = Form(...)):
    me = require(request, csrf)
    session_row(sid)
    until = (logic.utcnow() + timedelta(minutes=settings.session_minutes)).isoformat(timespec='seconds')
    db.run('UPDATE sessions SET join_until = ? WHERE id = ? AND closed_at IS NULL', until, sid)
    db.audit(actor(me), 'session_extended', 'session', sid)
    return RedirectResponse(f'/sessions/{sid}', status_code=303)


# ----------------------------------------------------------------- public: joining and the inductee checklist
@app.get('/join', response_class=HTMLResponse)
def join_code_form(request: Request, code: str = ''):
    if code:
        return RedirectResponse(f'/j/{code.strip().upper()}', status_code=303)
    return render(request, 'join_code.html')


@app.get('/j/{code}', response_class=HTMLResponse)
def join_form(request: Request, code: str):
    ip = request.client.host if request.client else '?'
    if not join_limiter.allow(ip):
        return message(request, 'Too many attempts', 'Wait ten minutes and try again.', 429)
    s = db.one('SELECT * FROM sessions WHERE code = ? ORDER BY id DESC', code.upper())
    if not s or not session_open_for_joining(s):
        return message(request, 'Session not available',
                       'This induction code is not valid or has expired. Ask your assessor for a new code.', 404)
    _, ind = logic.induction(s['induction_id'], s['induction_version'])
    return render(request, 'join.html', s=s, ind=ind, roles=ROLES, me=None)


@app.post('/j/{code}')
def join(request: Request, code: str, name: str = Form(...), email: str = Form(...), unikey: str = Form(''),
         role: str = Form(''), csrf: str = Form(...)):
    if csrf != request.session.get('csrf'):
        return message(request, 'Form expired', 'Go back, reload the page and try again.', 400)
    s = db.one('SELECT * FROM sessions WHERE code = ? ORDER BY id DESC', code.upper())
    if not s or not session_open_for_joining(s):
        return message(request, 'Session not available', 'This induction code has expired. Ask your assessor.', 404)
    _, ind = logic.induction(s['induction_id'], s['induction_version'])
    name, email, unikey = ' '.join(name.split()), email.strip().lower(), unikey.strip().lower()
    form = dict(s=s, ind=ind, roles=ROLES, me=None, name=name, email=email, unikey=unikey, role=role)
    if len(name) < 2 or not EMAIL_RE.match(email):
        return render(request, 'join.html', status_code=400, error='Enter your full name and a valid email address.',
                      **form)
    if not UNIKEY_RE.match(unikey):
        return render(request, 'join.html', status_code=400, error=f'Enter your UniKey. {UNIKEY_HELP}', **form)
    pid, problem = resolve_person(name, email, unikey, role)
    if problem:
        return render(request, 'join.html', status_code=400, error=problem, **form)
    existing = db.one("SELECT * FROM records WHERE session_id = ? AND person_id = ? AND status != 'cancelled'",
                      s['id'], pid)
    if existing:
        return RedirectResponse(f'/p/{existing["participant_token"]}', status_code=303)
    token = security.new_token()
    rid = db.run('INSERT INTO records (session_id, person_id, induction_id, induction_version, assessor_id, status, '
                 'participant_token, created_at) VALUES (?,?,?,?,?,?,?,?)',
                 s['id'], pid, s['induction_id'], s['induction_version'], s['assessor_id'], 'joined', token, db.now())
    db.audit(f'inductee:{email}', 'joined_session', 'record', rid, session=s['id'])
    return RedirectResponse(f'/p/{token}', status_code=303)


def participant_record(token):
    r = db.one('SELECT r.*, p.name AS person_name, p.email AS person_email, p.unikey AS person_unikey FROM records r '
               'JOIN people p ON p.id = r.person_id WHERE r.participant_token = ?', token)
    if not r:
        raise HTTPException(404, 'Not found')
    return r


def participant_state(r):
    s = db.one('SELECT * FROM sessions WHERE id = ?', r['session_id'])
    _, ind = logic.induction(r['induction_id'], r['induction_version'])
    ticks = db.loads(s['check_ticks']) if r['status'] in ('joined', 'inductee_signed') else db.loads(r['check_ticks'])
    prog = logic.progress(ind, ticks, r)
    return s, ind, ticks, prog


@app.get('/p/{token}', response_class=HTMLResponse)
def participant_page(request: Request, token: str):
    r = participant_record(token)
    s, ind, ticks, prog = participant_state(r)
    return render(request, 'participant.html', me=None, r=r, ind=ind, ticks=ticks, prog=prog,
                  demo=db.loads(r['demo_ticks']), ack=db.loads(r['ack_ticks']), token=token)


@app.get('/p/{token}/state')
def participant_state_json(token: str):
    r = participant_record(token)
    s, ind, ticks, prog = participant_state(r)
    done = set(ticks) | set(db.loads(r['demo_ticks'])) | set(db.loads(r['ack_ticks']))
    return JSONResponse({'status': r['status'], 'done': sorted(done), 'complete': prog['complete'],
                         'progress': f'{prog["done"]} of {prog["total"]}'})


@app.post('/p/{token}/ack')
async def participant_ack(request: Request, token: str):
    r = participant_record(token)
    if r['status'] != 'joined':
        raise HTTPException(400, 'Already signed')
    data = await request.json()
    _, ind = logic.induction(r['induction_id'], r['induction_version'])
    if data.get('item') not in logic.ids_of(ind, 'ack'):
        raise HTTPException(400, 'Unknown item')
    ack = db.loads(r['ack_ticks'])
    if data.get('value'):
        ack[data['item']] = {'by': r['person_email'], 'at': db.now()}
    else:
        ack.pop(data['item'], None)
    db.run('UPDATE records SET ack_ticks = ? WHERE id = ?', json.dumps(ack), r['id'])
    return JSONResponse({'ok': True})


@app.post('/p/{token}/sign')
def participant_sign(request: Request, token: str, typed_name: str = Form(...), declaration: str = Form(''),
                     signature: str = Form('')):
    r = participant_record(token)
    if r['status'] != 'joined':
        return RedirectResponse(f'/p/{token}', status_code=303)
    s, ind, ticks, prog = participant_state(r)
    if not prog['complete']:
        return message(request, 'Not finished yet', 'Every item must be completed before you sign.', 400,
                       link=f'/p/{token}')
    typed = ' '.join(typed_name.split())
    if not declaration or typed.lower() != r['person_name'].lower():
        return message(request, 'Check your signature',
                       f'Tick the declaration and type your name exactly as entered: {r["person_name"]}.', 400,
                       link=f'/p/{token}')
    if signature and not signature.startswith('data:image/png;base64,'):
        signature = ''
    if ind.get('signature_required') and not signature:
        return message(request, 'Signature needed', 'Draw your signature in the box before signing.', 400,
                       link=f'/p/{token}')
    db.run("UPDATE records SET status = 'inductee_signed', inductee_name = ?, inductee_signature = ?, "
           'inductee_signed_at = ? WHERE id = ?', typed, signature[:400000] or None, db.now(), r['id'])
    db.audit(f'inductee:{r["person_email"]}', 'inductee_signed', 'record', r['id'])
    return RedirectResponse(f'/p/{token}', status_code=303)


# ----------------------------------------------------------------- assessor: sign-off
@app.get('/records/{rid}/signoff', response_class=HTMLResponse)
def signoff_form(request: Request, rid: int):
    me = require(request)
    r = record_with_person(rid)
    s = db.one('SELECT * FROM sessions WHERE id = ?', r['session_id'])
    _, ind = logic.induction(r['induction_id'], r['induction_version'])
    prog = logic.progress(ind, db.loads(s['check_ticks']), r)
    return render(request, 'signoff.html', me=me, r=r, ind=ind, prog=prog)


@app.post('/records/{rid}/signoff')
def signoff(request: Request, rid: int, refresh_months: int = Form(...), notes: str = Form(''),
            declaration: str = Form(''), csrf: str = Form(...)):
    me = require(request, csrf)
    r = record_with_person(rid)
    if r['status'] != 'inductee_signed':
        return message(request, 'Cannot sign off', 'The inductee must finish and sign first.', 400,
                       link=f'/sessions/{r["session_id"]}')
    s = db.one('SELECT * FROM sessions WHERE id = ?', r['session_id'])
    _, ind = logic.induction(r['induction_id'], r['induction_version'])
    ticks = db.loads(s['check_ticks'])
    if not logic.progress(ind, ticks, r)['complete'] or not declaration:
        return message(request, 'Cannot sign off', 'Every item must be complete and the declaration ticked.', 400,
                       link=f'/records/{rid}/signoff')
    refresh_months = max(0, min(int(refresh_months), 120))
    db.run("UPDATE records SET status = 'awaiting_confirmation', assessor_id = ?, assessor_signed_at = ?, "
           'check_ticks = ?, refresh_months = ?, notes = ?, snapshot = ? WHERE id = ?',
           me['id'], db.now(), json.dumps(ticks), refresh_months, notes.strip()[:2000] or None,
           json.dumps(ind), rid)
    db.audit(actor(me), 'assessor_signed_off', 'record', rid, refresh_months=refresh_months)
    status = send_confirmation(record_with_person(rid), ind, me)
    request.session['flash'] = ('Signed off. Confirmation email sent.' if status == 'sent' else
                                'Signed off. The confirmation email was not sent '
                                f'({status.replace("_", " ")}); see Email log.')
    return RedirectResponse(f'/sessions/{r["session_id"]}', status_code=303)


@app.post('/records/{rid}/resend')
def resend(request: Request, rid: int, csrf: str = Form(...)):
    me = require(request, csrf)
    r = record_with_person(rid)
    if r['status'] != 'awaiting_confirmation':
        return message(request, 'Not awaiting confirmation', 'Only unconfirmed records can be resent.', 400)
    _, ind = logic.induction(r['induction_id'], r['induction_version'])
    send_confirmation(r, ind, me)
    return RedirectResponse(f'/records/{rid}', status_code=303)


@app.post('/records/{rid}/cancel')
def cancel(request: Request, rid: int, csrf: str = Form(...)):
    me = require(request, csrf)
    r = record_with_person(rid)
    manual_complete = r['entry_method'] == 'manual' and r['status'] == 'complete'
    if r['status'] in ('superseded', 'cancelled') or (r['status'] == 'complete' and not manual_complete):
        return message(request, 'Cannot cancel', 'Confirmed records are never changed.', 400)
    db.run("UPDATE records SET status = 'cancelled', confirm_token_hash = NULL WHERE id = ?", rid)
    db.audit(actor(me), 'record_cancelled', 'record', rid)
    return RedirectResponse(f'/records/{rid}', status_code=303)


# ----------------------------------------------------------------- certificates
def certificate_data(r):
    """Display strings for a completed record's certificate."""
    ind = json.loads(r['snapshot']) if r['snapshot'] else logic.induction(r['induction_id'], r['induction_version'])[1]
    return {
        'record_id': str(r['id']),
        'issued': datetime.now(ZoneInfo(settings.timezone)).strftime('%d %b %Y'),
        'name': r['person_name'],
        'induction': ind['title'],
        'location': ind['location'],
        'induction_date': local(r['assessor_signed_at'], '%d %b %Y'),
        'valid_for': certificate.months_text(r['refresh_months']),
        'renewal_due': local(r['expires_at'], '%d %b %Y') if r['expires_at'] else 'Not required',
        'assessor': r['assessor_name'] or '',
        'sources': '; '.join(f'{s["title"]} (version {s.get("version", "")})' for s in ind.get('sources', [])),
        'grants_access': logic.grants_access(ind),
    }


def certificate_filename(r, data):
    safe = re.sub(r'[^A-Za-z0-9]+', '-', f'{data["name"]} {data["induction"]}').strip('-')
    return f'SPL-certificate-{safe}-{r["id"]}.pdf'


def send_certificate(r, actor_name):
    """Email the confirmation with the PDF certificate to the inductee, CC'd to the certificate_cc list."""
    data = certificate_data(r)
    attachments, note = [], ''
    try:
        attachments = [(certificate_filename(r, data), certificate.render(data), 'application/pdf')]
    except Exception as e:  # never let a PDF problem block the confirmation email
        note = '\n(The certificate could not be attached this time. SPL staff can send it again.)\n'
        db.audit(actor_name, 'certificate_failed', 'record', r['id'], error=str(e)[:300])
    access = (f'Grants access: {", ".join(data["grants_access"])}\n' if data['grants_access'] else '')
    body = (f'Hi {data["name"]},\n\nYour {data["induction"]} ({data["location"]}) is complete.\n\n'
            f'Assessor: {data["assessor"]}\nInduction date: {data["induction_date"]}\n'
            f'Valid for: {data["valid_for"]}\nRenewal due: {data["renewal_due"]}\n{access}'
            f'Record number: {r["id"]}\n\nYour certificate is attached. Keep it for your records'
            f'{" and quote the access above when requesting card access" if access else ""}.\n{note}\n'
            'Sydney Propulsion Lab, University of Sydney')
    status = mailer.send(settings, r['person_email'], f'Induction certificate: {data["induction"]}', body,
                         cc=settings.certificate_cc, attachments=attachments)
    db.audit(actor_name, 'certificate_sent', 'record', r['id'], to=r['person_email'],
             cc=', '.join(settings.certificate_cc), status=status, attached=bool(attachments))
    return status


@app.get('/records/{rid}/certificate.pdf')
def certificate_download(request: Request, rid: int):
    me = require(request)
    r = record_with_person(rid)
    if r['status'] != 'complete':
        raise HTTPException(400, 'Certificates are only available for completed inductions.')
    data = certificate_data(r)
    pdf = certificate.render(data)
    db.audit(actor(me), 'certificate_downloaded', 'record', rid)
    return Response(pdf, media_type='application/pdf',
                    headers={'Content-Disposition': f'inline; filename="{certificate_filename(r, data)}"'})


@app.post('/records/{rid}/certificate/send')
def certificate_resend(request: Request, rid: int, csrf: str = Form(...)):
    me = require(request, csrf)
    r = record_with_person(rid)
    if r['status'] != 'complete':
        return message(request, 'Not complete', 'Certificates are only sent for completed inductions.', 400,
                       link=f'/records/{rid}')
    status = send_certificate(r, actor(me))
    cc = f' (CC {", ".join(settings.certificate_cc)})' if settings.certificate_cc else ''
    request.session['flash'] = (f'Certificate emailed to {r["person_email"]}{cc}.' if status == 'sent' else
                                f'Certificate email {status.replace("_", " ")}; see Email log.')
    return RedirectResponse(f'/records/{rid}', status_code=303)


# ----------------------------------------------------------------- public: email confirmation
def record_by_confirm_token(token):
    return db.one('SELECT r.*, p.name AS person_name, p.email AS person_email, p.unikey AS person_unikey, a.name AS assessor_name '
                  'FROM records r JOIN people p ON p.id = r.person_id LEFT JOIN assessors a ON a.id = r.assessor_id '
                  'WHERE r.confirm_token_hash = ?', security.token_hash(token))


@app.get('/c/{token}', response_class=HTMLResponse)
def confirm_page(request: Request, token: str):
    # Showing a button (not confirming on GET) stops email link scanners confirming records.
    r = record_by_confirm_token(token)
    if not r or r['status'] != 'awaiting_confirmation':
        return message(request, 'Link not valid', 'This confirmation link has already been used or is not valid.', 404)
    if logic.parse(r['confirm_expires_at']) <= logic.utcnow():
        return message(request, 'Link expired', 'This confirmation link has expired. Ask SPL staff to resend it.', 410)
    ind = json.loads(r['snapshot'])
    return render(request, 'confirm.html', me=None, r=r, ind=ind, token=token)


@app.post('/c/{token}')
def confirm(request: Request, token: str):
    r = record_by_confirm_token(token)
    if not r or r['status'] != 'awaiting_confirmation' or logic.parse(r['confirm_expires_at']) <= logic.utcnow():
        return message(request, 'Link not valid', 'This confirmation link has expired or has already been used.', 404)
    signed = logic.parse(r['assessor_signed_at'])
    expires = logic.add_months(signed, r['refresh_months']).isoformat(timespec='seconds') if r['refresh_months'] else None
    db.run("UPDATE records SET status = 'complete', confirmed_at = ?, expires_at = ?, confirm_token_hash = NULL "
           'WHERE id = ?', db.now(), expires, r['id'])
    db.audit(f'inductee:{r["person_email"]}', 'confirmed', 'record', r['id'])
    ind = json.loads(r['snapshot'])
    send_certificate(record_with_person(r['id']), f'inductee:{r["person_email"]}')
    return message(request, 'Induction confirmed',
                   f'Thank you, {r["person_name"]}. Your {ind["title"]} is confirmed'
                   + (f' and valid until {local(expires, "%d %b %Y")}.' if expires else '.')
                   + ' Your certificate has been emailed to you.')


# ----------------------------------------------------------------- people: UniKey identity
def mask_email(e):
    user, _, domain = e.partition('@')
    return f'{user[:1]}***@{domain}'


def resolve_person(name, email, unikey, role, assessor=False):
    """Find or create the person for a UniKey and email. Returns (person_id, error).

    The UniKey is the identity. Existing people without a UniKey are matched by email and get it filled in.
    A UniKey and email that point at two different people, or at someone with a different email or UniKey,
    are refused rather than merged; an assessor sorts those out under Edit details.
    """
    by_u = db.one('SELECT * FROM people WHERE unikey = ?', unikey)
    by_e = db.one('SELECT * FROM people WHERE email = ?', email)
    show = (lambda e: e) if assessor else mask_email
    if by_u and by_e and by_u['id'] != by_e['id']:
        return None, (f'UniKey {unikey} is recorded for {by_u["name"] if assessor else "another person"} and '
                      f'{email} for {by_e["name"] if assessor else "someone else"}. '
                      + ('Fix their details under Edit details first.' if assessor else 'Ask your assessor to sort this out.'))
    if by_u:
        if by_u['email'] != email:
            return None, (f'UniKey {unikey} is recorded with the email {show(by_u["email"])}. '
                          + ('Use that email, or update it under Edit details.' if assessor
                             else 'Use that email, or ask your assessor to update it.'))
        db.run('UPDATE people SET name = ?, role = ?, updated_at = ? WHERE id = ?',
               name, role or by_u['role'], db.now(), by_u['id'])
        return by_u['id'], None
    if by_e:
        if by_e['unikey'] and by_e['unikey'] != unikey:
            return None, (f'{email} is recorded with a different UniKey'
                          + (f' ({by_e["unikey"]}). Check it, or update it under Edit details.' if assessor
                             else '. Check your UniKey, or ask your assessor.'))
        db.run('UPDATE people SET name = ?, role = ?, unikey = ?, updated_at = ? WHERE id = ?',
               name, role or by_e['role'], unikey, db.now(), by_e['id'])
        return by_e['id'], None
    return db.run('INSERT INTO people (name, email, unikey, role, created_at, updated_at) VALUES (?,?,?,?,?,?)',
                  name, email, unikey, role, db.now(), db.now()), None


# ----------------------------------------------------------------- assessor: manual records


def manual_form(request, me, status_code=200, **ctx):
    ctx.setdefault('f', {})
    return render(request, 'record_new.html', status_code=status_code, me=me, roles=ROLES,
                  inductions=logic.active_inductions(),
                  assessors=db.q('SELECT * FROM assessors ORDER BY active DESC, name'),
                  today=datetime.now(ZoneInfo(settings.timezone)).date().isoformat(), **ctx)


@app.get('/records/new', response_class=HTMLResponse)
def manual_record_form(request: Request, person: int = 0, induction: str = ''):
    me = require(request)
    f = {'induction_id': induction, 'assessor_id': str(me['id'])}
    p = db.one('SELECT * FROM people WHERE id = ?', person) if person else None
    if p:
        f.update(name=p['name'], email=p['email'], unikey=p['unikey'] or '', role=p['role'])
    return manual_form(request, me, f=f)


@app.post('/records/new')
def manual_record(request: Request, name: str = Form(...), email: str = Form(...), unikey: str = Form(''),
                  role: str = Form(''),
                  induction_id: str = Form(...), induction_date: str = Form(...), assessor_id: str = Form(''),
                  assessor_text: str = Form(''), refresh_months: str = Form(''), notes: str = Form(''),
                  csrf: str = Form(...)):
    me = require(request, csrf)
    f = {'name': ' '.join(name.split()), 'email': email.strip().lower(), 'unikey': unikey.strip().lower(),
         'role': role, 'induction_id': induction_id,
         'induction_date': induction_date, 'assessor_id': assessor_id, 'assessor_text': ' '.join(assessor_text.split()),
         'refresh_months': refresh_months, 'notes': notes.strip()}
    errors = []
    if len(f['name']) < 2:
        errors.append('Enter the person\'s full name.')
    if not EMAIL_RE.match(f['email']):
        errors.append('Enter a valid email address.')
    if not UNIKEY_RE.match(f['unikey']):
        errors.append(f'Enter their UniKey (usually 4 letters then 4 digits, e.g. abcd1234).')
    row, ind = logic.induction(induction_id)
    if not row:
        errors.append('Choose an induction.')
    tz = ZoneInfo(settings.timezone)
    try:
        day = date.fromisoformat(induction_date)
        if day > datetime.now(tz).date():
            errors.append('The induction date cannot be in the future.')
    except ValueError:
        day = None
        errors.append('Enter the date the induction was done.')
    assessor = None
    if assessor_id and assessor_id != 'other':
        assessor = db.one('SELECT * FROM assessors WHERE id = ?', assessor_id)
    if not assessor and not f['assessor_text']:
        errors.append('Choose who did the induction, or type their name under "Someone else".')
    try:
        months = int(refresh_months) if refresh_months.strip() else (row['refresh_months'] if row else 0)
        if not 0 <= months <= 120:
            raise ValueError
    except ValueError:
        months = 0
        errors.append('Refresh period must be between 0 and 120 months.')
    if errors:
        return manual_form(request, me, status_code=400, f=f, errors=errors)

    # Stored at midday local time so the date never shifts when converted to UTC.
    signed = datetime.combine(day, time(12), tz).astimezone(ZoneInfo('UTC'))
    signed_iso = signed.isoformat(timespec='seconds')
    pid, problem = resolve_person(f['name'], f['email'], f['unikey'], f['role'], assessor=True)
    if problem:
        return manual_form(request, me, status_code=400, f=f, errors=[problem])
    for other in db.q("SELECT assessor_signed_at FROM records WHERE person_id = ? AND induction_id = ? "
                      "AND status != 'cancelled' AND assessor_signed_at IS NOT NULL", pid, row['id']):
        if logic.parse(other['assessor_signed_at']).astimezone(tz).date() == day:
            return manual_form(request, me, status_code=400, f=f,
                               errors=[f'{f["name"]} already has a {ind["title"]} record on that date.'])
    expires = logic.add_months(signed, months).isoformat(timespec='seconds') if months else None
    rid = db.run('INSERT INTO records (person_id, induction_id, induction_version, assessor_id, assessor_text, status, '
                 'participant_token, assessor_signed_at, refresh_months, expires_at, notes, snapshot, entry_method, '
                 "entered_by, created_at) VALUES (?,?,?,?,?,'complete',?,?,?,?,?,?,'manual',?,?)",
                 pid, row['id'], row['version'], assessor['id'] if assessor else None,
                 None if assessor else f['assessor_text'], security.new_token(), signed_iso, months, expires,
                 f['notes'][:2000] or None, json.dumps(ind), me['id'], db.now())
    db.audit(actor(me), 'record_entered_manually', 'record', rid, induction=row['id'], date=day.isoformat(),
             assessor=assessor['name'] if assessor else f['assessor_text'])
    request.session['flash'] = f'Record added for {f["name"]}.'
    return RedirectResponse(f'/records/{rid}', status_code=303)


# ----------------------------------------------------------------- assessor: editing and deleting
EDITABLE = ('complete',)


def edit_form(request, me, r, status_code=200, **ctx):
    tz = ZoneInfo(settings.timezone)
    signed = logic.parse(r['assessor_signed_at'])
    f = ctx.pop('f', None) or {
        'induction_id': r['induction_id'],
        'induction_date': signed.astimezone(tz).date().isoformat() if signed else '',
        'assessor_id': str(r['assessor_id']) if r['assessor_id'] else 'other',
        'assessor_text': r['assessor_text'] or '',
        'refresh_months': str(r['refresh_months'] if r['refresh_months'] is not None else 0),
        'notes': r['notes'] or '', 'reason': ''}
    return render(request, 'record_edit.html', status_code=status_code, me=me, r=r, f=f,
                  inductions=logic.active_inductions(),
                  assessors=db.q('SELECT * FROM assessors ORDER BY active DESC, name'),
                  today=datetime.now(tz).date().isoformat(), **ctx)


@app.get('/records/{rid}/edit', response_class=HTMLResponse)
def record_edit_form(request: Request, rid: int):
    me = require(request)
    r = record_with_person(rid)
    if r['status'] not in EDITABLE:
        return message(request, 'Cannot edit', 'Only completed records can be edited. Cancel an unfinished '
                       'record instead.', 400, link=f'/records/{rid}')
    return edit_form(request, me, r)


@app.post('/records/{rid}/edit')
def record_edit(request: Request, rid: int, induction_id: str = Form(''), induction_date: str = Form(...),
                assessor_id: str = Form(''), assessor_text: str = Form(''), refresh_months: str = Form(...),
                notes: str = Form(''), reason: str = Form(''), csrf: str = Form(...)):
    me = require(request, csrf)
    r = record_with_person(rid)
    if r['status'] not in EDITABLE:
        return message(request, 'Cannot edit', 'Only completed records can be edited.', 400, link=f'/records/{rid}')
    f = {'induction_id': induction_id or r['induction_id'], 'induction_date': induction_date,
         'assessor_id': assessor_id, 'assessor_text': ' '.join(assessor_text.split()),
         'refresh_months': refresh_months, 'notes': notes.strip(), 'reason': ' '.join(reason.split())}
    errors = []
    tz = ZoneInfo(settings.timezone)
    # The induction can only be changed on manual entries; app records were run against a specific checklist.
    new_ind_id = f['induction_id'] if r['entry_method'] == 'manual' else r['induction_id']
    row, ind = logic.induction(new_ind_id) if new_ind_id != r['induction_id'] else (None, None)
    if new_ind_id != r['induction_id'] and not row:
        errors.append('Choose an induction.')
    try:
        day = date.fromisoformat(induction_date)
        if day > datetime.now(tz).date():
            errors.append('The induction date cannot be in the future.')
    except ValueError:
        day = None
        errors.append('Enter the induction date.')
    assessor = db.one('SELECT * FROM assessors WHERE id = ?', assessor_id) if assessor_id not in ('', 'other') else None
    if not assessor and not f['assessor_text']:
        errors.append('Choose who did the induction, or type their name under "Someone else".')
    try:
        months = int(refresh_months)
        if not 0 <= months <= 120:
            raise ValueError
    except ValueError:
        months = None
        errors.append('Refresh period must be between 0 and 120 months.')
    if len(f['reason']) < 3:
        errors.append('Give a reason for the change. It is kept in the record history.')
    if errors:
        return edit_form(request, me, r, status_code=400, f=f, errors=errors)

    old_signed = logic.parse(r['assessor_signed_at'])
    signed = old_signed
    if old_signed is None or old_signed.astimezone(tz).date() != day:
        signed = datetime.combine(day, time(12), tz).astimezone(ZoneInfo('UTC'))
    if new_ind_id != r['induction_id']:
        for other in db.q("SELECT assessor_signed_at FROM records WHERE person_id = ? AND induction_id = ? "
                          "AND status != 'cancelled' AND id != ? AND assessor_signed_at IS NOT NULL",
                          r['person_id'], new_ind_id, rid):
            if logic.parse(other['assessor_signed_at']).astimezone(tz).date() == day:
                return edit_form(request, me, r, status_code=400, f=f,
                                 errors=[f'{r["person_name"]} already has a {ind["title"]} record on that date.'])
    new = {
        'assessor_signed_at': signed.isoformat(timespec='seconds'),
        'assessor_id': assessor['id'] if assessor else None,
        'assessor_text': None if assessor else f['assessor_text'],
        'refresh_months': months,
        'expires_at': logic.add_months(signed, months).isoformat(timespec='seconds') if months else None,
        'notes': f['notes'][:2000] or None,
    }
    if new_ind_id != r['induction_id']:
        new.update(induction_id=row['id'], induction_version=row['version'], snapshot=json.dumps(ind))
    labels = {'assessor_signed_at': 'induction date', 'assessor_id': 'assessor', 'assessor_text': 'assessor',
              'refresh_months': 'refresh months', 'expires_at': 'valid until', 'notes': 'notes',
              'induction_id': 'induction'}
    changes = {}
    for k, v in new.items():
        if k in labels and r[k] != v:
            old_v, new_v = r[k], v
            if k in ('assessor_signed_at', 'expires_at'):
                old_v, new_v = local(old_v, '%d %b %Y') or 'none', local(new_v, '%d %b %Y') or 'none'
            elif k in ('assessor_id', 'assessor_text'):
                old_v, new_v = r['assessor_name'] or '', assessor['name'] if assessor else f['assessor_text']
            if old_v != new_v:
                changes[labels[k]] = [old_v, new_v]
    if not changes:
        request.session['flash'] = 'Nothing changed.'
        return RedirectResponse(f'/records/{rid}', status_code=303)
    db.run(f'UPDATE records SET {", ".join(k + " = ?" for k in new)} WHERE id = ?', *new.values(), rid)
    db.audit(actor(me), 'record_edited', 'record', rid, reason=f['reason'], changes=changes)
    request.session['flash'] = 'Record updated.'
    return RedirectResponse(f'/records/{rid}', status_code=303)


@app.post('/records/{rid}/delete')
def record_delete(request: Request, rid: int, reason: str = Form(''), csrf: str = Form(...)):
    me = require(request, csrf)
    r = record_with_person(rid)
    reason = ' '.join(reason.split())
    if len(reason) < 3:
        return message(request, 'Reason needed', 'Give a reason for deleting the record. It is kept in the audit log.',
                       400, link=f'/records/{rid}')
    title = json.loads(r['snapshot'])['title'] if r['snapshot'] else r['induction_id']
    # Keep a short note of what was deleted (no signature or checklist), so the audit log still makes sense.
    db.audit(actor(me), 'record_deleted', 'record', rid, reason=reason, person=r['person_name'],
             email=r['person_email'], induction=title, status=r['status'],
             induction_date=local(r['assessor_signed_at'], '%d %b %Y'), entry=r['entry_method'])
    db.run('DELETE FROM records WHERE id = ?', rid)
    request.session['flash'] = f'Record {rid} ({title}, {r["person_name"]}) deleted.'
    return RedirectResponse(f'/people/{r["person_id"]}', status_code=303)


@app.get('/people/{pid}/edit', response_class=HTMLResponse)
def person_edit_form(request: Request, pid: int):
    me = require(request)
    p = db.one('SELECT * FROM people WHERE id = ?', pid)
    if not p:
        raise HTTPException(404, 'Person not found')
    return render(request, 'person_edit.html', me=me, p=p, roles=ROLES, f=dict(p))


@app.post('/people/{pid}/edit')
def person_edit(request: Request, pid: int, name: str = Form(...), email: str = Form(...), unikey: str = Form(''),
                role: str = Form(''), csrf: str = Form(...)):
    me = require(request, csrf)
    p = db.one('SELECT * FROM people WHERE id = ?', pid)
    if not p:
        raise HTTPException(404, 'Person not found')
    f = {'name': ' '.join(name.split()), 'email': email.strip().lower(), 'unikey': unikey.strip().lower(), 'role': role}
    error = None
    if len(f['name']) < 2 or not EMAIL_RE.match(f['email']):
        error = 'Enter a full name and a valid email address.'
    elif not UNIKEY_RE.match(f['unikey']):
        error = 'Enter their UniKey (usually 4 letters then 4 digits, e.g. abcd1234).'
    elif f['email'] != p['email'] and db.one('SELECT 1 FROM people WHERE email = ?', f['email']):
        error = 'Someone else already has that email address.'
    elif f['unikey'] != p['unikey'] and db.one('SELECT 1 FROM people WHERE unikey = ?', f['unikey']):
        other = db.one('SELECT id, name FROM people WHERE unikey = ?', f['unikey'])
        error = f'UniKey {f["unikey"]} already belongs to {other["name"]} (person {other["id"]}).'
    if error:
        return render(request, 'person_edit.html', status_code=400, me=me, p=p, roles=ROLES, f=f, error=error)
    changes = {k: [p[k], v] for k, v in f.items() if p[k] != v}
    if changes:
        db.run('UPDATE people SET name = ?, email = ?, unikey = ?, role = ?, updated_at = ? WHERE id = ?',
               f['name'], f['email'], f['unikey'], f['role'], db.now(), pid)
        db.audit(actor(me), 'person_edited', 'person', pid, changes=changes)
    request.session['flash'] = 'Details updated.' if changes else 'Nothing changed.'
    return RedirectResponse(f'/people/{pid}', status_code=303)


@app.post('/people/{pid}/delete')
def person_delete(request: Request, pid: int, csrf: str = Form(...)):
    me = require(request, csrf)
    p = db.one('SELECT * FROM people WHERE id = ?', pid)
    if not p:
        raise HTTPException(404, 'Person not found')
    if db.one('SELECT 1 FROM records WHERE person_id = ?', pid):
        return message(request, 'Cannot delete', 'Delete this person\'s records first.', 400, link=f'/people/{pid}')
    db.audit(actor(me), 'person_deleted', 'person', pid, name=p['name'], email=p['email'])
    db.run('DELETE FROM people WHERE id = ?', pid)
    request.session['flash'] = f'{p["name"]} deleted.'
    return RedirectResponse('/registry', status_code=303)


# ----------------------------------------------------------------- assessor: records, people, register
@app.get('/records/{rid}', response_class=HTMLResponse)
def record_page(request: Request, rid: int):
    me = require(request)
    r = record_with_person(rid)
    ind = json.loads(r['snapshot']) if r['snapshot'] else logic.induction(r['induction_id'], r['induction_version'])[1]
    _, current = logic.induction(r['induction_id'])
    session_ticks = {}
    if r['status'] in ('joined', 'inductee_signed') and r['session_id']:
        session_ticks = db.loads(db.one('SELECT check_ticks FROM sessions WHERE id = ?', r['session_id'])[0])
    ticks = {**(db.loads(r['check_ticks']) or session_ticks), **db.loads(r['demo_ticks']), **db.loads(r['ack_ticks'])}
    history = [(h, db.loads(h['details'])) for h in
               db.q("SELECT * FROM audit_log WHERE entity = 'record' AND entity_id = ? ORDER BY id", str(rid))]
    return render(request, 'record.html', me=me, r=r, ind=ind, ticks=ticks,
                  status=logic.status(r, settings, current), history=history,
                  newer=current if current and current['version'] != r['induction_version'] else None)


@app.get('/records/{rid}/compare', response_class=HTMLResponse)
def record_compare(request: Request, rid: int, against: str = ''):
    """Git-style comparison of the checklist a record was signed against with the current (or another) version."""
    me = require(request)
    r = record_with_person(rid)
    old = json.loads(r['snapshot']) if r['snapshot'] else logic.induction(r['induction_id'], r['induction_version'])[1]
    versions = db.q('SELECT i.version, i.active, b.uploaded_at FROM inductions i JOIN bundles b ON b.id = i.bundle_id '
                    'WHERE i.id = ? ORDER BY i.bundle_id DESC', r['induction_id'])
    row, new = logic.induction(r['induction_id'], against) if against else logic.induction(r['induction_id'])
    if not row:
        return message(request, 'Nothing to compare', 'This induction is no longer in the loaded bundle, so there is '
                       'no current version to compare with.', 404, link=f'/records/{rid}')
    return render(request, 'compare.html', me=me, r=r, old=old, new=new, row=row, versions=versions,
                  d=diff.compare(old, new), kinds=diff.KIND_LABELS)


@app.get('/people/{pid}', response_class=HTMLResponse)
def person_page(request: Request, pid: int):
    me = require(request)
    p = db.one('SELECT * FROM people WHERE id = ?', pid)
    if not p:
        raise HTTPException(404, 'Person not found')
    recs = []
    for r in db.q('SELECT r.*, COALESCE(a.name, r.assessor_text) AS assessor_name FROM records r '
                  'LEFT JOIN assessors a ON a.id = r.assessor_id WHERE r.person_id = ? '
                  'ORDER BY COALESCE(r.assessor_signed_at, r.created_at) DESC, r.id DESC', pid):
        row, cur = logic.induction(r['induction_id'])
        title = (json.loads(r['snapshot'])['title'] if r['snapshot'] else
                 logic.induction(r['induction_id'], r['induction_version'])[1]['title'])
        recs.append((r, title, logic.status(r, settings, cur)))
    return render(request, 'person.html', me=me, p=p, recs=recs)


INDUCTION_STATUS_FILTERS = {'current': 'Current', 'expiring': 'Expiring soon', 'expired': 'Expired',
                            'waiting': 'Waiting on someone', 'ended': 'Cancelled or replaced', 'all': 'Everything'}
WAITING = ('In progress', 'Ready for sign-off', 'Awaiting confirmation', 'Confirmation link expired')


def induction_records(q='', induction='', location='', category='', status=''):
    """Induction records for the Registry's Records list, newest first, with the filters applied.
    By default cancelled and replaced records are left out."""
    rows = db.q('SELECT r.*, p.name AS person_name, p.email AS person_email, p.unikey AS person_unikey, '
                'p.role AS person_role, COALESCE(a.name, r.assessor_text) AS assessor_name FROM records r '
                'JOIN people p ON p.id = r.person_id LEFT JOIN assessors a ON a.id = r.assessor_id '
                'ORDER BY COALESCE(r.assessor_signed_at, r.created_at) DESC, r.id DESC')
    current = {}
    out = []
    ql = q.strip().lower()
    for r in rows:
        if r['induction_id'] not in current:
            current[r['induction_id']] = logic.induction(r['induction_id'])[1]
        cur = current[r['induction_id']]
        snap = json.loads(r['snapshot']) if r['snapshot'] else (cur or {})
        title = snap.get('title') or r['induction_id']
        cat = ((cur or snap).get('type') or '')
        loc = (cur or snap).get('location') or ''
        label, cls, flags = logic.status(r, settings, cur)
        ended = r['status'] in ('cancelled', 'superseded')
        if status == 'ended' and not ended or status not in ('ended', 'all') and ended:
            continue
        if status == 'current' and label != 'Current' or status == 'expiring' and label != 'Expiring' \
                or status == 'expired' and label != 'Expired' or status == 'waiting' and label not in WAITING:
            continue
        if induction and r['induction_id'] != induction or location and loc != location \
                or category and cat.lower() != category.lower():
            continue
        if ql and ql not in ' '.join([r['person_name'], r['person_email'], r['person_unikey'] or '', title]).lower():
            continue
        out.append({'r': r, 'title': title, 'category': cat, 'location': loc, 'status': (label, cls, flags),
                    'snap': snap})
    return out


def csv_response(filename, header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return Response(buf.getvalue(), media_type='text/csv',
                    headers={'Content-Disposition': f'attachment; filename="{filename}"'})


@app.get('/export/records.csv')
def export_records(request: Request, q: str = '', induction: str = '', location: str = '', category: str = '',
                   status: str = 'all'):
    """Induction records as CSV, with the Registry's filters (no filters: every record)."""
    me = require(request)
    out = []
    for x in reversed(induction_records(q, induction, location, category, status)):
        r, snap = x['r'], x['snap']
        label, _, flags = x['status']
        out.append([r['id'], r['person_name'], r['person_email'], r['person_unikey'] or '', r['person_role'],
                    r['induction_id'], x['title'], x['category'], x['location'], r['induction_version'],
                    '; '.join(f'{s["ref"]} v{s.get("version", "")} ({s.get("hash", "")})' for s in snap.get('sources', [])),
                    r['assessor_name'] or '', label, '; '.join(flags), local(r['inductee_signed_at']),
                    local(r['assessor_signed_at'], '%d %b %Y' if r['entry_method'] == 'manual' else '%d %b %Y %H:%M'),
                    local(r['confirmed_at']), local(r['expires_at'], '%d %b %Y'),
                    r['refresh_months'] if r['refresh_months'] is not None else '', r['notes'] or '',
                    'Manual entry' if r['entry_method'] == 'manual' else 'In app'])
    db.audit(actor(me), 'export_records')
    return csv_response('induction-records.csv',
                        ['Record', 'Name', 'Email', 'UniKey', 'Role', 'Induction ID', 'Induction', 'Category',
                         'Location', 'Checklist version', 'Source documents', 'Assessor', 'Status', 'Flags',
                         'Inductee signed', 'Assessor signed', 'Confirmed', 'Expires', 'Refresh months', 'Notes',
                         'Entry'], out)


@app.get('/export/register.csv')
def export_register(request: Request, location: str = '', type: str = ''):
    """The older grid export (one row per person, one column per induction); kept for existing links."""
    me = require(request)
    inds = [i for i in logic.active_inductions()
            if (not location or i[0]['location'] == location)
            and (not type or (i[0]['type'] or '').lower() == type.lower())]
    latest = {}
    for x in reversed(induction_records()):
        latest[(x['r']['person_id'], x['r']['induction_id'])] = x
    people = {}
    for (pid, _), x in latest.items():
        people[pid] = x['r']
    out = []
    for pid, pr in sorted(people.items(), key=lambda kv: kv[1]['person_name'].lower()):
        line = [pr['person_name'], pr['person_email'], pr['person_unikey'] or '', pr['person_role']]
        for row, _ in inds:
            x = latest.get((pid, row['id']))
            line.append('' if not x else x['status'][0] + (f' (until {local(x["r"]["expires_at"], "%d %b %Y")})'
                                                         if x['r']['expires_at'] else ''))
        out.append(line)
    db.audit(actor(me), 'export_register')
    return csv_response('induction-register.csv', ['Name', 'Email', 'UniKey', 'Role'] + [ind['title'] for _, ind in inds], out)


# ----------------------------------------------------------------- assessor: admin pages
@app.get('/assessors', response_class=HTMLResponse)
def assessors_page(request: Request):
    me = require(request)
    return render(request, 'assessors.html', me=me, assessors=db.q('SELECT * FROM assessors ORDER BY name'),
                  created=request.session.pop('created_assessor', None))


@app.post('/assessors')
def add_assessor(request: Request, name: str = Form(...), email: str = Form(...), csrf: str = Form(...)):
    me = require(request, csrf)
    email = email.strip().lower()
    if not EMAIL_RE.match(email) or db.one('SELECT 1 FROM assessors WHERE email = ?', email):
        return message(request, 'Cannot add assessor', 'Enter a valid email that is not already an assessor.', 400,
                       link='/assessors')
    pw = security.new_password()
    aid = db.run('INSERT INTO assessors (name, email, pw_hash, created_at) VALUES (?,?,?,?)',
                 ' '.join(name.split()), email, security.hash_password(pw), db.now())
    db.audit(actor(me), 'assessor_added', 'assessor', aid, email=email)
    request.session['created_assessor'] = {'name': name, 'email': email, 'password': pw}
    return RedirectResponse('/assessors', status_code=303)


@app.post('/assessors/{aid}/toggle')
def toggle_assessor(request: Request, aid: int, csrf: str = Form(...)):
    me = require(request, csrf)
    if aid == me['id']:
        return message(request, 'Not allowed', 'You cannot deactivate your own account.', 400, link='/assessors')
    db.run('UPDATE assessors SET active = 1 - active WHERE id = ?', aid)
    db.audit(actor(me), 'assessor_toggled', 'assessor', aid)
    return RedirectResponse('/assessors', status_code=303)


@app.get('/account', response_class=HTMLResponse)
def account_page(request: Request):
    me = require(request)
    return render(request, 'account.html', me=me, done=request.session.pop('pw_done', None))


@app.post('/account')
def change_password(request: Request, current: str = Form(...), new: str = Form(...), csrf: str = Form(...)):
    me = require(request, csrf)
    if not security.verify_password(current, me['pw_hash']) or len(new) < 10:
        return render(request, 'account.html', me=me, status_code=400,
                      error='Current password is wrong, or the new one is shorter than 10 characters.')
    db.run('UPDATE assessors SET pw_hash = ? WHERE id = ?', security.hash_password(new), me['id'])
    db.audit(actor(me), 'password_changed', 'assessor', me['id'])
    request.session['pw_done'] = True
    return RedirectResponse('/account', status_code=303)


@app.get('/emails', response_class=HTMLResponse)
def emails_page(request: Request):
    me = require(request)
    return render(request, 'emails.html', me=me, emails=db.q('SELECT * FROM outbox ORDER BY id DESC LIMIT 200'))


@app.get('/audit', response_class=HTMLResponse)
def audit_page(request: Request):
    me = require(request)
    return render(request, 'audit.html', me=me, rows=db.q('SELECT * FROM audit_log ORDER BY id DESC LIMIT 500'))


# ----------------------------------------------------------------- assessor: backup and restore
@app.get('/backup', response_class=HTMLResponse)
def backup_page(request: Request):
    me = require(request)
    return render(request, 'backup.html', me=me, current=backup.current_summary(),
                  copies=backup.safety_copies(), error=request.session.pop('backup_error', None))


@app.get('/backup/download')
def backup_download(request: Request):
    me = require(request)
    stamp = datetime.now(ZoneInfo(settings.timezone)).strftime('%Y-%m-%d-%H%M')
    path = backup.snapshot(backup.BACKUP_DIR / f'download-{security.new_token()[:12]}.db')
    db.audit(actor(me), 'backup_downloaded')
    return FileResponse(path, media_type='application/vnd.sqlite3', filename=f'spl-registry-{stamp}.db',
                        background=BackgroundTask(path.unlink, missing_ok=True))


@app.get('/backup/copies/{name}')
def backup_download_copy(request: Request, name: str):
    me = require(request)
    try:
        path = backup.backup_path(name)
    except backup.BackupError as e:
        raise HTTPException(404, str(e))
    db.audit(actor(me), 'safety_copy_downloaded', 'backup', name)
    return FileResponse(path, media_type='application/vnd.sqlite3', filename=f'spl-registry-{name}')


@app.post('/backup/upload')
async def backup_upload(request: Request, file: UploadFile, csrf: str = Form(...)):
    me = require(request, csrf)
    try:
        path = backup.save_upload(file.file)
        try:
            incoming = backup.validate(path)
        except backup.BackupError:
            path.unlink(missing_ok=True)
            raise
    except backup.BackupError as e:
        request.session['backup_error'] = str(e)
        return RedirectResponse('/backup', status_code=303)
    request.session['pending_restore'] = path.name
    return render(request, 'backup_confirm.html', me=me, incoming=incoming, current=backup.current_summary(),
                  source=file.filename or 'uploaded file', backup_name=path.name)


@app.post('/backup/copies/{name}/review')
def backup_review_copy(request: Request, name: str, csrf: str = Form(...)):
    me = require(request, csrf)
    try:
        incoming = backup.validate(backup.backup_path(name))
    except backup.BackupError as e:
        request.session['backup_error'] = str(e)
        return RedirectResponse('/backup', status_code=303)
    request.session['pending_restore'] = name
    return render(request, 'backup_confirm.html', me=me, incoming=incoming, current=backup.current_summary(),
                  source=f'safety copy {name}', backup_name=name)


@app.post('/backup/restore')
def backup_restore(request: Request, name: str = Form(...), confirm: str = Form(''), csrf: str = Form(...)):
    me = require(request, csrf)
    if name != request.session.get('pending_restore') or confirm != 'RESTORE':
        request.session['backup_error'] = 'Restore not confirmed. Type RESTORE to confirm.'
        return RedirectResponse('/backup', status_code=303)
    who = (me['id'], me['email'], me['name'])
    try:
        path = backup.backup_path(name)
        safety = backup.restore(path)
    except backup.BackupError as e:
        request.session['backup_error'] = str(e)
        return RedirectResponse('/backup', status_code=303)
    if name.startswith('upload-'):
        path.unlink(missing_ok=True)
    request.session.pop('pending_restore', None)
    db.audit(f'assessor:{who[2]}', 'database_restored', 'backup', name, safety_copy=safety)
    # Stay logged in only if the same account exists in the restored database.
    if not db.one('SELECT 1 FROM assessors WHERE id = ? AND email = ? AND active = 1', who[0], who[1]):
        request.session.clear()
        return message(request, 'Database restored',
                       f'The backup has been restored. Your account is not in it, so log in with an account from '
                       f'the backup. The previous database was saved as {safety}.', link='/login')
    request.session['flash'] = f'Database restored. The previous database was saved as {safety} (see below).'
    return RedirectResponse('/backup', status_code=303)


# ----------------------------------------------------------------- assessor: API keys and reference
@app.get('/api-keys', response_class=HTMLResponse)
def api_keys_page(request: Request):
    me = require(request)
    keys = db.q('SELECT k.*, a.name AS by_name FROM api_keys k LEFT JOIN assessors a ON a.id = k.created_by '
                'ORDER BY k.revoked_at IS NOT NULL, k.id DESC')
    return render(request, 'api_keys.html', me=me, keys=keys, created=request.session.pop('created_key', None))


@app.post('/api-keys')
def api_key_create(request: Request, name: str = Form(...), csrf: str = Form(...)):
    me = require(request, csrf)
    name = ' '.join(name.split())[:80]
    if len(name) < 2:
        return message(request, 'Name needed', 'Give the key a name, e.g. "Card access add-on".', 400, link='/api-keys')
    key, prefix, key_hash = api.new_key()
    kid = db.run('INSERT INTO api_keys (name, prefix, key_hash, created_by, created_at) VALUES (?,?,?,?,?)',
                 name, prefix, key_hash, me['id'], db.now())
    db.audit(actor(me), 'api_key_created', 'api_key', kid, name=name)
    request.session['created_key'] = {'name': name, 'key': key}
    return RedirectResponse('/api-keys', status_code=303)


@app.post('/api-keys/{kid}/revoke')
def api_key_revoke(request: Request, kid: int, csrf: str = Form(...)):
    me = require(request, csrf)
    db.run('UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL', db.now(), kid)
    db.audit(actor(me), 'api_key_revoked', 'api_key', kid)
    request.session['flash'] = 'API key revoked. Anything using it will now get "Invalid or revoked API key".'
    return RedirectResponse('/api-keys', status_code=303)


@app.get('/api-docs', response_class=HTMLResponse)
def api_docs(request: Request):
    me = require(request)
    _, res = access.catalogue()
    return render(request, 'api_docs.html', me=me, resources=sorted(res.values(), key=lambda r: r['id']))


@app.get('/healthz')
def healthz():
    db.one('SELECT 1')
    return {'ok': True}
