"""Pulling the bundle from the wiki. Run from the spl_registry folder: python -m pytest -q"""
import base64
import copy
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='spl-ind-test-'))

from app import db, wikisync  # noqa: E402
from app.config import Settings  # noqa: E402

SAMPLE = json.loads((HERE / 'sample_bundle.json').read_text())


class Wiki:
    """A stand-in for the wiki's fetch.php: Basic auth, ETag and 304s."""
    bundle = None
    calls = 0

    @classmethod
    def body(cls):
        return json.dumps(cls.bundle).encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        Wiki.calls += 1
        if self.headers.get('Authorization') != 'Basic ' + base64.b64encode(b'inductions-app:secret').decode():
            self.send_response(403)
            self.end_headers()
            return
        body = Wiki.body()
        etag = '"%s"' % Wiki.bundle['hash']
        if self.headers.get('If-None-Match') == etag:
            self.send_response(304)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('ETag', etag)
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope='module')
def wiki():
    srv = HTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{srv.server_port}/lib/exe/fetch.php?media=whs:inductions:registry.json'
    srv.shutdown()


def bundle(drop=None, tweak=None, h='wiki0001'):
    b = copy.deepcopy(SAMPLE)
    b['inductions'] = [i for i in b['inductions'] if i['id'] != drop]
    for i in b['inductions']:
        if i['id'] == tweak:
            i['version'] = 'w' + h
            i['sections'][0]['items'][0]['text'] += ' (edited on the wiki)'
    b['hash'] = h
    return b


def settings(url, password='secret'):
    return Settings(bundle_url=url, bundle_username='inductions-app', bundle_password=password)


def active():
    return {r['id']: r['version'] for r in db.q('SELECT id, version FROM inductions WHERE active = 1')}


def test_off_without_url():
    assert wikisync.check(Settings())['status'] == 'off'


def test_wrong_password(wiki):
    Wiki.bundle = bundle()
    r = wikisync.check(settings(wiki, 'nope'))
    assert r['status'] == 'error' and 'refused access (403)' in r['message']


def test_import_then_unchanged(wiki):
    Wiki.bundle = bundle(h='wiki0001')
    r = wikisync.check(settings(wiki))
    assert r['status'] in ('imported', 'unchanged'), r
    ids = set(active())
    assert {'IND_J11_N209B_SPACE', 'IND_J11_S120_FORM4', 'IND_J11_S120_SPACE'} <= ids
    r = wikisync.check(settings(wiki))
    assert r['status'] == 'unchanged'  # 304 from the ETag
    assert wikisync.state()['status'] == 'unchanged'


def test_edit_on_wiki_is_imported(wiki):
    Wiki.bundle = bundle(tweak='IND_J11_S120_FORM4', h='wiki0002')
    r = wikisync.check(settings(wiki))
    assert r['status'] == 'imported' and 'IND_J11_S120_FORM4' in r['changed']
    assert active()['IND_J11_S120_FORM4'] == 'wwiki0002'
    last = db.one('SELECT hash, uploaded_by FROM bundles ORDER BY id DESC LIMIT 1')
    assert last['hash'] == 'wiki0002' and last['uploaded_by'] is None
    assert db.one("SELECT 1 FROM audit_log WHERE action = 'bundle_synced'")


def test_dropping_an_induction_is_held_until_confirmed(wiki):
    Wiki.bundle = bundle(drop='IND_J11_S120_SPACE', tweak='IND_J11_S120_FORM4', h='wiki0003')
    r = wikisync.check(settings(wiki))
    assert r['status'] == 'held' and r['retired'] == ['IND_J11_S120_SPACE']
    assert 'IND_J11_S120_SPACE' in active()  # nothing retired yet
    assert wikisync.pending()['hash'] == 'wiki0003'
    # polling again does not import it either
    assert wikisync.check(settings(wiki))['status'] in ('unchanged', 'held')
    assert 'IND_J11_S120_SPACE' in active()
    r = wikisync.confirm_pending(None, 'Test Assessor')
    assert r['status'] == 'imported'
    assert 'IND_J11_S120_SPACE' not in active()
    assert wikisync.pending() is None
    assert db.one("SELECT 1 FROM audit_log WHERE action = 'inductions_retired'")


def test_invalid_bundle_is_not_imported(wiki):
    before = active()
    Wiki.bundle = {'format': 2, 'hash': 'bad'}
    r = wikisync.check(settings(wiki))
    assert r['status'] == 'error' and 'not valid' in r['message']
    assert active() == before


def test_page_load_check(wiki, monkeypatch):
    """The Dashboard, Registry and Wiki pages check the wiki as they load: additions are imported,
    a bundle that would retire something is held and reported."""
    import re
    from fastapi.testclient import TestClient
    from app import main, security
    monkeypatch.setattr(main, 'settings', settings(wiki))
    email = 'pageload@example.edu'
    if not db.one('SELECT 1 FROM assessors WHERE email = ?', email):
        db.run('INSERT INTO assessors (name, email, pw_hash, created_at) VALUES (?,?,?,?)',
               'Page Load', email, security.hash_password('page load pass 1'), db.now())
    c = TestClient(main.app)
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get('/login').text).group(1)
    c.post('/login', data={'email': email, 'password': 'page load pass 1', 'csrf': tok})
    page = c.get('/registry').text
    assert 'wiki-check' in page and 'Update Registry' in page
    tok = re.search(r'window.CSRF = "([^"]+)"', page).group(1)
    check = lambda: c.post('/wiki/check', headers={'X-CSRF-Token': tok}).json()  # noqa: E731
    assert c.post('/wiki/check').status_code == 400                        # needs the CSRF token

    # an edit on the wiki: imported straight away, and the reloaded page says so
    main._last_page_check[0] = 0
    Wiki.bundle = bundle(tweak='IND_J11_N209B_SPACE', h='wiki0101')
    assert check()['status'] == 'imported'
    assert 'Registry updated from the wiki' in c.get('/registry').text
    assert check()['status'] == 'recent'                                   # throttled
    main._last_page_check[0] = 0
    assert check()['status'] == 'unchanged'

    # a bundle that drops something: held, reported, nothing retired
    main._last_page_check[0] = 0
    Wiki.bundle = bundle(drop='IND_J11_N209B_SPACE', h='wiki0102')
    r = check()
    assert r['status'] == 'held' and r['held']['retired'] == ['IND_J11_N209B_SPACE']
    assert 'IND_J11_N209B_SPACE' in active()
    assert 'would retire IND_J11_N209B_SPACE' in c.get('/registry').text
    main._last_page_check[0] = 0
    assert check()['held']['retired'] == ['IND_J11_N209B_SPACE']           # still reported on the next load

    # Update Registry on the Registry page
    tok2 = re.search(r'name="csrf" value="([^"]+)"', c.get('/registry').text).group(1)
    r = c.post('/inductions/sync', data={'csrf': tok2, 'next': '/registry'}, follow_redirects=False)
    assert r.headers['location'] == '/registry'
    wikisync.confirm_pending(None, 'Test')
