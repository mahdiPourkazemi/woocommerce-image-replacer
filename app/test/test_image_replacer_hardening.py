"""Acceptance-table gaps: scan edges, queue endings, upload ledger, HTTP boundary.

Every test uses simulated responses and a temporary SQLite file; nothing here
can reach a real store.
"""
from copy import deepcopy
import inspect
from pathlib import Path
import threading

import pytest
import requests

from app.image_replacer import transport
from app.image_replacer.config import Config, normalize_store
from app.image_replacer.core import ApiError, Page, gallery_ids, preview_product, scan_products
from app.image_replacer.engine import ReplacementEngine
from app.image_replacer.journal import JobStore
from app.image_replacer.media import upload_once
from app.image_replacer.transport import ImageApi

STORE = 'https://shop.test'
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def product(pid=1, images=(101, 200, 301), **extra):
    row = {'id': pid, 'name': f'محصول {pid}', 'sku': '', 'status': 'publish', 'type': 'simple',
        'permalink': f'{STORE}/?p={pid}', 'date_modified_gmt': '2026-11-16T10:00:00',
        'images': [{'id': image} for image in images]}
    row.update(extra)
    return row


class FakeStore:
    def __init__(self, products):
        self.products = {row['id']: deepcopy(row) for row in products}
        self.writes = []
        self.read_errors = {}
        self.write_errors = []

    def get_product(self, pid):
        if pid in self.read_errors:
            raise self.read_errors[pid]
        return deepcopy(self.products[pid])

    def put_images(self, pid, ids):
        self.writes.append(pid)
        if self.write_errors:
            raise self.write_errors.pop(0)
        self.products[pid]['images'] = [{'id': value} for value in ids]
        return deepcopy(self.products[pid])


@pytest.fixture
def db(tmp_path):
    return JobStore(tmp_path / 'jobs.sqlite3')


def make_job(db, products, **kwargs):
    rows = [preview_product(row, 200, 900) for row in products]
    return db.create_job(STORE, 200, 900, rows, [row['id'] for row in rows],
        settings={'interval': 0, 'delays': [0, 0, 0], 'max_retries': 3}, **kwargs)


def engine(db, api, **kwargs):
    return ReplacementEngine(db, api, STORE, **kwargs)


# ───────────────────────── scan and preview ─────────────────────────

class Pages:
    def __init__(self, pages, total=None):
        self.pages, self.total, self.requested = pages, total, []

    def list_products(self, page, per_page, status):
        self.requested.append(page)
        return Page(self.pages.get(page, []), self.total)


def test_scan_returns_only_products_that_reference_the_old_image():
    source = Pages({1: [product(1), product(2, images=(101, 301)), product(3, images=(200,))]}, total=1)
    found = scan_products(source, 200)
    assert [row['id'] for row in found['products']] == [1, 3]
    assert found['scanned'] == 3


def test_scan_without_pagination_header_continues_until_a_short_page():
    full = [product(pid) for pid in range(1, 21)]
    source = Pages({1: full, 2: [product(21)]})
    found = scan_products(source, 200, per_page=20)
    assert source.requested == [1, 2]
    assert found['scanned'] == 21


def test_scan_refuses_a_site_that_repeats_the_same_page():
    full = [product(pid) for pid in range(1, 21)]
    with pytest.raises(ValueError):
        scan_products(Pages({1: full, 2: full}), 200, per_page=20)


def test_stopped_scan_never_yields_a_partial_list():
    stop = threading.Event()
    stop.set()
    with pytest.raises(InterruptedError):
        scan_products(Pages({1: [product()]}, total=1), 200, stop=stop)


def test_scan_reports_progress_and_rejects_unknown_scope():
    seen = []
    scan_products(Pages({1: [product(1)], 2: [product(2)]}, total=2), 200, progress=seen.append)
    assert seen[-1] == {'pages': 2, 'scanned': 2, 'found': 2}
    with pytest.raises(ValueError):
        scan_products(Pages({}), 200, status='trash')


@pytest.mark.parametrize('bad_item', ['not-a-product', None, 7])
def test_scan_rejects_non_product_rows_with_a_readable_error(bad_item):
    with pytest.raises(ValueError):
        scan_products(Pages({1: [bad_item]}, total=1), 200)


