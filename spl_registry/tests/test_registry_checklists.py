"""Checklists: the registry bundle (format 2), filling in, photos, outcomes, amendments.
Run from the spl_registry folder: python -m pytest -q

registry_bundle.json is real output of the wiki's splregistry plugin (one induction and a UAV
preflight checklist).
"""
import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path

import pytest
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='spl-reg-test-'))
os.environ.setdefault('SPL_BASE_URL', 'http://testserver')

from fastapi.testclient import TestClient  # noqa: E402

from app import checklists, db, logic, security  # noqa: E402
from app.main import app  # noqa: E402

BUNDLE = json.loads((HERE / 'registry_bundle.json').read_text())
SIG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='
CHK = 'CHK_UAV_PREFLIGHT'


def csrf(client, url):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(url).text).group(1)


def jpeg(with_exif=True, size=(2400, 1800)):
    img = Image.new('RGB', size, (200, 30, 30))
    buf = io.BytesIO()
    if with_exif:
        exif = Image.Exif()
        exif[0x8769] = {36867: '2026:11:12 09:30:00'}            # DateTimeOriginal
        exif[0x8825] = {1: 'S', 2: (33.0, 53.0, 20.0), 3: 'E', 4: (151.0, 11.0, 30.0)}  # GPS
        exif[0x010f] = 'PhoneMaker'
        img.save(buf, 'JPEG', exif=exif)
    else:
        img.save(buf, 'JPEG')
    return buf.getvalue()


@pytest.fixture(scope='module')
def c():
    client = TestClient(app)
    email = 'checklists@example.edu'
    if not db.one('SELECT 1 FROM assessors WHERE email = ?', email):
        db.run('INSERT INTO assessors (name, email, pw_hash, created_at) VALUES (?,?,?,?)',
               'Pat Pilot', email, security.hash_password('preflight pass 1'), db.now())
    r = client.post('/login', data={'email': email, 'password': 'preflight pass 1', 'csrf': csrf(client, '/login')})
    assert r.status_code == 200
    r = client.post('/inductions/upload', data={'csrf': csrf(client, '/wiki')},
                    files={'file': ('registry.json', json.dumps(BUNDLE).encode(), 'application/json')})
    assert 'Bundle loaded: 1 induction and 1 checklist' in r.text
    yield client
    # leave no active checklist behind for the wiki sync tests, which share the database
    db.run('UPDATE checklists SET active = 0')


def start(c):
    r = c.post(f'/checklists/{CHK}/start', data={'csrf': csrf(c, '/registry')}, follow_redirects=False)
    assert r.status_code == 303
    return int(r.headers['location'].rsplit('/', 1)[1])


def items():
    chk = next(x for x in BUNDLE['checklists'] if x['id'] == CHK)
    return {it['text']: it for it in checklists.answerable(chk)}


def test_bundle_format_2(c):
    assert logic.induction('IND_J11_S120_FORM4')[0] is not None
    row, chk = checklists.checklist(CHK)
    assert row['category'] == 'Preflight' and row['outcome'] == 'Go / No-go'
    page = c.get('/registry').text
    assert 'UAV preflight checklist' in page and 'Preflight' in page
    assert 'data-rtype="checklist"' in page and 'data-rtype="induction"' in page   # both on one page
    assert '1 induction and 1 checklist loaded' in c.get('/wiki').text


def test_submit_needs_every_answer(c):
    rid = start(c)
    page = c.get(f'/records/checklist/{rid}').text
    assert 'name="v_2.2"' in page and 'Limits: 25–25.2 V' in page
    r = c.post(f'/records/checklist/{rid}', data={'csrf': csrf(c, f'/records/checklist/{rid}'), 'action': 'submit'})
    assert r.status_code == 400
    # everything except the optional notes, plus the signature (Signature required: yes)
    assert '8 items still need an answer' in r.text and 'Type your name.' in r.text
    assert db.one('SELECT status FROM runs WHERE id = ?', rid)['status'] == 'draft'


