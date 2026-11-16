"""Boundary contracts: no credentials in reports, no duplicate media uploads."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.image_replacer.core import ApiError
from app.image_replacer.config import Config, normalize_store
from app.image_replacer.transport import ImageApi, retry_after_seconds
from app.image_replacer.media import upload_once
from app.image_replacer.journal import JobStore


def test_env_is_explicit_external_and_never_uses_legacy_site_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv('SITE_URL', 'https://wrong.test')
    path = tmp_path / '.env'
    path.write_text('WC_BASE_URL=https://shop.test/\nWC_CONSUMER_KEY=test-key\nWC_CONSUMER_SECRET=test-secret\n', encoding='utf-8')
    config = Config.load(path)
    assert config.store == 'https://shop.test'
    assert 'test-key' not in repr(config)
    assert 'test-secret' not in repr(config)
    with pytest.raises(ValueError):
        Config.load(tmp_path / 'missing.env')


@pytest.mark.parametrize('url', ['http://shop.test', 'https://u:p@shop.test',
    'https://shop.test/?consumer_key=secret', 'https://shop.test/#secret', 'file:///x', 'https://shop.test/wp-json/wc/v3'])
def test_unsafe_store_urls_are_rejected(url):
    with pytest.raises(ValueError):
        normalize_store(url)


class Response:
    def __init__(self, data=None, status=200, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self._data = data

    def json(self):
        return self._data


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


def api_with(response):
    config = Config('https://shop.test', 'test-key', 'test-secret')
    session = Session(response)
    return ImageApi(config, session=session), session


def test_adapter_put_contains_complete_gallery_only_and_no_redirects():
    api, session = api_with(Response({'id': 12, 'images': [{'id': 5}, {'id': 7}]}))
    api.put_images(12, [5, 7])
    method, url, kwargs = session.calls[0]
    assert method == 'PUT'
    assert url == 'https://shop.test/wp-json/wc/v3/products/12'
    assert kwargs['json'] == {'images': [{'id': 5}, {'id': 7}]}
    assert kwargs['allow_redirects'] is False


def test_pagination_headers_and_all_required_fields_are_preserved():
    api, session = api_with(Response([{'id': 12, 'images': []}], headers={'X-WP-TotalPages': '4'}))
    page = api.list_products(2, 20, 'any')
    assert page.total_pages == 4
    params = session.calls[0][2]['params']
    assert params['page'] == 2 and params['status'] == 'any'
    assert {'id', 'name', 'sku', 'type', 'status', 'permalink', 'date_modified_gmt', 'images'} <= set(params['_fields'].split(','))


def test_rate_limit_header_is_preserved_in_safe_error():
    api, _ = api_with(Response({'message': 'test-secret'}, 429, {'Retry-After': '17'}))
    with pytest.raises(ApiError) as caught:
        api.get_product(1)
    assert caught.value.code == 'rate'
    assert caught.value.retry_after == 17
    assert 'test-secret' not in str(caught.value)


def test_media_credentials_are_not_inferred_from_wc_keys():
    api, session = api_with(Response({}))
    with pytest.raises(ApiError) as caught:
        api.upload_image(Path('nonexistent.webp'))
    assert caught.value.code == 'auth'
    assert not session.calls


def test_upload_timeout_is_durable_and_never_blindly_repeated(tmp_path):
    path = tmp_path / 'photo.webp'
    path.write_bytes(b'RIFFtestWEBP')
    db = JobStore(tmp_path / 'jobs.db')
    class Media:
        calls = 0
        def upload_image(self, path):
            self.calls += 1
            raise ApiError('temporary')
    api = Media()
    with pytest.raises(ApiError):
        upload_once(db, api, 'https://shop.test', path)
    with pytest.raises(ValueError):
        upload_once(JobStore(db.path), api, 'https://shop.test', path)
    assert api.calls == 1


def test_successful_upload_is_reused_without_another_post(tmp_path):
    path = tmp_path / 'photo.webp'
    path.write_bytes(b'RIFFtestWEBP')
    db = JobStore(tmp_path / 'jobs.db')
    class Media:
        calls = 0
        def upload_image(self, path):
            self.calls += 1
            return {'id': 900, 'source_url': 'https://shop.test/900.webp'}
        def get_media(self, mid):
            return {'id': mid, 'source_url': 'https://shop.test/900.webp'}
    api = Media()
    assert upload_once(db, api, 'https://shop.test', path)['id'] == 900
    assert upload_once(db, api, 'https://shop.test', path)['id'] == 900
    assert api.calls == 1


def test_retry_after_http_date_and_invalid_values():
    assert retry_after_seconds('Thu, 01 Jan 1970 00:01:40 GMT', now=90) == 10
    assert retry_after_seconds('bad') == 0
