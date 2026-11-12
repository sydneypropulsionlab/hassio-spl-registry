"""Settings, read from Home Assistant add-on options or environment variables.

In the add-on, Home Assistant writes the options set in the UI to /data/options.json.
Outside Home Assistant (local testing, docker compose), the same settings come from
environment variables named SPL_<OPTION>, e.g. SPL_BASE_URL.
"""
import json
import os
from dataclasses import dataclass, fields
from pathlib import Path

DATA_DIR = Path(os.environ.get('DATA_DIR', '/data'))


@dataclass
class Settings:
    base_url: str = 'http://localhost:8099'
    timezone: str = 'Australia/Sydney'
    smtp_host: str = ''
    smtp_port: int = 587
    smtp_username: str = ''
    smtp_password: str = ''
    smtp_from: str = ''
    smtp_starttls: bool = True
    session_minutes: int = 30
    confirm_days: int = 7
    expiry_warning_days: int = 30
    certificate_cc: tuple = ()
    access_grace_days: int = 0
    bundle_url: str = ''
    bundle_username: str = ''
    bundle_password: str = ''
    bundle_token: str = ''
    bundle_poll_minutes: int = 10


def _coerce(value, kind):
    if kind is tuple:  # a list in options.json, or comma-separated in an environment variable
        items = value if isinstance(value, (list, tuple)) else str(value).replace(';', ',').split(',')
        return tuple(str(i).strip() for i in items if str(i).strip())
    if kind is bool:
        return value if isinstance(value, bool) else str(value).lower() in ('1', 'true', 'yes', 'on')
    if kind is int:
        return int(value)
    return '' if value is None else str(value)


def load_settings():
    s = Settings()
    opts = {}
    options_file = DATA_DIR / 'options.json'
    if options_file.exists():
        opts = json.loads(options_file.read_text())
    for f in fields(Settings):
        kind = type(getattr(s, f.name))
        if f.name in opts and opts[f.name] is not None:
            setattr(s, f.name, _coerce(opts[f.name], kind))
        env = os.environ.get('SPL_' + f.name.upper())
        if env is not None:
            setattr(s, f.name, _coerce(env, kind))
    s.base_url = s.base_url.rstrip('/')
    return s
