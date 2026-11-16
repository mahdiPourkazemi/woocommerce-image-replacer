"""Image-only API surface; independent of the product-creation client and runtime.

Transport NEVER retries a write. The durable engine owns retry/reconciliation.
Remote response text and credential-bearing request URLs never become errors.
"""
from datetime import timezone
from email.utils import parsedate_to_datetime
import mimetypes
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth

from .core import ApiError, Page, media_id

PRODUCT_FIELDS = 'id,name,sku,type,status,permalink,date_modified_gmt,images'


def retry_after_seconds(value, now=None):
    try:
        return max(0, float(value))
    except (TypeError, ValueError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0, date.timestamp() - (time.time() if now is None else now))
        except (TypeError, ValueError, OverflowError):
            return 0


class ImageApi:
    def __init__(self, config, *, session=None):
        self.config = config
        self.store = config.store
        self._injected = session
        # One session per thread: the page thread and the queue worker never
        # share connection state. Adapter retries stay off (see module note).
        self._local = threading.local()
        self._sessions = []
        self._sessions_lock = threading.Lock()

    def _session(self):
        if self._injected is not None:
            return self._injected
        session = getattr(self._local, 'session', None)
        if session is None:
            session = requests.Session()
            session.mount('https://', HTTPAdapter(max_retries=0, pool_connections=1, pool_maxsize=2))
            self._local.session = session
            with self._sessions_lock:
                self._sessions.append(session)
        return session

    def close(self):
        with self._sessions_lock:
            for session in self._sessions:
                session.close()
            self._sessions.clear()
        self._local = threading.local()

    def _request(self, method, resource, *, media=False, **kwargs):
        auth = HTTPBasicAuth(self.config.wp_user, self.config.wp_password) if media and self.config.wp_user else (
            None if media else HTTPBasicAuth(self.config.key, self.config.secret))
        prefix = '/wp-json/wp/v2/' if media else '/wp-json/wc/v3/'
        session = self._session()
        try:
            response = session.request(method, self.store + prefix + resource,
                auth=auth, timeout=self.config.timeout, allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise ApiError('temporary') from None
        status = response.status_code
        if status in (401, 403):
            raise ApiError('auth')
        if status == 429:
            raise ApiError('rate', retry_after=retry_after_seconds(response.headers.get('Retry-After')))
        # 408: the server gave up waiting, so a write may or may not have landed.
        if status >= 500 or status == 408:
            raise ApiError('temporary')
        if status not in (200, 201):
            raise ApiError('invalid', 'سایت درخواست را نپذیرفت؛ دسترسی، شناسه و تنظیمات مسیر را بررسی کنید.')
        try:
            return response.json(), response.headers
        except (ValueError, TypeError):
            raise ApiError('temporary', 'پاسخ JSON معتبر دریافت نشد؛ نتیجه باید بازخوانی شود.') from None

    def list_products(self, page=1, per_page=20, status='any'):
        data, headers = self._request('GET', 'products', params={'page': page,
            'per_page': per_page, 'status': status, '_fields': PRODUCT_FIELDS, 'orderby': 'id', 'order': 'asc'})
        if not isinstance(data, list):
            raise ApiError('invalid', 'فهرست محصولات معتبر نیست.')
        raw = headers.get('X-WP-TotalPages')
        try:
            total = int(raw) if raw is not None else None
            if total is not None and total < 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ApiError('invalid', 'اطلاعات صفحه‌بندی سایت معتبر نیست.') from None
        return Page(data, total)

    def get_product(self, pid):
        # A slow site should not render descriptions and prices for a gallery check.
        data, _ = self._request('GET', 'products/' + str(media_id(pid)), params={'_fields': PRODUCT_FIELDS})
        if not isinstance(data, dict) or data.get('id') != pid:
            raise ApiError('invalid', 'هویت محصول در پاسخ تأیید نشد.')
        return data

    def put_images(self, pid, ids):
        payload = {'images': [{'id': media_id(value)} for value in ids]}
        data, _ = self._request('PUT', 'products/' + str(media_id(pid)), json=payload,
            params={'_fields': PRODUCT_FIELDS})
        return data

    def _product_list(self, **params):
        data, _ = self._request('GET', 'products', params={'status': 'any', 'per_page': 20, **params})
        if not isinstance(data, list):
            raise ApiError('invalid', 'نتیجهٔ جست‌وجوی محصول معتبر نیست.')
        return data

    def search_products(self, query, page=1):
        query = str(query).strip()
        if not query:
            raise ValueError('نام، SKU یا لینک محصول را وارد کنید.')
        parsed = urlsplit(query)
        # Only real web links are links; a product name such as "Box: large"
        # also parses with a "scheme" and must still reach the name search.
        if parsed.scheme.lower() in ('http', 'https'):
            if parsed.scheme != 'https' or parsed.netloc.lower() != urlsplit(self.store).netloc.lower() or parsed.username or parsed.password:
                raise ValueError('لینک باید متعلق به همین فروشگاه باشد.')
            from urllib.parse import parse_qs, unquote
            pid = parse_qs(parsed.query).get('p', [''])[0]
            if pid.isdigit():
                return [self.get_product(int(pid))]
            slug = unquote(parsed.path.rstrip('/').split('/')[-1])
            return self._product_list(slug=slug)
        # Exact SKU first; name search is separate, never an implicit overwrite.
        return self._product_list(sku=query) or self._product_list(search=query, page=page)

    def search_media(self, query='', page=1):
        data, _ = self._request('GET', 'media', media=True, params={'search': str(query),
            'media_type': 'image', 'per_page': 10, 'page': page, 'orderby': 'date', 'order': 'desc'})
        if not isinstance(data, list):
            raise ApiError('invalid')
        return data

    def get_media(self, mid):
        data, _ = self._request('GET', 'media/' + str(media_id(mid)), media=True)
        if (not isinstance(data, dict) or data.get('id') != mid or data.get('media_type') != 'image'
                or not str(data.get('mime_type', '')).startswith('image/')):
            raise ApiError('invalid', 'رسانهٔ انتخابی تصویر معتبر همین فروشگاه نیست.')
        return data

    def check_upload(self):
        if not self.config.wp_user or not self.config.wp_password:
            raise ApiError('auth', 'برای بارگذاری، نام کاربری و Application Password وردپرس لازم است؛ کلید ووکامرس کافی نیست.')
        data, _ = self._request('GET', 'users/me', media=True, params={'context': 'edit', '_fields': 'id,capabilities'})
        if not isinstance(data, dict) or data.get('capabilities', {}).get('upload_files') is not True:
            raise ApiError('auth', 'مجوز upload_files تأیید نشد؛ تصویر را از مسیر مجاز خود بارگذاری و به‌عنوان رسانهٔ موجود انتخاب کنید.')

    def upload_image(self, path):
        self.check_upload()
        path = Path(path)
        if path.suffix.lower() not in {'.png', '.jpg', '.jpeg', '.webp'} or not 0 < path.stat().st_size <= 10 * 1024 * 1024:
            raise ValueError('فایل PNG، JPEG یا WebP با اندازهٔ حداکثر ۱۰ مگابایت انتخاب کنید.')
        with path.open('rb') as source:
            data, _ = self._request('POST', 'media', media=True, data=source,
                params={'title': path.stem}, headers={
                    'Content-Disposition': 'attachment; filename="replacement' + path.suffix.lower() + '"',
                    'Content-Type': mimetypes.guess_type(path.name)[0] or 'application/octet-stream'})
        if not isinstance(data, dict) or type(data.get('id')) is not int or data['id'] <= 0:
            raise ApiError('temporary', 'نتیجهٔ بارگذاری نامشخص است؛ کتابخانه را بررسی و رسانهٔ ساخته‌شده را انتخاب کنید.')
        return data