@pytest.mark.parametrize('images', [5, 'text', None, {'id': 200}])
def test_scan_survives_products_whose_images_field_is_not_a_list(images):
    source = Pages({1: [product(1, images=()) | {'images': images}, product(2)]}, total=1)
    assert [row['id'] for row in scan_products(source, 200)['products']] == [2]


def test_malformed_gallery_containing_old_image_is_found_then_blocked():
    broken = product(1) | {'images': [{'id': 200}, 'junk']}
    found = scan_products(Pages({1: [broken]}, total=1), 200)['products']
    assert len(found) == 1
    assert preview_product(found[0], 200, 900)['blocked']


def test_preview_flags_main_image_change_and_uncovered_variation_images():
    row = preview_product(product(images=(200, 301), type='variable'), 200, 900)
    assert row['main_image_changes'] is True
    assert row['type'] == 'variable' and row['status'] == 'publish'
    assert 'تنوع' in row['notice']
    plain = preview_product(product(images=(101, 200)), 200, 900)
    assert plain['main_image_changes'] is False
    assert plain['notice'] == ''


def test_product_without_sku_is_previewed_and_edited_by_internal_id(db):
    api = FakeStore([product(5)])
    job = make_job(db, [product(5)])
    engine(db, api).run(job)
    assert api.writes == [5]
    assert gallery_ids(api.get_product(5)) == [101, 900, 301]


# ───────────────────────── queue behaviour ─────────────────────────

def test_crash_before_server_applied_write_is_retried_exactly_once(db):
    api = FakeStore([product()])
    job = make_job(db, [product()])
    db.mark_writing(job, 1)  # App died after the durable marker, before the server saved.
    engine(JobStore(db.path), api).run(job)
    assert api.writes == [1]
    assert db.summary(job)['verified'] == 1


def test_auth_error_on_write_pauses_and_resume_finishes(db):
    api = FakeStore([product(1), product(2)])
    api.write_errors = [ApiError('auth')]
    job = make_job(db, [product(1), product(2)])
    engine(db, api).run(job)
    assert db.job(job)['state'] == 'paused'
    assert db.summary(job)['pending'] == 2
    engine(db, api).run(job)
    assert db.job(job)['state'] == 'completed'
    assert db.summary(job)['verified'] == 2


def test_deleted_product_fails_alone_and_queue_continues(db):
    api = FakeStore([product(1), product(2)])
    api.read_errors[1] = ApiError('invalid')
    job = make_job(db, [product(1), product(2)])
    engine(db, api).run(job)
    assert api.writes == [2]
    assert db.summary(job) == {'verified': 1, 'review': 0, 'failed': 1, 'selected': 2, 'pending': 0}
    assert db.job(job)['state'] == 'partial'


def test_unreadable_product_stays_pending_instead_of_being_called_failed(db):
    api = FakeStore([product()])
    api.read_errors[1] = ApiError('temporary')
    job = make_job(db, [product()])
    engine(db, api).run(job)
    assert not api.writes
    assert db.job(job)['state'] == 'paused'
    assert db.summary(job)['pending'] == 1


def test_three_consecutive_failures_pause_the_queue(db):
    products = [product(pid) for pid in range(1, 6)]
    api = FakeStore(products)
    for pid in (1, 2, 3):
        api.read_errors[pid] = ApiError('invalid')
    job = make_job(db, products)
    engine(db, api).run(job)
    assert db.job(job)['state'] == 'paused'
    assert db.summary(job)['failed'] == 3
    assert db.summary(job)['pending'] == 2
    engine(db, api).run(job)  # An explicit resume gets a fresh failure budget.
    assert db.summary(job)['verified'] == 2
    assert db.job(job)['state'] == 'partial'


def test_review_items_do_not_count_as_server_failures(db):
    products = [product(pid) for pid in range(1, 6)]
    api = FakeStore(products)
    job = make_job(db, products)
    for pid in (1, 2, 3):
        api.products[pid]['images'] = [{'id': 555}]
    engine(db, api).run(job)
    assert db.summary(job) == {'verified': 2, 'review': 3, 'failed': 0, 'selected': 5, 'pending': 0}
    assert db.job(job)['state'] == 'partial'


def test_stop_during_last_product_still_ends_as_completed(db):
    api = FakeStore([product()])
    job = make_job(db, [product()])
    stop = threading.Event()
    original = api.put_images
    def save_then_stop(pid, ids):
        result = original(pid, ids)
        stop.set()
        return result
    api.put_images = save_then_stop
    engine(db, api, stop=stop).run(job)
    assert db.summary(job)['pending'] == 0
    assert db.job(job)['state'] == 'completed'


