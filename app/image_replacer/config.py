"""Explicit external configuration; never inherit the old app's default site."""
from dataclasses import dataclass, field
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit, urlunsplit

from dotenv import dotenv_values


def normalize_store(value):
    parsed = urlsplit(str(value).strip())
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or '/wp-json' in parsed.path.lower()):
        raise ValueError('آدرس پایهٔ HTTPS فروشگاه، بدون رمز، پارامتر یا مسیر wp-json وارد کنید.')
    try:
        port = parsed.port
    except ValueError:
        raise ValueError('پورت آدرس فروشگاه معتبر نیست.') from None
    host = parsed.hostname.lower()
    if ':' in host:
        host = '[' + host + ']'
    if port and port != 443:
        host += ':' + str(port)
    return urlunsplit(('https', host, parsed.path.rstrip('/'), '', ''))


def entry_directory():
    return Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[2]


def data_directory(demo=False):
    base = Path(os.environ.get('LOCALAPPDATA') or Path.home() / '.local' / 'share')
    return base / 'Mahdipoori' / 'WooCommerceImageReplacer' / ('demo' if demo else 'live')


@dataclass(frozen=True)
class Config:
    store: str
    key: str = field(repr=False)
    secret: str = field(repr=False)
    wp_user: str = field(default='', repr=False)
    wp_password: str = field(default='', repr=False)
    timeout: int = 120

    @classmethod
    def load(cls, path=None):
        path = Path(path) if path else entry_directory() / '.env'
        if not path.is_file():
            raise ValueError('فایل .env پیدا نشد؛ از نمونه بسازید یا مسیر تنظیمات را انتخاب کنید.')
        values = dotenv_values(path, interpolate=False)
        store = normalize_store(values.get('WC_BASE_URL') or '')
        key, secret = values.get('WC_CONSUMER_KEY') or '', values.get('WC_CONSUMER_SECRET') or ''
        if not key.strip() or not secret.strip():
            raise ValueError('WC_CONSUMER_KEY و WC_CONSUMER_SECRET را در فایل .env تکمیل کنید.')
        return cls(store, key, secret, values.get('WP_USERNAME') or '', values.get('WP_APPLICATION_PASSWORD') or '')
