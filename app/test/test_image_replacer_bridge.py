"""The page-facing workflow, driven end to end against the offline demo store."""
import json
import time

import pytest

from app.image_replacer.bridge import Bridge
from app.image_replacer.core import ApiError, gallery_ids
from app.image_replacer.demo import DemoApi, SHARED_OLD_IMAGE


def wait_until(check, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(.01)
    raise AssertionError('condition was not reached in time')


def ok(result):
    assert result['ok'], result.get('error')
    return result['data']


@pytest.fixture
def demo(tmp_path, monkeypatch):
    """A connected bridge whose demo store answers instantly."""
    api = DemoApi(delay=0)
    monkeypatch.setattr('app.image_replacer.demo.DemoApi', lambda: api)
    monkeypatch.setattr('app.image_replacer.bridge.DEMO_SETTINGS', {'interval': 0, 'delays': [0, 0, 0], 'max_retries': 3})
    bridge = Bridge(data_dir=tmp_path)
    ok(bridge.start_demo())
    yield bridge, api
    assert bridge._shutdown(timeout=20)


def scan(bridge, scope='any'):
    ok(bridge.start_scan(scope))
    wait_until(lambda: not bridge._scan_running())
    state = ok(bridge.state())['scan']
    assert state['state'] == 'done', state
    return state['result']


def choose_old(bridge):
    products = ok(bridge.search_products('DEMO-001'))
    assert [row['id'] for row in products] == [1]
    return ok(bridge.select_old(1, SHARED_OLD_IMAGE))


def run_to_end(bridge):
    wait_until(lambda: not bridge._job_running())


def prepared(bridge, new_id=900):
    choose_old(bridge)
    scan(bridge)
    ok(bridge.select_new(new_id))
    return ok(bridge.preview())


def test_nothing_is_written_before_explicit_confirmation(demo):
    bridge, api = demo
    rows = prepared(bridge)
    selectable = [row['id'] for row in rows if not row['blocked']]
    for refused in (False, None, 'yes', 1):
        result = bridge.start_job(selectable, refused)
        assert not result['ok']
    assert api.writes == []
    assert ok(bridge.jobs()) == []


def test_full_flow_replaces_only_selected_products_and_keeps_order(demo):
    bridge, api = demo
    rows = prepared(bridge)
    blocked = [row for row in rows if row['blocked']]
    assert [row['id'] for row in blocked] == [17]  # Already contains the new image.
    assert next(row for row in rows if row['id'] == 13)['main_image_changes']
    assert next(row for row in rows if row['id'] == 9)['notice']
    assert all(image['src'] for row in rows for image in row['before_images'] + row['after_images'])
    chosen = [row['id'] for row in rows if not row['blocked'] and row['id'] != 5]
    job_id = ok(bridge.start_job(chosen, True))
    run_to_end(bridge)
    detail = ok(bridge.job_detail(job_id))
    assert detail['job']['state'] == 'completed'
    assert detail['job']['summary']['verified'] == len(chosen)
    assert sorted(pid for pid, _ in api.writes) == sorted(chosen)
    assert gallery_ids(api.get_product(5)) == [1050, SHARED_OLD_IMAGE, 1051, 1052]  # Deselected: untouched.
    assert gallery_ids(api.get_product(1)) == [1010, 900, 1011, 1012]
    assert api.get_media(SHARED_OLD_IMAGE)['id'] == SHARED_OLD_IMAGE  # Old media still exists.
    assert detail['job']['scan']['scope'] == 'any'


def test_blocked_product_cannot_be_forced_into_the_queue(demo):
    bridge, api = demo
    prepared(bridge)
    assert not bridge.start_job([17], True)['ok']
    assert api.writes == []


def test_same_approval_cannot_be_queued_twice(demo):
    bridge, api = demo
    prepared(bridge)
    ok(bridge.start_job([1], True))
    run_to_end(bridge)
    assert not bridge.start_job([1], True)['ok']
    assert len(api.writes) == 1


def test_old_image_cannot_be_chosen_as_new_and_must_belong_to_the_product(demo):
    bridge, _ = demo
    choose_old(bridge)
    assert not bridge.select_new(SHARED_OLD_IMAGE)['ok']
    assert not bridge.select_old(1, 424242)['ok']
    assert not bridge.select_old(999, SHARED_OLD_IMAGE)['ok']


def test_preview_requires_a_finished_scan_and_both_images(demo):
    bridge, _ = demo
    assert not bridge.preview()['ok']
    choose_old(bridge)
    ok(bridge.select_new(900))
    assert not bridge.preview()['ok']
    scan(bridge)
    assert ok(bridge.preview())


def test_changing_the_old_image_discards_scan_and_preview(demo):
    bridge, _ = demo
    prepared(bridge)
    choose_old(bridge)
    state = ok(bridge.state())
    assert state['scan']['state'] == 'idle' and not state['preview_ready'] and state['new'] is None
    assert not bridge.start_job([1], True)['ok']


def test_scan_scope_is_respected_and_reported(demo):
    bridge, _ = demo
    choose_old(bridge)
    result = scan(bridge, 'draft')
    assert result['scope'] == 'draft'
    assert result['scanned'] == 4  # Products 10, 20, 30 and 40.


def test_stop_pauses_and_resume_needs_confirmation(demo):
    bridge, api = demo
    rows = prepared(bridge)
    chosen = [row['id'] for row in rows if not row['blocked']]
    original = api.put_images
    def write_then_stop(pid, ids):
        result = original(pid, ids)
        bridge.stop_job()
        return result
    api.put_images = write_then_stop
    job_id = ok(bridge.start_job(chosen, True))
    run_to_end(bridge)
    job = ok(bridge.job_detail(job_id))['job']
    assert job['state'] == 'paused' and job['can_resume']
    assert job['summary']['verified'] == 1
    api.put_images = original
    assert not bridge.resume_job(job_id, False)['ok']
    ok(bridge.resume_job(job_id, True))
    run_to_end(bridge)
    assert ok(bridge.job_detail(job_id))['job']['state'] == 'completed'


def test_rollback_restores_galleries_once_and_respects_later_edits(demo):
    bridge, api = demo
    prepared(bridge)
    job_id = ok(bridge.start_job([1, 13], True))
    run_to_end(bridge)
    api._products[13]['images'].append({'id': 901})  # Someone edits product 13 afterwards.
    assert not bridge.start_rollback(job_id, False)['ok']
    undo = ok(bridge.start_rollback(job_id, True))
    run_to_end(bridge)
    assert gallery_ids(api.get_product(1)) == [1010, SHARED_OLD_IMAGE, 1011, 1012]
    assert 900 in gallery_ids(api.get_product(13))  # Not overwritten.
    summary = ok(bridge.job_detail(undo))['job']['summary']
    assert (summary['verified'], summary['review']) == (1, 1)
    assert not ok(bridge.job_detail(job_id))['job']['can_rollback']
    assert not bridge.start_rollback(job_id, True)['ok']


def test_interrupted_job_is_shown_as_paused_after_restart(demo, tmp_path):
    bridge, api = demo
    prepared(bridge)
    job_id = ok(bridge.start_job([1], True))
    run_to_end(bridge)
    bridge._journal.set_job(job_id, 'running')  # As left behind by a killed process.
    bridge._journal.update_item(job_id, 1, state='writing')
    restarted = Bridge(data_dir=tmp_path)
    ok(restarted.start_demo())
    job = ok(restarted.jobs())[0]
    assert job['state'] == 'paused' and job['can_resume'] and not job['running']
    assert len(api.writes) == 1  # Opening the app wrote nothing.


def test_upload_goes_through_the_once_only_ledger(demo, tmp_path):
    bridge, _ = demo
    choose_old(bridge)
    picture = tmp_path / 'new.webp'
    picture.write_bytes(b'RIFFdemoWEBP')
    bridge._pick_file = lambda types: str(picture)
    first = ok(bridge.upload_new())['new']['id']
    assert ok(bridge.upload_new())['new']['id'] == first
    bridge._pick_file = lambda types: None
    assert ok(bridge.upload_new()) is None


def test_report_files_are_written_without_credentials(demo):
    bridge, _ = demo
    prepared(bridge)
    job_id = ok(bridge.start_job([1], True))
    run_to_end(bridge)
    exported = ok(bridge.export_report(job_id))
    folder = bridge._journal.path.parent / 'reports'
    text = ''.join((folder / name).read_text(encoding='utf-8-sig') for name in exported['files'])
    assert json.loads((folder / exported['files'][1]).read_text(encoding='utf-8'))['summary']['verified'] == 1
    for word in ('consumer', 'secret', 'password', 'ck_', 'cs_'):
        assert word not in text.lower()


def test_every_answer_is_json_and_actions_need_a_connection(tmp_path):
    bridge = Bridge(data_dir=tmp_path)
    for call in (lambda: bridge.search_products('x'), bridge.jobs, bridge.preview, bridge.upload_new,
            lambda: bridge.start_scan('any'), lambda: bridge.start_job([1], True)):
        result = call()
        assert result['ok'] is False and result['error']
        json.dumps(result)
    json.dumps(bridge.state())


# ───────────────────────── live connection path ─────────────────────────

class ReadOnlyApi:
    def __init__(self, config, fail=None):
        self.config, self.fail, self.calls, self.closed = config, fail, [], False

    def list_products(self, page, per_page, status):
        self.calls.append(('list', page, per_page))
        if self.fail:
            raise self.fail
        from app.image_replacer.core import Page
        return Page([], 0)

    def close(self):
        self.closed = True


def write_env(tmp_path, extra=''):
    path = tmp_path / '.env'
    path.write_text('WC_BASE_URL=https://shop.test\nWC_CONSUMER_KEY=ck_private\nWC_CONSUMER_SECRET=cs_private\n' + extra,
        encoding='utf-8')
    return path


def test_connect_reads_only_and_never_returns_keys(tmp_path):
    made = []
    bridge = Bridge(data_dir=tmp_path, api_factory=lambda config: made.append(ReadOnlyApi(config)) or made[-1])
    state = ok(bridge.connect(str(write_env(tmp_path))))
    assert made[0].calls == [('list', 1, 1)]
    assert state['store'] == 'https://shop.test' and state['connected'] and not state['demo']
    assert not state['upload_ready']
    assert 'private' not in json.dumps(state) + json.dumps(bridge.state())
    assert not bridge.upload_new()['ok']


def test_failed_connection_keeps_the_app_disconnected(tmp_path):
    made = []
    bridge = Bridge(data_dir=tmp_path,
        api_factory=lambda config: made.append(ReadOnlyApi(config, ApiError('auth'))) or made[-1])
    result = bridge.connect(str(write_env(tmp_path)))
    assert not result['ok'] and 'private' not in result['error']
    assert made[0].closed
    assert not ok(bridge.state())['connected']
    assert not bridge.connect(str(tmp_path / 'missing.env'))['ok']


def test_jobs_of_another_store_are_not_listed_or_resumable(demo, tmp_path):
    bridge, _ = demo
    prepared(bridge)
    job_id = ok(bridge.start_job([1], True))
    run_to_end(bridge)
    other = Bridge(data_dir=tmp_path, api_factory=ReadOnlyApi)
    ok(other.connect(str(write_env(tmp_path))))
    assert ok(other.jobs()) == []
    assert not other.resume_job(job_id, True)['ok']
    assert not other.open_link('https://evil.test/x')['ok']
