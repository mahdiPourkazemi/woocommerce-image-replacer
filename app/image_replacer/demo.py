"""Offline practice store: same surface as ImageApi, no network, no credentials."""
from copy import deepcopy
import threading
import time
from urllib.parse import parse_qs, quote, urlsplit

from .core import ApiError, Page

DEMO_STORE = 'https://demo.invalid'
SHARED_OLD_IMAGE = 200
PALETTE = ('#c0392b', '#2471a3', '#1e8449', '#b7950b', '#7d3c98', '#ca6f1e', '#117a65', '#5d6d7e')


def thumbnail(mid, label=''):
    """A self-contained picture, so the practice mode never loads a remote file."""
    color = PALETTE[mid % len(PALETTE)]
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96">'
        f'<rect width="96" height="96" rx="10" fill="{color}"/>'
        f'<text x="48" y="44" font-size="22" font-family="Segoe UI,Tahoma" fill="#fff" text-anchor="middle">{mid}</text>'
        f'<text x="48" y="70" font-size="11" font-family="Segoe UI,Tahoma" fill="#fff" text-anchor="middle">{label}</text></svg>')
    return 'data:image/svg+xml;utf8,' + quote(svg)


def _media(mid, title, label=''):
    return {'id': mid, 'title': {'rendered': title}, 'source_url': thumbnail(mid, label),
        'media_type': 'image', 'mime_type': 'image/svg+xml'}


def _catalogue():
    media = {SHARED_OLD_IMAGE: _media(SHARED_OLD_IMAGE, 'راهنمای اندازهٔ قدیمی', 'قدیمی'),
        900: _media(900, 'راهنمای اندازهٔ جدید', 'جدید'), 901: _media(901, 'بنر ارسال رایگان', 'بنر')}
    products = {}
    for pid in range(1, 46):
        own = [1000 + pid * 10 + slot for slot in range(3)]
        gallery = list(own)
        if pid % 4 == 1:  # Shared image in the second slot.
            gallery.insert(1, SHARED_OLD_IMAGE)
        elif pid % 9 == 0:  # ...or last.
            gallery.append(SHARED_OLD_IMAGE)
        if pid == 13:  # ...or as the main image.
            gallery = [SHARED_OLD_IMAGE] + own
        if pid == 17:  # Already holds the usual replacement: a conflict to review.
            gallery.append(900)
        for mid in own:
            media[mid] = _media(mid, f'عکس محصول {pid}')
        products[pid] = {'id': pid, 'name': f'جعبهٔ نمونه {pid}', 'sku': '' if pid % 5 == 0 else f'DEMO-{pid:03d}',
            'type': 'variable' if pid in (9, 21) else 'simple', 'status': 'draft' if pid % 10 == 0 else 'publish',
            'permalink': f'{DEMO_STORE}/?p={pid}', 'date_modified_gmt': '2026-11-16T10:00:00',
            'images': [{'id': mid} for mid in gallery]}
    return products, media


class DemoApi:
    store = DEMO_STORE

    def __init__(self, *, delay=.12, sleep=time.sleep):
        self._products, self._media = _catalogue()
        self._lock = threading.Lock()
        self._delay, self._sleep = delay, sleep
        self._next_media = 5000
        self.writes = []

    def close(self):
        pass

    def _view(self, product):
        row = deepcopy(product)
        for image in row['images']:
            image['src'] = self._media[image['id']]['source_url']
        return row

    def list_products(self, page=1, per_page=20, status='any'):
        self._sleep(self._delay)
        with self._lock:
            rows = [row for row in self._products.values() if status == 'any' or row['status'] == status]
            start = (page - 1) * per_page
            return Page([self._view(row) for row in rows[start:start + per_page]], -(-len(rows) // per_page))

    def get_product(self, pid):
        self._sleep(self._delay)
        with self._lock:
            if pid not in self._products:
                raise ApiError('invalid', 'محصول در فروشگاه نمایشی پیدا نشد.')
            return self._view(self._products[pid])

    def put_images(self, pid, ids):
        self._sleep(self._delay)
        with self._lock:
            if pid not in self._products or any(mid not in self._media for mid in ids):
                raise ApiError('invalid')
            self.writes.append((pid, list(ids)))
            self._products[pid]['images'] = [{'id': mid} for mid in ids]
            return self._view(self._products[pid])

    def search_products(self, query, page=1):
        query = str(query).strip()
        if not query:
            raise ValueError('نام، SKU یا لینک محصول را وارد کنید.')
        linked = parse_qs(urlsplit(query).query).get('p', [''])[0]
        with self._lock:
            rows = [row for row in self._products.values()
                if (linked.isdigit() and row['id'] == int(linked)) or row['sku'].lower() == query.lower()
                or query in row['name']]
            return [self._view(row) for row in rows[:20]]

    def search_media(self, query='', page=1):
        with self._lock:
            rows = [row for row in self._media.values() if str(query) in row['title']['rendered']]
            rows.sort(key=lambda row: (row['id'] > 999 and row['id'] < 5000, -row['id']))
            return deepcopy(rows[(page - 1) * 10:page * 10])

    def get_media(self, mid):
        with self._lock:
            if mid not in self._media:
                raise ApiError('invalid', 'رسانهٔ انتخابی تصویر معتبر همین فروشگاه نیست.')
            return deepcopy(self._media[mid])

    def check_upload(self):
        pass

    def upload_image(self, path):
        with self._lock:
            self._next_media += 1
            self._media[self._next_media] = _media(self._next_media, 'عکس بارگذاری‌شدهٔ نمایشی', 'تازه')
            return deepcopy(self._media[self._next_media])
