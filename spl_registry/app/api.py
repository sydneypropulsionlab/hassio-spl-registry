"""Read-only JSON API for other systems (e.g. the tap-card add-on), keyed by UniKey.

Every request needs an API key, created by an assessor on the API keys page, sent as
`Authorization: Bearer <key>` or `X-API-Key: <key>`.
"""
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from . import access, db, security

router = APIRouter(prefix='/api/v1')
limiter = security.RateLimiter(limit=1200, window=60)
API_VERSION = 1
UNIKEY_RE = re.compile(r'^[a-z]{2,8}[0-9]{1,6}$')


def new_key():
    """Returns (key, prefix, hash). The key is shown once; only its hash is stored."""
    key = 'spl_' + security.new_token()
    return key, key[:10], security.token_hash(key)


def _error(status, message):
    return JSONResponse({'error': message}, status_code=status)


def _authenticate(request):
    auth = request.headers.get('authorization', '')
    key = auth[7:].strip() if auth.lower().startswith('bearer ') else request.headers.get('x-api-key', '').strip()
    if not key:
        return None, _error(401, 'Missing API key. Send "Authorization: Bearer <key>" or "X-API-Key: <key>".')
    row = db.one('SELECT * FROM api_keys WHERE key_hash = ? AND revoked_at IS NULL', security.token_hash(key))
    if not row:
        return None, _error(401, 'Invalid or revoked API key.')
    if not limiter.allow(row['id']):
        return None, _error(429, 'Too many requests for this key; slow down.')
    db.run('UPDATE api_keys SET last_used_at = ?, uses = uses + 1 WHERE id = ?', db.now(), row['id'])
    return row, None


def _settings(request):
    return request.app.state.settings


@router.get('')
def index(request: Request):
    _, err = _authenticate(request)
    if err:
        return err
    return {'api_version': API_VERSION, 'endpoints': [
        'GET /api/v1/access/check?unikey=<unikey>&resource=<resource id>',
        'GET /api/v1/people/<unikey>',
        'GET /api/v1/resources',
        'GET /api/v1/access',
    ]}


@router.get('/access/check')
def check(request: Request, unikey: str = '', resource: str = ''):
    _, err = _authenticate(request)
    if err:
        return err
    unikey, resource = unikey.strip().lower(), resource.strip().lower()
    if not unikey or not resource:
        return _error(400, 'Give both unikey and resource, e.g. ?unikey=abcd1234&resource=j11-n209b')
    return access.check(unikey, resource, _settings(request))


@router.get('/people/{unikey}')
def person(request: Request, unikey: str):
    _, err = _authenticate(request)
    if err:
        return err
    unikey = unikey.strip().lower()
    p = db.one('SELECT * FROM people WHERE unikey = ?', unikey)
    if not p:
        return _error(404, f'No inductions recorded for UniKey {unikey}.')
    inductions, allowed = access.person_access(p, _settings(request))
    return {'unikey': p['unikey'], 'name': p['name'], 'role': p['role'],
            'inductions': [{k: i[k] for k in ('id', 'title', 'type', 'location', 'status', 'expires_at',
                                                  'requires', 'requires_missing', 'grants')}
                           for i in inductions if i['status'] != 'not_inducted'],
            'access': sorted(allowed.values(), key=lambda a: a['id'])}


@router.get('/resources')
def resources(request: Request):
    _, err = _authenticate(request)
    if err:
        return err
    _, res = access.catalogue()
    return {'resources': sorted(res.values(), key=lambda r: r['id'])}


@router.get('/access')
def everyone(request: Request):
    """Everyone with current access and what to. Supports If-None-Match so polling is cheap."""
    _, err = _authenticate(request)
    if err:
        return err
    data = access.everyone(_settings(request))
    etag = f'"{access.fingerprint(data)}"'
    if request.headers.get('if-none-match') == etag:
        return Response(status_code=304, headers={'ETag': etag})
    return JSONResponse({**data, 'generated_at': db.now()}, headers={'ETag': etag})