def test_no_interval_wait_after_the_final_product(db):
    api = FakeStore([product(1), product(2)])
    job = make_job(db, [product(1), product(2)])
    waits = []
    engine(db, api, wait=lambda seconds: waits.append(seconds) or False).run(job)
    assert len(waits) == 1


def test_empty_retry_delay_list_does_not_crash_the_queue(db):
    api = FakeStore([product()])
    api.write_errors = [ApiError('temporary')]
    rows = [preview_product(product(), 200, 900)]
    job = db.create_job(STORE, 200, 900, rows, [1], settings={'interval': 0, 'delays': []})
    engine(db, api, wait=lambda seconds: False, clock=lambda: 10**12).run(job)
    assert db.summary(job)['verified'] == 1


def test_second_worker_is_refused_while_a_queue_is_running(db):
    with db.execution_lock():
        with pytest.raises(RuntimeError):
            with JobStore(db.path).execution_lock():
                pass
    with db.execution_lock():
        pass  # Released after the first worker exits.


def test_job_keeps_scan_scope_and_report_holds_no_credentials(db):
    job = make_job(db, [product()], scan={'scope': 'publish', 'scanned_at': '2026-11-16T10:00:00+00:00'})
    report = db.report(job)
    assert report['job']['settings']['scan']['scope'] == 'publish'
    assert report['items'][0]['before'] == [101, 200, 301]
    assert report['items'][0]['after'] == [101, 900, 301]
    flat = repr(report).lower()
    for word in ('consumer', 'secret', 'password', 'authorization'):
        assert word not in flat


# ───────────────────────── rollback ─────────────────────────

def test_rollback_covers_only_verified_items_and_is_single_use(db):
    api = FakeStore([product(1), product(2)])
    job = make_job(db, [product(1), product(2)])
    api.products[2]['images'] = [{'id': 555}]  # Becomes a review item.
    engine(db, api).run(job)
    undo = db.create_rollback(job)
    assert [row['pid'] for row in db.items(undo)] == [1]
    with pytest.raises(ValueError):
        db.create_rollback(job)
    with pytest.raises(ValueError):
        db.create_rollback(undo)
    engine(db, api).run(undo)
    assert gallery_ids(api.get_product(1)) == [101, 200, 301]
    assert gallery_ids(api.get_product(2)) == [555]


def test_rollback_needs_at_least_one_verified_item(db):
    job = make_job(db, [product()])
    with pytest.raises(ValueError):
        db.create_rollback(job)


# ───────────────────────── upload ledger ─────────────────────────

class Uploader:
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), 0

    def upload_image(self, path):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.mark.parametrize('rejection', [ApiError('auth'), ApiError('invalid'), ApiError('rate'), ValueError('bad file')])
def test_definite_upload_rejection_does_not_block_a_later_attempt(db, tmp_path, rejection):
    path = tmp_path / 'photo.webp'
    path.write_bytes(b'RIFFtestWEBP')
    api = Uploader([rejection, {'id': 900}])
    with pytest.raises(type(rejection)):
        upload_once(db, api, STORE, path)
    assert upload_once(db, api, STORE, path)['id'] == 900
    assert api.calls == 2


def test_upload_without_confirmed_id_stays_uncertain(db, tmp_path):
    path = tmp_path / 'photo.webp'
    path.write_bytes(b'RIFFtestWEBP')
    api = Uploader([{'id': None}])
    with pytest.raises(ValueError):
        upload_once(db, api, STORE, path)
    with pytest.raises(ValueError):
        upload_once(db, api, STORE, path)
    assert api.calls == 1


def test_user_can_clear_an_uncertain_upload_after_checking_the_library(db, tmp_path):
    path = tmp_path / 'photo.webp'
    path.write_bytes(b'RIFFtestWEBP')
    api = Uploader([ApiError('temporary'), {'id': 901}])
    with pytest.raises(ApiError):
        upload_once(db, api, STORE, path)
    digest = __import__('hashlib').sha256(path.read_bytes()).hexdigest()
    db.release_upload(STORE, digest)
    assert upload_once(db, api, STORE, path)['id'] == 901


