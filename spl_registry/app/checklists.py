"""Checklists (preflights, QC checks, inspections): filling them in, photos, outcomes.

A run is one filled-in checklist. It is a draft until it is submitted; after that its answers never
change, and corrections are added as amendments with a reason. Photos are resized, stripped of
location data and stored in the database, so the app's backups include them.
"""
import hashlib
import io
import json
import re
from datetime import datetime

import nh3
from PIL import Image, ImageOps, UnidentifiedImageError

from . import db, logic

MAX_UPLOAD_BYTES = 30 * 1024 * 1024
PHOTO_MAX_PX = 1600
THUMB_MAX_PX = 360
MAX_PHOTOS_PER_RUN = 60
TEXT_MAX = 500
RICH_MAX = 20000
RICH_TAGS = {'p', 'br', 'strong', 'b', 'em', 'i', 'u', 'ul', 'ol', 'li', 'a'}
# runs.outcome is the result: pass (nothing failed), fail (something failed) or complete (a one-word
# outcome); runs.outcome_label is the word the checklist page gave it, e.g. Go or Grounded
OUTCOME_FILTERS = {'pass': ('Passed', 'ok'), 'fail': ('Failed', 'bad'), 'complete': ('Complete', 'ok')}
_OLD_LABELS = {'go': ('Go', 'ok'), 'no-go': ('No-go', 'bad'), 'pass': ('Pass', 'ok'), 'fail': ('Fail', 'bad'),
                  'complete': ('Complete', 'ok')}


class PhotoError(ValueError):
    pass


# ----------------------------------------------------------------- checklists
def checklist(chk_id, version=None):
    if version:
        row = db.one('SELECT * FROM checklists WHERE id = ? AND version = ?', chk_id, version)
    else:
        row = db.one('SELECT * FROM checklists WHERE id = ? AND active = 1', chk_id)
    return (row, json.loads(row['json'])) if row else (None, None)


def active_checklists():
    return [(r, json.loads(r['json'])) for r in
            db.q("SELECT * FROM checklists WHERE active = 1 ORDER BY LOWER(category), location, title")]


def categories(rows):
    """Every category in use, as written the first time it appears; case doesn't matter."""
    seen = {}
    for r in rows:
        c = (r['category'] or '').strip()
        if c and c.lower() not in seen:
            seen[c.lower()] = c
    return sorted(seen.values(), key=str.lower)


def answerable(chk):
    """Checklist items that take an answer (everything but information lines)."""
    return [it for it in logic.items(chk) if it['kind'] != 'info']


def can_fail(it):
    return (it['kind'] == 'pass' or (it['kind'] == 'num' and ('min' in it or 'max' in it))
            or (it['kind'] == 'select' and bool(it.get('fail_choices'))))


def outcome_label(outcome, label=None):
    """(word to show, badge colour) for a run."""
    if label:
        return label, 'bad' if outcome == 'fail' else 'ok'
    return _OLD_LABELS.get(outcome or '', (outcome or '', 'muted'))


# ----------------------------------------------------------------- answers
def _num(s):
    s = (s or '').strip().replace(',', '.')
    if s == '':
        return None
    try:
        return float(s)
    except ValueError:
        raise ValueError('Enter a number') from None


def fmt_num(v):
    if v is None:
        return ''
    return str(int(v)) if float(v).is_integer() else f'{v:g}'


def limits_text(it):
    unit = (' ' + it['unit']) if it.get('unit') else ''
    if 'min' in it and 'max' in it:
        return f'{fmt_num(it["min"])}–{fmt_num(it["max"])}{unit}'
    if 'min' in it:
        return f'at least {fmt_num(it["min"])}{unit}'
    if 'max' in it:
        return f'at most {fmt_num(it["max"])}{unit}'
    return it.get('unit', '')


def clean_rich(html):
    """Keep simple formatting only (bold, italic, lists, links); plain text keeps its line breaks."""
    html = (html or '').strip()
    if html and '<' not in html:
        from markupsafe import escape
        html = ''.join(f'<p>{escape(p).replace(chr(10), "<br>")}</p>' for p in re.split(r'\n\s*\n', html) if p.strip())
    html = nh3.clean(html, tags=RICH_TAGS, attributes={'a': {'href'}}, link_rel='noopener noreferrer',
                     url_schemes={'http', 'https', 'mailto'})
    return '' if not re.sub(r'<[^>]+>|&nbsp;|\s', '', html) else html


def read_answers(chk, form):
    """Pull every item's answer out of the submitted form (values only, no checking)."""
    out = {}
    for it in answerable(chk):
        iid = it['id']
        a = {}
        if it['kind'] in ('check', 'ack'):
            a['value'] = form.get(f'v_{iid}') == 'on'
        elif it['kind'] == 'rich':
            a['value'] = clean_rich(form.get(f'v_{iid}', ''))[:RICH_MAX]
        else:
            a['value'] = (form.get(f'v_{iid}') or '').strip()[:TEXT_MAX]
        c = (form.get(f'c_{iid}') or '').strip()[:TEXT_MAX]
        if c:
            a['comment'] = c
        out[iid] = a
    return out


