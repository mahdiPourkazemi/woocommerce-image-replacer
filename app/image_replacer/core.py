"""Pure gallery contracts and read-only discovery; no credential or UI logic."""
from dataclasses import dataclass
from datetime import datetime, timezone


class ApiError(Exception):
    """A safe, classified boundary error, never a raw remote response."""
    def __init__(self, code, message='', retry_after=0):
        self.code = code
        self.retry_after = max(0, float(retry_after))
        super().__init__(message or {
            'auth': 'دسترسی تأیید نشد؛ کلیدها و مجوز حساب را بررسی کنید.',
            'temporary': 'پاسخ سایت نامشخص است؛ پیش از تکرار، وضعیت بازخوانی می‌شود.',
            'rate': 'محدودیت درخواست سایت؛ صف پس از مهلت اعلام‌شده ادامه می‌یابد.',
            'invalid': 'داده یا رسانه پذیرفته نشد؛ این مورد را بررسی کنید.',
        }.get(code, 'پاسخ سایت معتبر نیست؛ تنظیمات و اتصال را بررسی کنید.'))


@dataclass(frozen=True)
class Page:
    items: list
    total_pages: int | None = None


def media_id(value):
    if type(value) is not int or value <= 0:
        raise ValueError('شناسهٔ رسانه باید عدد صحیح مثبت باشد.')
    return value


def gallery_ids(product):
    """Require a complete ordered gallery; never silently drop malformed slots."""
    images = product.get('images')
    if not isinstance(images, list):
        raise ValueError('فهرست کامل تصاویر محصول دریافت نشد.')
    return [media_id(image.get('id')) for image in images if isinstance(image, dict)] if all(
        isinstance(image, dict) for image in images) else _invalid_gallery()


def _invalid_gallery():
    raise ValueError('گالری دارای دادهٔ نامعتبر است؛ تغییر خودکار مجاز نیست.')


def preview_product(product, old_id, new_id):
    media_id(old_id)
    media_id(new_id)
    if old_id == new_id:
        raise ValueError('عکس جدید با عکس قدیمی یکسان است.')
    row = {key: product.get(key, '') for key in
        ('id', 'name', 'sku', 'permalink', 'date_modified_gmt', 'type', 'status')}
    row.update(before=[], after=[], positions=[], blocked=False, reason='', main_image_changes=False,
        # Variation images live on separate records this version never edits.
        notice='تصاویر مستقل تنوع‌های این محصول متغیر تغییر نمی‌کنند.' if row['type'] == 'variable' else '')
    try:
        media_id(row['id'])
        row['before'] = gallery_ids(product)
        if old_id not in row['before']:
            raise ValueError('عکس قدیمی دیگر در این گالری نیست.')
        if new_id in row['before']:
            raise ValueError('عکس جدید از قبل در گالری وجود دارد؛ نیازمند بررسی.')
        row['positions'] = [index for index, value in enumerate(row['before']) if value == old_id]
        row['after'] = [new_id if value == old_id else value for value in row['before']]
        # Slot 0 is the product's main image, so the storefront thumbnail changes too.
        row['main_image_changes'] = 0 in row['positions']
    except ValueError as error:
        row.update(blocked=True, reason=str(error))
    return row


def scan_products(api, old_id, *, status='any', per_page=20, stop=None, progress=None):
    """Read all accessible pages; report scan scope/time, not a live snapshot."""
    media_id(old_id)
    if status not in {'any', 'publish', 'draft', 'pending', 'private', 'future'}:
        raise ValueError('محدودهٔ وضعیت پشتیبانی نمی‌شود.')
    seen, found, fingerprints = set(), [], set()
    page, pages = 1, 0
    while True:
        if stop and stop.is_set():
            raise InterruptedError('جست‌وجو متوقف شد؛ فهرست ناقص برای اجرا تأیید نمی‌شود.')
        response = api.list_products(page, per_page, status)
        if not isinstance(response, Page) or not isinstance(response.items, list):
            raise ValueError('پاسخ صفحه‌بندی معتبر نیست.')
        if not all(isinstance(item, dict) for item in response.items):
            raise ValueError('فهرست محصولات دارای ردیف نامعتبر است؛ اسکن کامل تأیید نشد.')
        fingerprint = tuple(item.get('id') for item in response.items)
        if fingerprint and fingerprint in fingerprints:
            raise ValueError('سایت همان صفحه را تکرار می‌کند؛ اسکن کامل تأیید نشد.')
        fingerprints.add(fingerprint)
        if not response.items and response.total_pages and page <= response.total_pages:
            raise ValueError('صفحهٔ خالی پیش از پایان اعلام‌شده؛ اسکن ناقص است.')
        for item in response.items:
            pid = media_id(item.get('id'))
            if pid in seen:
                continue
            seen.add(pid)
            try:
                matches = old_id in gallery_ids(item)
            except ValueError:
                # A malformed matching gallery is surfaced during preview.
                images = item.get('images')
                matches = any(isinstance(image, dict) and image.get('id') == old_id
                    for image in (images if isinstance(images, list) else []))
            if matches:
                found.append(item)
        pages += 1
        if progress:
            progress({'pages': pages, 'scanned': len(seen), 'found': len(found)})
        if response.total_pages is not None:
            if page >= response.total_pages:
                break
        elif len(response.items) < per_page:
            break
        page += 1
        if page > 100000:
            raise ValueError('پایان صفحه‌بندی مشخص نشد؛ اسکن متوقف شد.')
    return {'products': found, 'pages': pages, 'scanned': len(seen), 'scope': status,
        'scanned_at': datetime.now(timezone.utc).isoformat(),
        'coverage': 'فقط محصولات قابل‌دسترسی در محدودهٔ انتخابی؛ اسکن، عکس لحظه‌ای اتمی نیست.'}