def test_upload_ledger_is_per_store(db, tmp_path):
    path = tmp_path / 'photo.webp'
    path.write_bytes(b'RIFFtestWEBP')
    api = Uploader([ApiError('temporary'), {'id': 902}])
    with pytest.raises(ApiError):
        upload_once(db, api, STORE, path)
    assert upload_once(db, api, 'https://other.test', path)['id'] == 902


# ───────────────────────── HTTP boundary ─────────────────────────

class Response:
    def __init__(self, data=None, status=200, headers=None):
        self.status_code, self.headers, self._data = status, headers or {}, data

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


class Session:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        outcome = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def api_with(*responses, wp=False):
    config = Config(STORE, 'ck_test', 'cs_test', *(('editor', 'app-pass') if wp else ()))
    session = Session(*responses)
    return ImageApi(config, session=session), session


@pytest.mark.parametrize('status,code', [(401, 'auth'), (403, 'auth'), (500, 'temporary'), (503, 'temporary'),
    (408, 'temporary'), (404, 'invalid'), (400, 'invalid'), (301, 'invalid')])
def test_http_status_is_classified_without_leaking_the_remote_body(status, code):
    api, _ = api_with(Response({'message': 'cs_test leaked'}, status))
    with pytest.raises(ApiError) as caught:
        api.get_product(1)
    assert caught.value.code == code
    assert 'cs_test' not in str(caught.value)


def test_network_failure_is_uncertain_and_hides_url_and_keys():
    api, _ = api_with(requests.ConnectionError(f'{STORE}/wp-json?consumer_secret=cs_test'))
    with pytest.raises(ApiError) as caught:
        api.put_images(1, [5])
    assert caught.value.code == 'temporary'
    assert 'cs_test' not in str(caught.value) and STORE not in str(caught.value)
    assert caught.value.__cause__ is None


def test_non_json_success_is_uncertain_not_success():
    api, _ = api_with(Response(ValueError('html page')))
    with pytest.raises(ApiError) as caught:
        api.put_images(1, [5])
    assert caught.value.code == 'temporary'


def test_single_product_read_and_write_request_only_needed_fields():
    api, session = api_with(Response({'id': 7, 'images': []}))
    api.get_product(7)
    api.put_images(7, [5])
    for _, _, kwargs in session.calls:
        assert 'images' in kwargs['params']['_fields'].split(',')
        assert 'description' not in kwargs['params']['_fields']
    assert session.calls[1][2]['json'] == {'images': [{'id': 5}]}


def test_product_identity_mismatch_is_rejected():
    api, _ = api_with(Response({'id': 8, 'images': []}))
    with pytest.raises(ApiError):
        api.get_product(7)


def test_transport_never_repeats_a_write():
    api, session = api_with(Response({}, 500))
    with pytest.raises(ApiError):
        api.put_images(1, [5])
    assert len(session.calls) == 1


def test_products_use_wc_keys_and_media_never_receives_them():
    api, session = api_with(Response([]), wp=True)
    api.list_products()
    api.search_media('x')
    products_auth, media_auth = (call[2]['auth'] for call in session.calls)
    assert (products_auth.username, products_auth.password) == ('ck_test', 'cs_test')
    assert (media_auth.username, media_auth.password) == ('editor', 'app-pass')
    api, session = api_with(Response([]))
    api.search_media('x')
    assert session.calls[0][2]['auth'] is None
    assert '/wp-json/wp/v2/media' in session.calls[0][1]


def test_upload_without_upload_permission_sends_no_post(tmp_path):
    path = tmp_path / 'photo.webp'
    path.write_bytes(b'RIFFtestWEBP')
    api, session = api_with(Response({'id': 3, 'capabilities': {'upload_files': False}}), wp=True)
    with pytest.raises(ApiError) as caught:
        api.upload_image(path)
    assert caught.value.code == 'auth'
    assert [call[0] for call in session.calls] == ['GET']


def test_upload_posts_once_with_media_credentials_and_returns_id(tmp_path):
    path = tmp_path / 'عکس جدید.webp'
    path.write_bytes(b'RIFFtestWEBP')
    api, session = api_with(Response({'id': 3, 'capabilities': {'upload_files': True}}),
        Response({'id': 900, 'source_url': f'{STORE}/900.webp'}, 201), wp=True)
    assert api.upload_image(path)['id'] == 900
    assert [call[0] for call in session.calls] == ['GET', 'POST']
    post = session.calls[1][2]
    assert post['auth'].username == 'editor'
    post['headers']['Content-Disposition'].encode('latin-1')  # Header must stay ASCII-safe.


