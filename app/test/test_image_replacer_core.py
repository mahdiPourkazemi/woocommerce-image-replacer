"""Safety contracts: mutation of ordering, reconciliation or selection must fail."""
from copy import deepcopy
import threading

import pytest

from app.image_replacer.core import ApiError, Page, gallery_ids, preview_product, scan_products
from app.image_replacer.journal import JobStore
from app.image_replacer.engine import ReplacementEngine


def product(pid=1, images=(101, 200, 301)):
    return {'id': pid, 'name': f'محصول {pid}', 'sku': '', 'status': 'draft',
        'type': 'variable', 'permalink': f'https://shop.test/?p={pid}',
        'date_modified_gmt': '2026-11-16T10:00:00',
        'images': [{'id': image, 'src': f'https://shop.test/{image}.webp'} for image in images]}


class FakeStore:
    """External side effects only; real planner, SQLite and engine stay intact."""
    def __init__(self, products=None):
        self.products = {row['id']: deepcopy(row) for row in (products or [product()])}
        self.writes = []
        self.behavior = None
        self.read_error = None

    def get_product(self, pid):
        if self.read_error:
            raise self.read_error
        return deepcopy(self.products[pid])

    def put_images(self, pid, ids):
        self.writes.append((pid, {'images': [{'id': value} for value in ids]}))
        if self.behavior:
            return self.behavior(pid, ids)
        self.products[pid]['images'] = [{'id': value} for value in ids]
        return deepcopy(self.products[pid])


@pytest.fixture
def db(tmp_path):
    return JobStore(tmp_path / 'jobs.sqlite3')


def make_job(db, products=None, selected=None):
    rows = [preview_product(row, 200, 900) for row in (products or [product()])]
    return db.create_job('https://shop.test', 200, 900, rows, selected or [row['id'] for row in rows],
        settings={'interval': 0, 'delays': [0, 0, 0], 'max_retries': 3})


@pytest.mark.parametrize('before,after,positions', [
    ([101, 200, 301], [101, 900, 301], [1]),
    ([200, 102, 301], [900, 102, 301], [0]),
    ([101, 301, 200], [101, 301, 900], [2]),
    ([200, 101, 200], [900, 101, 900], [0, 2]),
])
def test_only_old_media_positions_change(before, after, positions):
    row = preview_product(product(images=before), 200, 900)
    assert row['before'] == before
    assert row['after'] == after
    assert row['positions'] == positions
    assert not row['blocked']


@pytest.mark.parametrize('images', [(101, 301), (101, 200, 900), (101, 0, 200)])
def test_missing_old_existing_target_and_invalid_media_are_blocked(images):
    assert preview_product(product(images=images), 200, 900)['blocked']


def test_same_image_cannot_be_selected_as_replacement():
    with pytest.raises(ValueError):
        preview_product(product(), 200, 200)


def test_pagination_covers_all_pages_and_deduplicates():
    class Source:
        def list_products(self, page, per_page, status):
            assert per_page == 20 and status == 'any'
            return Page({1: [product(1), product(2)], 2: [product(2), product(3)]}[page], 2)
    found = scan_products(Source(), 200, status='any')
    assert [row['id'] for row in found['products']] == [1, 2, 3]
    assert found['pages'] == 2
    assert found['scope'] == 'any'


def test_incomplete_scan_is_not_presented_as_complete():
    class Source:
        def list_products(self, page, per_page, status):
            return Page([product()] if page == 1 else [], 2)
    with pytest.raises(ValueError):
        scan_products(Source(), 200)


def test_only_selected_products_are_written_and_only_images_field(db):
    products = [product(1), product(2)]
    api = FakeStore(products)
    job = make_job(db, products, [2])
    ReplacementEngine(db, api, 'https://shop.test').run(job)
    assert api.writes == [(2, {'images': [{'id': 101}, {'id': 900}, {'id': 301}]})]
    assert db.summary(job)['verified'] == 1
    assert gallery_ids(api.get_product(1)) == [101, 200, 301]


def test_changed_gallery_becomes_review_without_write(db):
    api = FakeStore()
    job = make_job(db)
    api.products[1]['images'] = [{'id': 400}, {'id': 200}]
    ReplacementEngine(db, api, 'https://shop.test').run(job)
    assert not api.writes
    assert db.summary(job)['review'] == 1


