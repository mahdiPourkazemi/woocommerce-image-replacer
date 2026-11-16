"""One-time upload intent is durable before the media POST, not after it."""
import hashlib
from pathlib import Path

from .core import ApiError


def upload_once(journal, api, store, path):
    path = Path(path)
    if not path.is_file() or not 0 < path.stat().st_size <= 10 * 1024 * 1024:
        raise ValueError('یک فایل تصویر موجود تا ۱۰ مگابایت انتخاب کنید.')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    existing = journal.upload(store, digest)
    if existing:
        if existing['state'] == 'complete':
            return api.get_media(existing['media_id'])
        raise ValueError('نتیجهٔ بارگذاری قبلی نامشخص است؛ بارگذاری تکرار نشد. کتابخانه را بررسی و رسانهٔ موجود را انتخاب کنید.')
    journal.reserve_upload(store, digest)
    try:
        result = api.upload_image(path)
    except (ApiError, ValueError) as error:
        # Only a 'temporary' outcome may have created the media. A definite
        # rejection created nothing, so it must not block a corrected retry.
        if not (isinstance(error, ApiError) and error.code == 'temporary'):
            journal.release_upload(store, digest)
        raise
    mid = result.get('id') if isinstance(result, dict) else None
    if type(mid) is not int or mid <= 0:
        raise ValueError('شناسهٔ رسانه تأیید نشد؛ پیش از هر بارگذاری تازه کتابخانه را بررسی کنید.')
    journal.complete_upload(store, digest, mid)
    return result