def full_answers(it, token, **override):
    d = {'csrf': token, 'action': 'submit',
         f'v_{it["Conditions"]["id"]}': 'Clear',
         f'v_{it["Airframe serial"]["id"]}': 'SPL-UAV-001',
         f'v_{it["Props secure, no chips or cracks"]["id"]}': 'pass',
         f'v_{it["Flight battery voltage"]["id"]}': '25.1',
         f'v_{it["GPS satellites"]["id"]}': '12',
         f'v_{it["RTH altitude set"]["id"]}': 'on',
         f'v_{it["Flight log / notes"]["id"]}': '<p>Test <strong>flight</strong><script>alert(1)</script></p>',
         'signed_name': 'Pat Pilot', 'signature': SIG}
    d.update(override)
    return d


def test_photos_and_go(c):
    it = items()
    rid = start(c)
    photo_item = it['Airframe overall']['id']
    r = c.post(f'/records/checklist/{rid}', data={'csrf': csrf(c, f'/records/checklist/{rid}'), 'action': 'save', 'anchor': photo_item},
               files=[(f'photo_{photo_item}', ('IMG_0001.JPG', jpeg(), 'image/jpeg'))], follow_redirects=False)
    assert r.status_code == 303 and r.headers['location'].endswith(f'#i-{photo_item}')
    a = db.one('SELECT * FROM attachments WHERE run_id = ?', rid)
    assert a['item_id'] == photo_item and a['taken_at'] == '2026-11-12T09:30:00'
    assert max(a['width'], a['height']) == checklists.PHOTO_MAX_PX
    stored = Image.open(io.BytesIO(a['data']))
    assert not stored.getexif().get_ifd(0x8825) and 0x010f not in stored.getexif()   # GPS and camera dropped
    assert c.get(f'/attachments/{a["id"]}?thumb=1').headers['content-type'] == 'image/jpeg'

    r = c.post(f'/records/checklist/{rid}', data=full_answers(it, csrf(c, f'/records/checklist/{rid}')), follow_redirects=False)
    assert r.status_code == 303
    run = db.one('SELECT * FROM runs WHERE id = ?', rid)
    assert run['status'] == 'submitted' and run['outcome'] == 'pass' and run['outcome_label'] == 'Go'
    assert run['submitted_by'] == 'Pat Pilot'
    notes = json.loads(run['answers'])[it['Flight log / notes']['id']]['value']
    assert '<strong>flight</strong>' in notes and 'script' not in notes
    page = c.get(f'/records/checklist/{rid}').text
    assert '>Go</span>' in page and 'SPL-UAV-001' in page and 'alt="Signature of Pat Pilot"' in page
    # submitted: no more changes
    r = c.post(f'/records/checklist/{rid}', data=full_answers(it, csrf(c, f'/records/checklist/{rid}')))
    assert r.status_code == 400


def test_out_of_limits_is_no_go_and_needs_comment(c):
    it = items()
    rid = start(c)
    token = csrf(c, f'/records/checklist/{rid}')
    c.post(f'/records/checklist/{rid}', data={'csrf': token, 'action': 'save'},
           files=[(f'photo_{it["Airframe overall"]["id"]}', ('a.jpg', jpeg(False), 'image/jpeg'))])
    props = it['Props secure, no chips or cracks']['id']
    r = c.post(f'/records/checklist/{rid}', data=full_answers(it, token, **{f'v_{props}': 'fail',
                                                                f'v_{it["Flight battery voltage"]["id"]}': '24.3'}))
    assert r.status_code == 400 and 'Say what failed in the comment.' in r.text
    r = c.post(f'/records/checklist/{rid}', data=full_answers(it, token, **{f'v_{props}': 'fail', f'c_{props}': 'Chipped prop 3',
                                                                f'v_{it["Flight battery voltage"]["id"]}': '24.3'}),
               follow_redirects=False)
    assert r.status_code == 303
    run = db.one('SELECT * FROM runs WHERE id = ?', rid)
    assert run['outcome'] == 'fail' and run['outcome_label'] == 'No-go'
    page = c.get(f'/records/checklist/{rid}').text
    assert '2 items failed' in page and 'Out of limits' in page and 'Chipped prop 3' in page

    # amendment, then CSV
    r = c.post(f'/records/checklist/{rid}/amend', data={'csrf': token, 'reason': 'Typo', 'note': 'Voltage was 25.1 V'})
    assert 'Voltage was 25.1 V' in r.text
    assert json.loads(db.one('SELECT answers FROM runs WHERE id = ?', rid)['answers'])[
        it['Flight battery voltage']['id']]['value'] == '24.3'          # original answer untouched
    csv = c.get('/export/checklists.csv?outcome=fail').text
    assert 'No-go' in csv and 'Props secure' in csv and 'Go,' not in csv
    assert 'No-go' in c.get('/registry?view=checklists&category=preflight').text