def test_timeout_after_save_is_reconciled_without_duplicate_write(db):
    api = FakeStore()
    job = make_job(db)
    def saved_but_lost(pid, ids):
        api.products[pid]['images'] = [{'id': value} for value in ids]
        raise ApiError('temporary')
    api.behavior = saved_but_lost
    ReplacementEngine(db, api, 'https://shop.test').run(job)
    assert len(api.writes) == 1
    assert db.summary(job)['verified'] == 1


def test_timeout_without_save_has_bounded_reconciled_retries(db):
    api = FakeStore()
    job = make_job(db)
    api.behavior = lambda *_: (_ for _ in ()).throw(ApiError('temporary'))
    ReplacementEngine(db, api, 'https://shop.test').run(job)
    assert len(api.writes) == 4
    assert db.summary(job)['failed'] == 1


def test_restart_recovers_inflight_write_from_real_sqlite(db):
    api = FakeStore([product(images=(101, 900, 301))])
    job = make_job(db)
    db.mark_writing(job, 1)
    reopened = JobStore(db.path)
    ReplacementEngine(reopened, api, 'https://shop.test').run(job)
    assert not api.writes
    assert reopened.summary(job)['verified'] == 1


def test_pause_then_resume_keeps_pending_products(db):
    api = FakeStore([product(1), product(2)])
    job = make_job(db, [product(1), product(2)])
    stop = threading.Event()
    def save_then_stop(pid, ids):
        api.products[pid]['images'] = [{'id': value} for value in ids]
        stop.set()
        return api.get_product(pid)
    api.behavior = save_then_stop
    ReplacementEngine(db, api, 'https://shop.test', stop=stop).run(job)
    assert len(api.writes) == 1
    assert db.summary(job)['pending'] == 1
    assert db.job(job)['state'] == 'paused'
    api.behavior = None
    ReplacementEngine(db, api, 'https://shop.test').run(job)
    assert db.summary(job)['verified'] == 2


def test_auth_failure_pauses_whole_queue(db):
    api = FakeStore([product(1), product(2)])
    api.read_error = ApiError('auth')
    job = make_job(db, [product(1), product(2)])
    ReplacementEngine(db, api, 'https://shop.test').run(job)
    assert not api.writes
    assert db.job(job)['state'] == 'paused'
    assert db.summary(job)['pending'] == 2


def test_rate_limit_wait_uses_retry_after(db):
    api = FakeStore()
    job = make_job(db)
    delays = []
    def once(pid, ids):
        api.behavior = None
        raise ApiError('rate', retry_after=17)
    api.behavior = once
    ReplacementEngine(db, api, 'https://shop.test', clock=lambda: 100,
        wait=lambda seconds: delays.append(seconds) or False).run(job)
    assert any(delay >= 17 for delay in delays)
    assert db.summary(job)['verified'] == 1


def test_response_mismatch_is_read_back_not_assumed_success(db):
    api = FakeStore()
    job = make_job(db)
    def conflict(pid, ids):
        api.products[pid]['images'] = [{'id': 888}]
        return product(images=(777,))
    api.behavior = conflict
    ReplacementEngine(db, api, 'https://shop.test').run(job)
    assert db.summary(job)['review'] == 1
    assert len(api.writes) == 1


def test_rollback_is_conditional_and_never_deletes_media(db):
    api = FakeStore([product(1), product(2)])
    job = make_job(db, [product(1), product(2)])
    engine = ReplacementEngine(db, api, 'https://shop.test')
    engine.run(job)
    undo = db.create_rollback(job)
    api.products[2]['images'] = [{'id': 999}]
    engine.run(undo)
    assert gallery_ids(api.get_product(1)) == [101, 200, 301]
    assert gallery_ids(api.get_product(2)) == [999]
    assert db.summary(undo)['verified'] == 1
    assert db.summary(undo)['review'] == 1


def test_job_cannot_run_against_different_store(db):
    api = FakeStore()
    job = make_job(db)
    with pytest.raises(ValueError):
        ReplacementEngine(db, api, 'https://other.test').run(job)
    assert not api.writes


def test_blocked_preview_cannot_be_queued_even_if_selected(db):
    with pytest.raises(ValueError):
        make_job(db, [product(images=(101, 200, 900))])
