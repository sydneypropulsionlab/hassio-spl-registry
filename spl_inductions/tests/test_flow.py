"""End-to-end test of the induction flow. Run from the spl_inductions folder: python -m pytest -q"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='spl-ind-test-')
os.environ['SPL_BASE_URL'] = 'http://testserver'
os.environ['SPL_CERTIFICATE_CC'] = 'whs@example.edu, dries@example.edu'

from fastapi.testclient import TestClient  # noqa: E402

from app import db, logic  # noqa: E402
from app.main import app  # noqa: E402

BUNDLE = (HERE / 'sample_bundle.json').read_bytes()


def csrf(client, url):
    html = client.get(url).text
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


@pytest.fixture(scope='module')
def assessor():
    c = TestClient(app)
    assert c.get('/', follow_redirects=False).headers['location'] == '/setup'
    r = c.post('/setup', data={'name': 'Brock Cooper', 'email': 'Brock@example.edu', 'password': 'correct horse 1',
                               'csrf': csrf(c, '/setup')})
    assert r.status_code == 200 and 'Hello, Brock' in r.text
    return c


def test_bad_bundle_rejected(assessor):
    r = assessor.post('/inductions/upload', data={'csrf': csrf(assessor, '/inductions')},
                      files={'file': ('x.json', b'{"format": 2}', 'application/json')})
    assert 'Not an induction bundle' in r.text


def test_full_flow(assessor):
    c = assessor
    r = c.post('/inductions/upload', data={'csrf': csrf(c, '/inductions')},
               files={'file': ('inductions.json', BUNDLE, 'application/json')})
    assert 'Bundle loaded: 3 inductions (3 new, 0 updated)' in r.text

    token = csrf(c, '/inductions')
    r = c.post('/sessions', data={'induction_id': 'IND_J11_S120_FORM4', 'csrf': token})
    sid = int(str(r.url).rstrip('/').split('/')[-1])
    assert '<svg' in r.text and 'Scan to join' in r.text
    code = db.one('SELECT code FROM sessions WHERE id = ?', sid)['code']
    _, ind = logic.induction('IND_J11_S120_FORM4')

    # Two inductees join on their phones (a group session).
    phones = []
    for name, email, uk in [('Alex Lee', 'alex@example.edu', 'alee0001'), ('Sam Chen', 'SAM@example.edu', 'SCHE0002')]:
        p = TestClient(app)
        r = p.post(f'/j/{code}', data={'name': name, 'email': email, 'unikey': uk, 'role': 'Student',
                                       'csrf': csrf(p, f'/j/{code}')})
        assert 'Follow along with your assessor' in r.text
        phones.append((p, str(r.url).split('/p/')[1]))
    assert db.one('SELECT email FROM people WHERE name = ?', 'Sam Chen')['email'] == 'sam@example.edu'

    # Rejoining with the same email returns the same record.
    p0, tok0 = phones[0]
    r = p0.post(f'/j/{code}', data={'name': 'Alex Lee', 'email': 'alex@example.edu', 'unikey': 'alee0001',
                                    'csrf': csrf(p0, f'/j/{code}')})
    assert str(r.url).endswith(tok0)

    # Signing before finishing is refused.
    r = p0.post(f'/p/{tok0}/sign', data={'typed_name': 'Alex Lee', 'declaration': 'yes'})
    assert r.status_code == 400

    # Assessor ticks the covered items (once for the group), without CSRF header it fails.
    assert c.post(f'/sessions/{sid}/tick', json={'item': '1.1', 'value': True}).status_code == 400
    hdr = {'x-csrf-token': token}
    for item in logic.ids_of(ind, 'check'):
        assert c.post(f'/sessions/{sid}/tick', json={'item': item, 'value': True}, headers=hdr).status_code == 200
    assert c.post(f'/sessions/{sid}/tick', json={'item': 'nope', 'value': True}, headers=hdr).status_code == 400

    rids = [db.one('SELECT id FROM records WHERE participant_token = ?', t)['id'] for _, t in phones]
    for rid in rids:
        for item in logic.ids_of(ind, 'demo'):
            assert c.post(f'/records/{rid}/demo', json={'item': item, 'value': True}, headers=hdr).status_code == 200

    # Live participants panel renders.
    panel = c.get(f'/sessions/{sid}/participants').text
    assert 'Alex Lee' in panel and 'Sam Chen' in panel

    # Inductees tick acknowledgements and sign.
    for p, tok in phones:
        st = p.get(f'/p/{tok}/state').json()
        assert not st['complete']
        for item in logic.ids_of(ind, 'ack'):
            assert p.post(f'/p/{tok}/ack', json={'item': item, 'value': True}).status_code == 200
        assert p.get(f'/p/{tok}/state').json()['complete']
    r = p0.post(f'/p/{tok0}/sign', data={'typed_name': 'alex  lee', 'declaration': 'yes'})
    assert 'Signed. Thank you.' in r.text
    p1, tok1 = phones[1]
    r = p1.post(f'/p/{tok1}/sign', data={'typed_name': 'Somebody Else', 'declaration': 'yes'})
    assert r.status_code == 400
    p1.post(f'/p/{tok1}/sign', data={'typed_name': 'Sam Chen', 'declaration': 'yes'})

    # Acks are locked once signed.
    assert p0.post(f'/p/{tok0}/ack', json={'item': logic.ids_of(ind, 'ack')[0], 'value': False}).status_code == 400

    # Assessor signs off Alex with a custom refresh period.
    r = c.post(f'/records/{rids[0]}/signoff', data={'refresh_months': 6, 'notes': 'Supervised for first print',
                                                    'declaration': 'yes', 'csrf': token})
    assert 'not sent' in r.text  # no SMTP in tests: logged to the outbox
    mail = db.one("SELECT * FROM outbox WHERE to_addr = 'alex@example.edu' ORDER BY id DESC")
    link = re.search(r'http://testserver(/c/\S+)', mail['body']).group(1)
    assert 'Check your email' in p0.get(f'/p/{tok0}').text

    # A GET on the link (as an email scanner would do) does not confirm.
    r = TestClient(app).get(link)
    assert 'I confirm I completed this induction' in r.text
    assert db.one('SELECT status FROM records WHERE id = ?', rids[0])['status'] == 'awaiting_confirmation'
    r = TestClient(app).post(link)
    assert 'Induction confirmed' in r.text
    rec = db.one('SELECT * FROM records WHERE id = ?', rids[0])
    assert rec['status'] == 'complete' and rec['expires_at'] and rec['confirm_token_hash'] is None
    assert logic.add_months(logic.parse(rec['assessor_signed_at']), 6).isoformat(timespec='seconds') == rec['expires_at']
    assert TestClient(app).post(link).status_code == 404  # single use
    cert = db.one("SELECT * FROM outbox WHERE subject LIKE 'Induction certificate%' AND to_addr = 'alex@example.edu'")
    assert cert['cc'] == 'whs@example.edu, dries@example.edu'
    assert cert['attachments'].startswith('SPL-certificate-Alex-Lee-Formlabs-Form-4-induction-')
    assert 'Valid for: 6 months' in cert['body'] and 'Renewal due:' in cert['body']

    # Record page, register, CSV exports.
    page = c.get(f'/records/{rids[0]}').text
    assert 'Current' in page and 'Supervised for first print' in page and 'Brock Cooper' in page
    reg = c.get('/register').text
    assert 'Alex Lee' in reg and 'Sam Chen' in reg and 'Ready for sign-off' in reg
    csv_text = c.get('/export/records.csv').text
    assert 'Alex Lee,alex@example.edu,alee0001,Student,IND_J11_S120_FORM4' in csv_text
    assert 'SWP J11 S120 Form 4 v1' in csv_text
    assert 'Alex Lee' in c.get('/export/register.csv').text

    # Cancel Sam's record; it stays but is marked cancelled.
    c.post(f'/records/{rids[1]}/cancel', data={'csrf': token})
    assert db.one('SELECT status FROM records WHERE id = ?', rids[1])['status'] == 'cancelled'

    # An updated SWP flags the confirmed record.
    data = json.loads(BUNDLE)
    for i in data['inductions']:
        if i['id'] == 'IND_J11_S120_FORM4':
            i['sources'][0]['hash'] = 'changed00000'
            i['version'] = 'v2-test'
    r = c.post('/inductions/upload', data={'csrf': token},
               files={'file': ('inductions.json', json.dumps(data).encode(), 'application/json')})
    assert '1 updated' in r.text
    assert 'SWP updated since induction' in c.get(f'/records/{rids[0]}').text

    # Pages all render.
    for url in ['/dashboard', '/inductions', '/inductions/IND_J11_S120_SPACE', f'/sessions/{sid}', '/assessors',
                '/emails', '/audit', '/account', f'/people/{rec["person_id"]}', '/register?type=space']:
        assert c.get(url).status_code == 200, url


def test_join_code_expiry_and_close(assessor):
    c = assessor
    token = csrf(c, '/inductions')
    r = c.post('/sessions', data={'induction_id': 'IND_J11_S120_SPACE', 'csrf': token})
    sid = int(str(r.url).split('/')[-1])
    code = db.one('SELECT code FROM sessions WHERE id = ?', sid)['code']
    db.run("UPDATE sessions SET join_until = '2000-01-01T00:00:00+00:00' WHERE id = ?", sid)
    assert TestClient(app).get(f'/j/{code}').status_code == 404
    assert 'Joining has closed' in c.get(f'/sessions/{sid}').text
    c.post(f'/sessions/{sid}/extend', data={'csrf': token})
    assert TestClient(app).get(f'/j/{code}').status_code == 200
    c.post(f'/sessions/{sid}/close', data={'csrf': token})
    assert TestClient(app).get(f'/j/{code}').status_code == 404


def test_assessors_and_auth(assessor):
    c = assessor
    token = csrf(c, '/assessors')
    r = c.post('/assessors', data={'name': 'Dries Verstraete', 'email': 'dries@example.edu', 'csrf': token})
    pw = re.search(r'class="pw">([^<]+)<', r.text).group(1)
    d = TestClient(app)
    r = d.post('/login', data={'email': 'dries@example.edu', 'password': 'wrong', 'csrf': csrf(d, '/login')})
    assert r.status_code == 401
    r = d.post('/login', data={'email': 'DRIES@example.edu', 'password': pw, 'csrf': csrf(d, '/login')})
    assert 'Hello, Dries' in r.text
    # Public cannot reach assessor pages.
    anon = TestClient(app)
    for url in ['/dashboard', '/register', '/export/records.csv', '/audit']:
        assert anon.get(url, follow_redirects=False).headers['location'] == '/login'
    # Deactivated assessors are logged out.
    aid = db.one("SELECT id FROM assessors WHERE email = 'dries@example.edu'")['id']
    c.post(f'/assessors/{aid}/toggle', data={'csrf': token})
    assert d.get('/dashboard', follow_redirects=False).status_code == 303
    # CSRF is enforced on forms.
    assert c.post('/sessions', data={'induction_id': 'IND_J11_S120_SPACE', 'csrf': 'bad'}).status_code == 400


def test_manual_record(assessor):
    c = assessor
    token = csrf(c, '/records/new')
    form = {'name': 'Pat Old', 'email': 'pat@example.edu', 'unikey': 'pold0003', 'role': 'Researcher', 'induction_id': 'IND_J11_N209B_SPACE',
            'induction_date': '2099-01-01', 'assessor_id': 'other', 'assessor_text': '', 'refresh_months': '',
            'notes': 'Google Form response', 'csrf': token}
    r = c.post('/records/new', data=form)
    assert r.status_code == 400 and 'cannot be in the future' in r.text and 'type their name' in r.text

    # Back-dated beyond its refresh period: added as complete but already expired.
    form.update(induction_date='2023-03-15', assessor_text='Former Staff')
    r = c.post('/records/new', data=form)
    assert 'Record added for Pat Old' in r.text and 'manual entry' in r.text and 'Former Staff' in r.text
    assert 'Expired' in r.text and '15 Mar 2023' in r.text and 'Checklist</h2>' not in r.text
    rec = db.one("SELECT * FROM records WHERE notes = 'Google Form response'")
    assert rec['status'] == 'complete' and rec['entry_method'] == 'manual' and rec['assessor_id'] is None
    assert rec['expires_at'].startswith('2025-03-15')  # default 24 months from the induction file
    assert not db.one("SELECT 1 FROM outbox WHERE to_addr = 'pat@example.edu'")  # no email for manual entries

    # Same person, induction and date again is refused.
    r = c.post('/records/new', data=form)
    assert r.status_code == 400 and 'already has a' in r.text

    # A more recent manual record with a named assessor and no expiry becomes the current one in the register.
    aid = db.one("SELECT id FROM assessors WHERE email = 'brock@example.edu'")['id']
    form.update(induction_date='2026-01-10', assessor_id=str(aid), refresh_months='0', notes='')
    r = c.post('/records/new', data=form)
    assert 'Current' in r.text and 'Brock Cooper' in r.text
    reg = c.get('/register').text
    row = reg[reg.index('Pat Old'):]
    row = row[:row.index('</tr>')]
    assert 'Current' in row and 'Expired' not in row

    # Manual entries can be cancelled (to fix mistakes); the CSV marks them.
    rid = db.one("SELECT id FROM records WHERE person_id = ? AND refresh_months = 0", rec['person_id'])['id']
    c.post(f'/records/{rid}/cancel', data={'csrf': token})
    assert db.one('SELECT status FROM records WHERE id = ?', rid)['status'] == 'cancelled'
    assert 'Manual entry' in c.get('/export/records.csv').text
    assert 'Pat Old' in c.get(f'/records/new?person={rec["person_id"]}').text


def test_migration_adds_columns(tmp_path):
    import sqlite3
    c = sqlite3.connect(tmp_path / 'old.db')
    c.execute('CREATE TABLE records (id INTEGER PRIMARY KEY, status TEXT)')
    c.execute("INSERT INTO records (status) VALUES ('complete')")
    db._migrate(c)
    cols = {r[1] for r in c.execute('PRAGMA table_info(records)')}
    assert {'entry_method', 'assessor_text', 'entered_by'} <= cols
    assert c.execute('SELECT entry_method FROM records').fetchone()[0] == 'app'


def test_edit_and_delete(assessor):
    c = assessor
    token = csrf(c, '/records/new')
    c.post('/records/new', data={'name': 'Edit Me', 'email': 'edit@example.edu', 'unikey': 'emee0004', 'role': 'Student',
                                 'induction_id': 'IND_J11_N209B_SPACE', 'induction_date': '2025-05-01',
                                 'assessor_id': 'other', 'assessor_text': 'Old Staff', 'refresh_months': '12',
                                 'notes': '', 'csrf': token})
    rec = db.one("SELECT * FROM records WHERE assessor_text = 'Old Staff'")
    rid, pid = rec['id'], rec['person_id']
    assert 'Edit record' in c.get(f'/records/{rid}/edit').text

    # A reason is required.
    base = {'induction_id': 'IND_J11_N209B_SPACE', 'induction_date': '2025-06-02', 'assessor_id': 'other',
            'assessor_text': 'Old Staff', 'refresh_months': '24', 'notes': 'fixed', 'csrf': token}
    r = c.post(f'/records/{rid}/edit', data={**base, 'reason': ''})
    assert r.status_code == 400 and 'Give a reason' in r.text

    r = c.post(f'/records/{rid}/edit', data={**base, 'reason': 'Wrong date typed'})
    assert 'Record updated' in r.text and 'Reason: Wrong date typed' in r.text
    assert 'Induction date: 01 May 2025 → 02 Jun 2025' in r.text and 'Refresh months: 12 → 24' in r.text
    rec = db.one('SELECT * FROM records WHERE id = ?', rid)
    assert rec['expires_at'].startswith('2027-06-02') and rec['notes'] == 'fixed'

    # Saving with no changes does nothing.
    r = c.post(f'/records/{rid}/edit', data={**base, 'reason': 'again'})
    assert 'Nothing changed' in r.text

    # App records: induction can't be switched even if posted; the date and assessor can be corrected.
    app_rec = db.one("SELECT * FROM records WHERE entry_method = 'app' AND status = 'complete'")
    r = c.post(f'/records/{app_rec["id"]}/edit', data={**base, 'induction_id': 'IND_J11_N209B_SPACE',
                                                       'assessor_id': str(app_rec['assessor_id']),
                                                       'reason': 'Correct the date'})
    assert db.one('SELECT induction_id FROM records WHERE id = ?', app_rec['id'])[0] == app_rec['induction_id']
    assert 'Checklist</h2>' in r.text

    # Unfinished records can't be edited.
    pending = db.one("SELECT id FROM records WHERE status = 'cancelled'")
    assert c.get(f'/records/{pending["id"]}/edit').status_code == 400

    # Person details: email clash refused, otherwise updated.
    r = c.post(f'/people/{pid}/edit', data={'name': 'Edit Me', 'email': 'alex@example.edu', 'unikey': 'emee0004',
                                             'role': '', 'csrf': token})
    assert r.status_code == 400 and 'already has that email' in r.text
    r = c.post(f'/people/{pid}/edit', data={'name': 'Edited Person', 'email': 'edited@example.edu', 'unikey': 'emee0004',
                                             'role': 'Staff', 'csrf': token})
    assert 'Details updated' in r.text and 'Edited Person' in r.text

    # Person with records can't be deleted; delete needs a reason; then the record is gone but logged.
    assert c.post(f'/people/{pid}/delete', data={'csrf': token}).status_code == 400
    assert c.post(f'/records/{rid}/delete', data={'reason': '', 'csrf': token}).status_code == 400
    r = c.post(f'/records/{rid}/delete', data={'reason': 'Duplicate entry', 'csrf': token})
    assert f'Record {rid}' in r.text and 'deleted' in r.text
    assert db.one('SELECT 1 FROM records WHERE id = ?', rid) is None
    log = db.one("SELECT * FROM audit_log WHERE action = 'record_deleted' AND entity_id = ?", str(rid))
    assert 'Duplicate entry' in log['details'] and 'Edited Person' in log['details']
    assert c.get(f'/records/{rid}').status_code == 404

    # Now the person has no records and can be deleted.
    r = c.post(f'/people/{pid}/delete', data={'csrf': token})
    assert 'deleted' in r.text and db.one('SELECT 1 FROM people WHERE id = ?', pid) is None


def test_backup_and_restore(assessor):
    c = assessor
    token = csrf(c, '/backup')
    page = c.get('/backup').text
    assert 'Download backup' in page and 'None yet' in page

    r = c.get('/backup/download')
    assert r.status_code == 200 and r.content[:16] == b'SQLite format 3\x00'
    assert 'spl-inductions-' in r.headers['content-disposition']
    snapshot = r.content
    people_before = db.one('SELECT COUNT(*) FROM people')[0]

    # Change something after the backup.
    db.run("INSERT INTO people (name, email, role, created_at, updated_at) VALUES "
           "('After Backup', 'after@example.edu', '', '2026-01-01', '2026-01-01')")

    # Junk and non-app databases are refused.
    r = c.post('/backup/upload', data={'csrf': token}, files={'file': ('x.db', b'not a database', 'application/octet-stream')})
    assert 'not a database backup' in r.text
    import sqlite3, tempfile
    other = Path(tempfile.mkdtemp()) / 'other.db'
    sqlite3.connect(other).execute('CREATE TABLE t (x)').connection.commit()
    r = c.post('/backup/upload', data={'csrf': token}, files={'file': ('o.db', other.read_bytes(), 'application/octet-stream')})
    assert 'not an SPL Inductions backup' in r.text

    # Upload shows a comparison; restore needs RESTORE typed.
    r = c.post('/backup/upload', data={'csrf': token}, files={'file': ('b.db', snapshot, 'application/octet-stream')})
    assert 'Restore this backup?' in r.text and 'Current database' in r.text
    name = re.search(r'name="name" value="([^"]+)"', r.text).group(1)
    r = c.post('/backup/restore', data={'name': name, 'confirm': 'yes', 'csrf': token})
    assert 'Type RESTORE' in r.text and db.one("SELECT 1 FROM people WHERE email = 'after@example.edu'")

    r = c.post('/backup/restore', data={'name': name, 'confirm': 'RESTORE', 'csrf': token})
    assert 'Database restored' in r.text and 'before-restore-' in r.text
    assert db.one('SELECT COUNT(*) FROM people')[0] == people_before
    assert db.one("SELECT 1 FROM people WHERE email = 'after@example.edu'") is None
    assert db.one("SELECT 1 FROM audit_log WHERE action = 'database_restored'")

    # Undo using the safety copy.
    copy = re.search(r'/backup/copies/(before-restore-[\d-]+\.db)', r.text).group(1)
    assert c.get(f'/backup/copies/{copy}').content[:6] == b'SQLite'
    r = c.post(f'/backup/copies/{copy}/review', data={'csrf': token})
    assert 'Restore this backup?' in r.text
    r = c.post('/backup/restore', data={'name': copy, 'confirm': 'RESTORE', 'csrf': token})
    assert db.one("SELECT 1 FROM people WHERE email = 'after@example.edu'")

    # Path tricks are refused.
    assert c.get('/backup/copies/..%2Finductions.db').status_code == 404
    assert TestClient(app).get('/backup/download', follow_redirects=False).headers['location'] == '/login'


def test_restore_at_setup(assessor, monkeypatch, tmp_path):
    from app import backup
    snapshot = assessor.get('/backup/download').content
    people = db.one('SELECT COUNT(*) FROM people')[0]
    # Switch to a brand-new empty database, as on a fresh install.
    monkeypatch.setattr(db, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(backup, 'BACKUP_DIR', tmp_path / 'backups')
    db.reset_for_tests()
    try:
        c = TestClient(app)
        assert c.get('/', follow_redirects=False).headers['location'] == '/setup'
        token = csrf(c, '/setup')
        assert 'Or restore from a backup' in c.get('/setup').text
        r = c.post('/setup/restore', data={'csrf': token}, files={'file': ('x.db', b'junk', 'application/octet-stream')})
        assert r.status_code == 400 and 'not a database backup' in r.text
        r = c.post('/setup/restore', data={'csrf': token}, files={'file': ('b.db', snapshot, 'application/octet-stream')})
        assert 'Restore this backup?' in r.text and 'Current database' not in r.text
        name = re.search(r'name="name" value="([^"]+)"', r.text).group(1)
        r = c.post('/setup/restore/confirm', data={'name': name, 'confirm': 'RESTORE', 'csrf': token})
        assert 'Backup restored' in r.text
        assert db.one('SELECT COUNT(*) FROM people')[0] == people
        # Existing accounts work straight away; setup is no longer offered.
        r = c.post('/login', data={'email': 'brock@example.edu', 'password': 'correct horse 1', 'csrf': csrf(c, '/login')})
        assert 'Hello, Brock' in r.text
        assert c.post('/setup/restore', data={'csrf': token},
                      files={'file': ('b.db', snapshot, 'application/octet-stream')}, follow_redirects=False).status_code == 303
    finally:
        db.reset_for_tests()


def test_certificate_download_and_resend(assessor):
    c = assessor
    rec = db.one("SELECT r.id FROM records r JOIN people p ON p.id = r.person_id "
                 "WHERE p.email = 'alex@example.edu' AND r.status = 'complete'")
    r = c.get(f'/records/{rec["id"]}/certificate.pdf')
    assert r.status_code == 200 and r.content[:5] == b'%PDF-' and 'SPL-certificate-' in r.headers['content-disposition']
    token = csrf(c, f'/records/{rec["id"]}')
    r = c.post(f'/records/{rec["id"]}/certificate/send', data={'csrf': token})
    assert 'Certificate email not sent' in r.text  # no SMTP in tests; logged in the outbox
    assert 'CC: whs@example.edu' in c.get('/emails').text
    # Space inductions show their room access; unfinished records have no certificate.
    from app import certificate as cert_mod
    from app.main import certificate_data, record_with_person
    data = certificate_data(record_with_person(rec['id']))
    months = db.one('SELECT refresh_months FROM records WHERE id = ?', rec['id'])[0]
    assert data['grants_access'] == ['Form 4 3D Printers'] and data['valid_for'] == cert_mod.months_text(months)
    pending = db.one("SELECT id FROM records WHERE status = 'cancelled'")
    assert c.get(f'/records/{pending["id"]}/certificate.pdf').status_code == 400
    assert cert_mod.months_text(24) == '2 years' and cert_mod.months_text(0) == 'No expiry'


def test_compare_with_current(assessor):
    import copy
    from app import diff
    c = assessor
    rec = db.one("SELECT r.* FROM records r JOIN people p ON p.id = r.person_id "
                 "WHERE p.email = 'alex@example.edu' AND r.status = 'complete'")
    old = json.loads(rec['snapshot'])
    new = copy.deepcopy(old)
    new['version'] = 'v3-compare'
    sec = new['sections'][1]['items']
    removed = sec.pop(0)                                       # removed item
    sec[0]['text'] = sec[0]['text'].replace('sensitisation', 'skin sensitisation')  # reworded
    sec.append({'id': '2.99', 'kind': 'ack', 'text': 'I will wear nitrile gloves at all times', 'refs': []})  # added
    new['sections'][0]['items'][0]['refs'][0]['text'] += ' Extra SWP sentence.'   # SWP excerpt changed
    new['sources'][0]['version'] = '2'
    new['sources'][0]['hash'] = 'abcdef123456'
    new['refresh_months'] = 18

    d = diff.compare(old, new)
    assert d['counts'] == {'added': 1, 'removed': 1, 'changed': 1, 'refs': 1}
    assert d['sources'][0]['version_changed'] and d['fields'][0]['label'] == 'Default refresh (months)'
    assert '<ins>skin </ins>' in str(diff.words('sensitisation', 'skin sensitisation')[1])
    assert diff.compare(old, old)['any'] is False

    # Load it as the current version and check the record page and compare page.
    bundle = json.loads(BUNDLE)
    bundle['inductions'] = [new if i['id'] == new['id'] else i for i in bundle['inductions']]
    token = csrf(c, '/inductions')
    c.post('/inductions/upload', data={'csrf': token},
           files={'file': ('inductions.json', json.dumps(bundle).encode(), 'application/json')})
    page = c.get(f'/records/{rec["id"]}').text
    assert 'has changed since this record was signed' in page and 'Compare with current' in page
    r = c.get(f'/records/{rec["id"]}/compare')
    assert r.status_code == 200
    assert '+1 added' in r.text and '1 removed' in r.text and '1 reworded' in r.text
    assert removed['text'] in r.text and 'I will wear nitrile gloves at all times' in r.text
    assert 'cause<ins> skin</ins> sensitisation' in r.text
    assert 'Extra SWP sentence.' in r.text and 'Version 1 → 2' in r.text
    # Comparing against the record's own version shows no differences.
    r = c.get(f'/records/{rec["id"]}/compare?against={rec["induction_version"]}')
    assert 'No differences' in r.text


def test_grants_access_names():
    from app import logic
    assert logic.grants_access({'type': 'space', 'grants_access': ['J11 N209B', 'Wind tunnel area']}) == ['J11 N209B', 'Wind tunnel area']
    assert logic.grants_access({'type': 'space', 'room_access': ['J11 S120']}) == ['J11 S120']  # older bundles
    assert logic.grants_access({'type': 'equipment', 'grants_access': ['Formlabs Form 4']}) == ['Formlabs Form 4']
    b = json.loads(BUNDLE)
    assert [i['grants_access'] for i in b['inductions'] if i['id'] == 'IND_J11_N209B_SPACE'] == [['J11 N209B']]


def _manual(c, token, name, email, unikey, induction, day, months='12'):
    return c.post('/records/new', data={'name': name, 'email': email, 'unikey': unikey, 'role': 'Student',
                                        'induction_id': induction, 'induction_date': day, 'assessor_id': 'other',
                                        'assessor_text': 'Old Staff', 'refresh_months': months, 'notes': '',
                                        'csrf': token})


def test_unikey_identity_rules(assessor):
    c = assessor
    token = csrf(c, '/records/new')
    # A UniKey is required and checked.
    r = _manual(c, token, 'No Key', 'nokey@example.edu', '', 'IND_J11_N209B_SPACE', '2026-01-05')
    assert r.status_code == 400 and 'Enter their UniKey' in r.text
    r = _manual(c, token, 'Bad Key', 'bad@example.edu', '1234abcd', 'IND_J11_N209B_SPACE', '2026-01-05')
    assert r.status_code == 400
    # A legacy person without a UniKey gets it filled in by email.
    db.run("INSERT INTO people (name, email, role, created_at, updated_at) VALUES "
           "('Legacy Person', 'legacy@example.edu', '', '2026-01-01', '2026-01-01')")
    r = _manual(c, token, 'Legacy Person', 'legacy@example.edu', 'LEGA0010', 'IND_J11_N209B_SPACE', '2026-01-05')
    assert 'Record added' in r.text
    assert db.one("SELECT unikey FROM people WHERE email = 'legacy@example.edu'")['unikey'] == 'lega0010'
    # Same UniKey with a different email is refused, not merged.
    r = _manual(c, token, 'Legacy Person', 'other@example.edu', 'lega0010', 'IND_J11_N209B_SPACE', '2026-01-06')
    assert r.status_code == 400 and 'recorded with the email legacy@example.edu' in r.text
    # An inductee sees a masked email instead.
    s = c.post('/sessions', data={'induction_id': 'IND_J11_N209B_SPACE', 'csrf': token})
    code = db.one('SELECT code FROM sessions ORDER BY id DESC')['code']
    p = TestClient(app)
    r = p.post(f'/j/{code}', data={'name': 'Impostor', 'email': 'imp@example.edu', 'unikey': 'lega0010',
                                   'csrf': csrf(p, f'/j/{code}')})
    assert r.status_code == 400 and 'l***@example.edu' in r.text and 'legacy@example.edu' not in r.text
    r = p.post(f'/j/{code}', data={'name': 'Nobody', 'email': 'nobody@example.edu', 'unikey': '',
                                   'csrf': csrf(p, f'/j/{code}')})
    assert r.status_code == 400 and 'Enter your UniKey' in r.text
    # Register and CSV show UniKeys; missing ones are flagged.
    reg = c.get('/register').text
    assert 'lega0010' in reg
    assert ',lega0010,' in c.get('/export/records.csv').text


def test_access_api(assessor):
    c = assessor
    token = csrf(c, '/api-keys')
    # Bundle: a space induction granting the room, and equipment that requires it.
    bundle = json.loads(BUNDLE)
    space = next(i for i in bundle['inductions'] if i['id'] == 'IND_J11_N209B_SPACE')
    equip = next(i for i in bundle['inductions'] if i['id'] == 'IND_J11_S120_FORM4')
    space.update(version='api-space', resources=[{'id': 'j11-n209b', 'name': 'J11 N209B'}])
    equip.update(version='api-equip', location='J11 N209B', requires=['IND_J11_N209B_SPACE'],
                 resources=[{'id': 'form4', 'name': 'Formlabs Form 4'}], grants_access=['Formlabs Form 4'])
    c.post('/inductions/upload', data={'csrf': token},
           files={'file': ('i.json', json.dumps(bundle).encode(), 'application/json')})

    t = csrf(c, '/records/new')
    _manual(c, t, 'Full Access', 'full@example.edu', 'full0001', 'IND_J11_N209B_SPACE', '2026-06-01', '24')
    _manual(c, t, 'Full Access', 'full@example.edu', 'full0001', 'IND_J11_S120_FORM4', '2026-06-02', '12')
    _manual(c, t, 'No Space', 'nospace@example.edu', 'nosp0002', 'IND_J11_S120_FORM4', '2026-06-02', '12')
    _manual(c, t, 'Lapsed', 'lapsed@example.edu', 'laps0003', 'IND_J11_N209B_SPACE', '2020-01-01', '12')
    _manual(c, t, 'Forever', 'forever@example.edu', 'fore0004', 'IND_J11_N209B_SPACE', '2026-01-01', '0')

    # Keys: none, wrong, then a real one.
    anon = TestClient(app)
    assert anon.get('/api/v1/resources').status_code == 401
    assert anon.get('/api/v1/resources', headers={'Authorization': 'Bearer nope'}).status_code == 401
    r = c.post('/api-keys', data={'name': 'Card access add-on', 'csrf': token})
    key = re.search(r'id="newkey">([^<]+)<', r.text).group(1)
    assert key.startswith('spl_') and key not in c.get('/api-keys').text  # shown once
    H = {'Authorization': f'Bearer {key}'}

    res = anon.get('/api/v1/resources', headers={'X-API-Key': key}).json()['resources']
    assert {r['id'] for r in res} >= {'j11-n209b', 'form4'}

    def chk(uk, rid):
        return anon.get(f'/api/v1/access/check?unikey={uk}&resource={rid}', headers=H).json()

    a = chk('FULL0001', 'form4')
    assert a['allowed'] and a['reason'] == 'current' and a['person']['name'] == 'Full Access'
    assert a['access_until'].startswith('2027-06-02')  # earliest of Form 4 (12 months) and space (24 months)
    assert chk('full0001', 'j11-n209b')['allowed']
    b = chk('nosp0002', 'form4')
    assert not b['allowed'] and b['reason'] == 'prerequisite_missing' and b['requires_missing'] == ['IND_J11_N209B_SPACE']
    assert chk('laps0003', 'j11-n209b')['reason'] == 'expired'
    f = chk('fore0004', 'j11-n209b')
    assert f['allowed'] and f['access_until'] is None
    assert chk('fore0004', 'form4')['reason'] == 'not_inducted'
    assert chk('zzzz9999', 'form4')['reason'] == 'unknown_person'
    assert chk('full0001', 'nothing')['reason'] == 'unknown_resource'
    assert anon.get('/api/v1/access/check?unikey=full0001', headers=H).status_code == 400

    person = anon.get('/api/v1/people/full0001', headers=H).json()
    assert {x['id'] for x in person['access']} == {'j11-n209b', 'form4'}
    assert anon.get('/api/v1/people/zzzz9999', headers=H).status_code == 404

    everyone = anon.get('/api/v1/access', headers=H)
    people = {p['unikey']: p for p in everyone.json()['people']}
    assert 'full0001' in people and 'fore0004' in people and 'laps0003' not in people
    assert 'nosp0002' not in people  # no space induction, so no access to anything
    etag = everyone.headers['etag']
    assert anon.get('/api/v1/access', headers={**H, 'If-None-Match': etag}).status_code == 304

    # Cancelling a record changes the answer and the ETag.
    rid = db.one("SELECT r.id FROM records r JOIN people p ON p.id = r.person_id WHERE p.unikey = 'fore0004'")['id']
    c.post(f'/records/{rid}/cancel', data={'csrf': token})
    assert chk('fore0004', 'j11-n209b')['reason'] == 'not_inducted'
    assert anon.get('/api/v1/access', headers={**H, 'If-None-Match': etag}).status_code == 200

    # Pages render; revoking stops the key.
    assert 'form4' in c.get('/api-docs').text
    kid = db.one('SELECT id FROM api_keys ORDER BY id DESC')['id']
    assert db.one('SELECT uses FROM api_keys WHERE id = ?', kid)['uses'] > 5
    c.post(f'/api-keys/{kid}/revoke', data={'csrf': token})
    assert anon.get('/api/v1/resources', headers=H).status_code == 401


def test_ingress_redirects_to_base_url():
    r = TestClient(app).get('/', headers={'X-Ingress-Path': '/api/hassio_ingress/abc'})
    assert r.status_code == 200 and 'window.top.location.href = "http://testserver/"' in r.text
    assert TestClient(app).get('/healthz').json() == {'ok': True}  # normal requests unaffected


def test_dashboard_is_an_overview(assessor):
    c = assessor
    d = c.get('/dashboard').text
    assert 'Start session' not in d and 'ind-search' not in d  # the list lives on the Inductions page
    for label in ('Current inductions', 'People inducted', 'Waiting on someone', 'Expired', 'Expiring within'):
        assert label in d
    assert 'href="/inductions">Start an induction' in d
    i = c.get('/inductions').text
    assert 'id="ind-search"' in i and 'Start session' in i
    assert '>Registry</a>' in d and '<h1>Induction registry</h1>' in c.get('/register').text