def test_windy_choice_fails(c):
    it = items()
    rid = start(c)
    token = csrf(c, f'/records/checklist/{rid}')
    c.post(f'/records/checklist/{rid}', data={'csrf': token, 'action': 'save'},
           files=[(f'photo_{it["Airframe overall"]["id"]}', ('a.jpg', jpeg(False), 'image/jpeg'))])
    r = c.post(f'/records/checklist/{rid}', data=full_answers(it, token, **{f'v_{it["Conditions"]["id"]}': 'Windy'}),
               follow_redirects=False)
    assert r.status_code == 303
    assert db.one('SELECT outcome_label FROM runs WHERE id = ?', rid)['outcome_label'] == 'No-go'


def test_bad_photo_and_discard(c):
    rid = start(c)
    token = csrf(c, f'/records/checklist/{rid}')
    r = c.post(f'/records/checklist/{rid}', data={'csrf': token, 'action': 'save'},
               files=[('photo__run', ('notes.txt', b'not an image', 'text/plain'))])
    assert r.status_code == 400 and 'is not a photo this app can read' in r.text
    c.post(f'/records/checklist/{rid}', data={'csrf': token, 'action': 'save'},
           files=[('photo__run', ('ok.jpg', jpeg(False), 'image/jpeg'))])
    aid = db.one('SELECT id FROM attachments WHERE run_id = ? AND item_id IS NULL', rid)['id']
    c.post(f'/records/checklist/{rid}', data={'csrf': token, 'action': f'delete:{aid}'})
    assert not db.one('SELECT 1 FROM attachments WHERE id = ?', aid)
    c.post(f'/records/checklist/{rid}', data={'csrf': token, 'action': 'save'},
           files=[('photo__run', ('ok.jpg', jpeg(False), 'image/jpeg'))])
    r = c.post(f'/records/checklist/{rid}/discard', data={'csrf': token}, follow_redirects=False)
    assert r.status_code == 303
    assert not db.one('SELECT 1 FROM runs WHERE id = ?', rid)
    assert not db.one('SELECT 1 FROM attachments WHERE run_id = ?', rid)


def test_wiki_dropping_a_checklist_is_held(c):
    assert CHK in logic.active_record_ids()


def test_needs_login():
    anon = TestClient(app)
    for url in ('/registry', '/wiki', '/records/checklist/1', '/attachments/1', '/export/checklists.csv'):
        assert anon.get(url, follow_redirects=False).status_code in (303, 307)


def test_signature_required(c):
    it = items()
    rid = start(c)
    token = csrf(c, f'/records/checklist/{rid}')
    c.post(f'/records/checklist/{rid}', data={'csrf': token, 'action': 'save'},
           files=[(f'photo_{it["Airframe overall"]["id"]}', ('a.jpg', jpeg(False), 'image/jpeg'))])
    r = c.post(f'/records/checklist/{rid}', data=full_answers(it, token, signature=''))
    assert r.status_code == 400 and 'Draw your signature.' in r.text
    r = c.post(f'/records/checklist/{rid}', data=full_answers(it, token, signature='javascript:alert(1)'))
    assert r.status_code == 400                                  # not a PNG data URL: ignored
    assert c.post(f'/records/checklist/{rid}', data=full_answers(it, token), follow_redirects=False).status_code == 303
    assert db.one('SELECT signature FROM runs WHERE id = ?', rid)['signature'] == SIG


