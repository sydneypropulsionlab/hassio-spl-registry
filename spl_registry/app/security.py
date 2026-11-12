"""Passwords, tokens, codes and simple rate limiting (standard library only)."""
import hashlib
import hmac
import secrets
import time
from collections import defaultdict

from .config import DATA_DIR

CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'  # no 0/O, 1/I


def hash_password(password):
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return f'scrypt${salt.hex()}${dk.hex()}'


def verify_password(password, stored):
    try:
        _, salt_hex, dk_hex = stored.split('$')
    except ValueError:
        return False
    dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2 ** 14, r=8, p=1)
    return hmac.compare_digest(dk.hex(), dk_hex)


def new_token():
    return secrets.token_urlsafe(32)


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def new_code(length=6):
    return ''.join(secrets.choice(CODE_ALPHABET) for _ in range(length))


def new_password():
    return '-'.join(secrets.token_hex(3) for _ in range(3))


def secret_key():
    """Session signing key, created once and kept in the data folder."""
    path = DATA_DIR / 'secret.key'
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(secrets.token_hex(32))
    return path.read_text().strip()


class RateLimiter:
    """Allow `limit` attempts per `window` seconds per key."""

    def __init__(self, limit, window):
        self.limit, self.window = limit, window
        self.hits = defaultdict(list)

    def allow(self, key):
        t = time.time()
        hits = [h for h in self.hits[key] if t - h < self.window]
        self.hits[key] = hits
        if len(hits) >= self.limit:
            return False
        hits.append(t)
        return True