def test_upload_rejects_wrong_type_before_any_post(tmp_path):
    path = tmp_path / 'notes.txt'
    path.write_text('x')
    api, session = api_with(Response({'id': 3, 'capabilities': {'upload_files': True}}), wp=True)
    with pytest.raises(ValueError):
        api.upload_image(path)
    assert [call[0] for call in session.calls] == ['GET']


def test_selected_media_must_be_an_image_of_this_store():
    api, _ = api_with(Response({'id': 9, 'media_type': 'file', 'mime_type': 'application/pdf'}))
    with pytest.raises(ApiError):
        api.get_media(9)
    api, _ = api_with(Response({'id': 9, 'media_type': 'image', 'mime_type': 'image/webp'}))
    assert api.get_media(9)['id'] == 9


def test_search_by_sku_then_name_and_names_with_colon_are_not_links():
    api, session = api_with(Response([]), Response([{'id': 4}]))
    assert api.search_products('جعبه: مدل ۲') == [{'id': 4}]
    assert 'sku' in session.calls[0][2]['params'] and 'search' in session.calls[1][2]['params']
    api, session = api_with(Response([]), Response([{'id': 4}]))
    assert api.search_products('Box: large') == [{'id': 4}]


def test_search_by_own_link_reads_product_and_foreign_link_is_refused():
    api, session = api_with(Response({'id': 12, 'images': []}))
    assert api.search_products(f'{STORE}/?p=12')[0]['id'] == 12
    api, session = api_with(Response([{'id': 5}]))
    assert api.search_products(f'{STORE}/product/my-box/') == [{'id': 5}]
    assert session.calls[0][2]['params']['slug'] == 'my-box'
    for link in ('https://evil.test/?p=12', 'http://shop.test/?p=12'):
        api, session = api_with(Response([]))
        with pytest.raises(ValueError):
            api.search_products(link)
        assert not session.calls


def test_search_rejects_non_list_answers():
    api, _ = api_with(Response({'code': 'weird'}))
    with pytest.raises(ApiError):
        api.search_products('abc')


def test_capability_has_no_delete_or_product_create_route():
    source = inspect.getsource(transport)
    assert 'DELETE' not in source.upper().replace('NEVER DELETE', '')
    assert "'POST', 'products" not in source
    api, session = api_with(Response([]), wp=True)
    api.list_products()
    api.search_products('abc')
    api.search_media()
    assert {call[0] for call in session.calls} == {'GET'}


# ───────────────────────── configuration ─────────────────────────

@pytest.mark.parametrize('raw,expected', [
    ('https://Shop.Test/', 'https://shop.test'),
    ('https://shop.test:443/store/', 'https://shop.test/store'),
    ('https://shop.test:8443', 'https://shop.test:8443'),
])
def test_store_address_is_normalized(raw, expected):
    assert normalize_store(raw) == expected


def test_env_without_keys_is_refused_and_media_credentials_are_optional(tmp_path):
    path = tmp_path / '.env'
    path.write_text('WC_BASE_URL=https://shop.test\nWC_CONSUMER_KEY=\nWC_CONSUMER_SECRET=x\n', encoding='utf-8')
    with pytest.raises(ValueError):
        Config.load(path)
    path.write_text('WC_BASE_URL=https://shop.test\nWC_CONSUMER_KEY=k\nWC_CONSUMER_SECRET=s\n'
        'WP_USERNAME=editor\nWP_APPLICATION_PASSWORD=abcd efgh\n', encoding='utf-8')
    config = Config.load(path)
    assert (config.wp_user, config.wp_password) == ('editor', 'abcd efgh')
    assert 'abcd' not in repr(config)
    assert config.timeout == 120


def test_env_example_names_every_variable_and_contains_no_secret_values():
    values = __import__('dotenv').dotenv_values(PROJECT_ROOT / '.env.example')
    for name in ('WC_BASE_URL', 'WC_CONSUMER_KEY', 'WC_CONSUMER_SECRET', 'WP_USERNAME', 'WP_APPLICATION_PASSWORD'):
        assert name in values
    for name in ('WC_CONSUMER_KEY', 'WC_CONSUMER_SECRET', 'WP_APPLICATION_PASSWORD', 'CONSUMER_KEY', 'CONSUMER_SECRET'):
        assert not values.get(name)
    assert normalize_store(values['WC_BASE_URL'])