def test_induction_signature_comes_from_the_page(c):
    _, ind = logic.induction('IND_J11_S120_FORM4')
    assert not ind.get('signature_required')


def test_save_and_close(c):
    rid = start(c)
    token = csrf(c, f'/records/checklist/{rid}')
    r = c.post(f'/records/checklist/{rid}', data={'csrf': token, 'action': 'save'}, follow_redirects=False)
    assert r.headers['location'] == f'/records/checklist/{rid}'
    assert 'Draft saved at' in c.get(f'/records/checklist/{rid}').text
    r = c.post(f'/records/checklist/{rid}', data={'csrf': token, 'action': 'save_close'}, follow_redirects=False)
    assert r.headers['location'] == '/registry'
    page = c.get('/registry').text
    assert 'Draft of UAV preflight checklist saved at' in page and f'/records/checklist/{rid}' in page   # listed under drafts


def test_custom_outcome_words():
    items = [{'id': '1.1', 'kind': 'pass', 'text': 'Frame intact', 'refs': []}]
    chk = {'id': 'X', 'sections': [{'title': 'S', 'items': items}], 'outcome': ['Airworthy', 'Grounded']}
    assert checklists.evaluate(chk, {'1.1': {'value': 'pass'}}, {})[2:] == ('pass', 'Airworthy')
    assert checklists.evaluate(chk, {'1.1': {'value': 'fail'}}, {})[2:] == ('fail', 'Grounded')
    chk['outcome'] = ['Complete']                     # one word: always that, even with a fail
    assert checklists.evaluate(chk, {'1.1': {'value': 'fail'}}, {})[2:] == ('complete', 'Complete')
    assert checklists.outcome_label('fail', 'Grounded') == ('Grounded', 'bad')
    assert checklists.outcome_label('pass', 'Airworthy') == ('Airworthy', 'ok')
    # bundles that still say go-nogo (from before pages named their results) keep working
    assert logic.outcome_words({'outcome': 'go-nogo'}) == ['Go', 'No-go']
    for bad in (['A', 'B', 'C'], [], ['']):
        with pytest.raises(logic.BundleError):
            logic.validate_bundle({'format': 2, 'checklists': [dict(chk, version='1', title='T', outcome=bad)]})


def test_checklist_record_page_and_delete(c):
    it = items()
    rid = start(c)
    token = csrf(c, f'/records/checklist/{rid}')
    c.post(f'/records/checklist/{rid}', data={'csrf': token, 'action': 'photo'},
           files=[(f'photo_{it["Airframe overall"]["id"]}', ('a.jpg', jpeg(False), 'image/jpeg'))])
    assert c.post(f'/records/checklist/{rid}', data=full_answers(it, token), follow_redirects=False).status_code == 303
    page = c.get(f'/records/checklist/{rid}').text
    assert 'Checklist record' in page and '&larr; Registry' in page and 'Delete this record' in page
    # old address
    assert c.get(f'/runs/{rid}', follow_redirects=False).headers['location'] == f'/records/checklist/{rid}'
    # a reason is needed
    r = c.post(f'/records/checklist/{rid}/delete', data={'csrf': token, 'reason': ''})
    assert r.status_code == 400 and db.one('SELECT 1 FROM runs WHERE id = ?', rid)
    r = c.post(f'/records/checklist/{rid}/delete', data={'csrf': token, 'reason': 'Wrong aircraft'},
               follow_redirects=False)
    assert r.headers['location'] == '/registry?view=checklists#records'
    assert not db.one('SELECT 1 FROM runs WHERE id = ?', rid)
    assert not db.one('SELECT 1 FROM attachments WHERE run_id = ?', rid)
    note = db.one("SELECT details FROM audit_log WHERE action = 'checklist_deleted' AND entity_id = ?", str(rid))
    assert 'Wrong aircraft' in note['details'] and 'UAV preflight checklist' in note['details']