def evaluate(chk, answers, photo_counts):
    """Check the answers. Returns (answers with results, {item id: error}, outcome, outcome label)."""
    errors = {}
    any_fail = False
    for it in answerable(chk):
        iid = it['id']
        a = dict(answers.get(iid) or {})
        v = a.get('value')
        optional = bool(it.get('optional'))
        result = None
        kind = it['kind']
        if kind in ('check', 'ack'):
            if not v and not optional:
                errors[iid] = 'Tick this to confirm it.'
        elif kind == 'pass':
            if v in ('pass', 'fail', 'na'):
                result = v
            elif not optional:
                errors[iid] = 'Choose Pass, Fail or N/A.'
        elif kind == 'num':
            try:
                n = _num(v)
            except ValueError as e:
                errors[iid] = str(e) + '.'
                n = None
            if n is None:
                if iid not in errors and not optional:
                    errors[iid] = 'Enter the reading.'
            else:
                a['number'] = n
                if 'min' in it or 'max' in it:
                    ok = ('min' not in it or n >= it['min']) and ('max' not in it or n <= it['max'])
                    result = 'pass' if ok else 'fail'
        elif kind == 'select':
            if v in it.get('choices', []):
                result = 'fail' if v in it.get('fail_choices', []) else ('pass' if it.get('fail_choices') else None)
            elif v:
                errors[iid] = 'Choose one of the options.'
            elif not optional:
                errors[iid] = 'Choose one.'
        elif kind in ('text', 'rich'):
            if not v and not optional:
                errors[iid] = 'Fill this in.'
        elif kind == 'photo':
            if not photo_counts.get(iid) and not optional:
                errors[iid] = 'Add a photo.'
        if it.get('photo') == 'required' and not photo_counts.get(iid) and iid not in errors:
            errors[iid] = 'Add a photo.'
        if result == 'fail':
            any_fail = True
            if it.get('comment') == 'on_fail' and not a.get('comment') and iid not in errors:
                errors[iid] = 'Say what failed in the comment.'
        if result:
            a['result'] = result
        else:
            a.pop('result', None)
        answers[iid] = a
    words = logic.outcome_words(chk)
    if len(words) == 1:
        outcome, label = 'complete', words[0]
    else:
        outcome, label = ('fail', words[1]) if any_fail else ('pass', words[0])
    return answers, errors, outcome, label


# ----------------------------------------------------------------- photos
def process_photo(raw, filename=''):
    """Resize a photo, keep the time it was taken, drop everything else in its metadata."""
    if not raw:
        raise PhotoError('The photo was empty.')
    if len(raw) > MAX_UPLOAD_BYTES:
        raise PhotoError(f'That photo is over {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.')
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except (UnidentifiedImageError, OSError):
        raise PhotoError(f'{filename or "That file"} is not a photo this app can read (use JPEG or PNG; on an '
                         'iPhone, Settings > Camera > Formats > Most Compatible).') from None
    taken = None
    try:
        exif = img.getexif()
        raw_dt = exif.get_ifd(0x8769).get(36867) or exif.get(306)   # DateTimeOriginal, else DateTime
        if raw_dt:
            taken = datetime.strptime(str(raw_dt).strip('\x00 '), '%Y:%m:%d %H:%M:%S').isoformat()
    except Exception:
        taken = None
    img = ImageOps.exif_transpose(img)
    if img.mode not in ('RGB', 'L'):
        bg = Image.new('RGB', img.size, (255, 255, 255))
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGBA')
            bg.paste(img, mask=img.split()[-1])
            img = bg
        else:
            img = img.convert('RGB')
    full = img.copy()
    full.thumbnail((PHOTO_MAX_PX, PHOTO_MAX_PX))
    buf = io.BytesIO()
    full.save(buf, 'JPEG', quality=85, optimize=True)   # no exif= so nothing else is kept
    data = buf.getvalue()
    th = img.copy()
    th.thumbnail((THUMB_MAX_PX, THUMB_MAX_PX))
    tbuf = io.BytesIO()
    th.save(tbuf, 'JPEG', quality=80)
    name = re.sub(r'[^A-Za-z0-9_.-]+', '_', (filename or 'photo').rsplit('.', 1)[0])[:60] + '.jpg'
    return {'data': data, 'thumb': tbuf.getvalue(), 'mime': 'image/jpeg', 'size': len(data),
            'width': full.width, 'height': full.height, 'sha256': hashlib.sha256(data).hexdigest(),
            'taken_at': taken, 'filename': name}


def add_photo(run_id, item_id, raw, filename, by):
    n = db.one('SELECT COUNT(*) AS n FROM attachments WHERE run_id = ?', run_id)['n']
    if n >= MAX_PHOTOS_PER_RUN:
        raise PhotoError(f'A checklist can have at most {MAX_PHOTOS_PER_RUN} photos.')
    p = process_photo(raw, filename)
    return db.run('INSERT INTO attachments (run_id, item_id, filename, mime, size, width, height, sha256, taken_at, '
                  'data, thumb, created_at, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  run_id, item_id, p['filename'], p['mime'], p['size'], p['width'], p['height'], p['sha256'],
                  p['taken_at'], p['data'], p['thumb'], db.now(), by)


def photos(run_id):
    """{item id or '' for the whole checklist: [attachment rows without the image data]}"""
    out = {}
    for a in db.q('SELECT id, item_id, filename, size, width, height, sha256, taken_at, created_at, created_by '
                  'FROM attachments WHERE run_id = ? ORDER BY id', run_id):
        out.setdefault(a['item_id'] or '', []).append(a)
    return out


# ----------------------------------------------------------------- runs
def run_row(rid):
    return db.one('SELECT r.*, c.title, c.category, c.location FROM runs r JOIN checklists c '
                  'ON c.id = r.checklist_id AND c.version = r.checklist_version WHERE r.id = ?', rid)


def failed_items(chk, answers):
    return [it for it in answerable(chk) if (answers.get(it['id']) or {}).get('result') == 'fail']
