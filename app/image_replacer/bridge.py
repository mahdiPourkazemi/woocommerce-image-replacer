"""Page-facing API: every public method is callable from the UI and returns JSON.

Rules this layer enforces on top of the engine:
- connecting, searching, scanning and previewing only ever READ from the store;
- a queue starts only from a call that carries an explicit confirmation;
- the queue receives the preview rows the user saw, never freshly fetched ones;
- nothing returned to the page or written to a report contains a credential.

Names starting with an underscore are invisible to the page (pywebview rule).
"""
import csv
from datetime import datetime
from functools import wraps
import json
import logging
import threading
from urllib.parse import urlsplit
import webbrowser

from .config import Config, data_directory
from .core import ApiError, media_id, preview_product, scan_products
from .engine import ReplacementEngine
from .journal import JobStore
from .media import upload_once

log = logging.getLogger(__name__)

LIVE_SETTINGS = {'interval': 1, 'delays': [5, 15, 45], 'max_retries': 3}
DEMO_SETTINGS = {'interval': .3, 'delays': [1, 2, 3], 'max_retries': 3}
IMAGE_FILE_TYPES = ('Image files (*.png;*.jpg;*.jpeg;*.webp)',)
ENV_FILE_TYPES = ('Settings (*.env;*.txt)', 'All files (*.*)')


def exposed(method):
    """Turn expected problems into a Persian message; hide unexpected detail."""
    @wraps(method)
    def call(self, *args):
        try:
            return {'ok': True, 'data': method(self, *args)}
        except (ValueError, ApiError, InterruptedError) as error:
            return {'ok': False, 'error': str(error)}
        except Exception as error:  # noqa: BLE001 - the page must always get an answer.
            # Only the type is logged: exception text may quote a request URL.
            log.error('bridge call %s failed: %s', method.__name__, type(error).__name__)
            return {'ok': False, 'error': 'خطای داخلی برنامه رخ داد؛ نوع خطا در گزارش محلی ثبت شد.'}
    return call


def _image_rows(product):
    images = product.get('images')
    return [{'id': image.get('id'), 'src': str(image.get('src') or '')}
        for image in (images if isinstance(images, list) else []) if isinstance(image, dict)]


def _product_row(product):
    return {'id': product.get('id'), 'name': str(product.get('name') or ''), 'sku': str(product.get('sku') or ''),
        'permalink': str(product.get('permalink') or ''), 'status': str(product.get('status') or ''),
        'type': str(product.get('type') or ''), 'images': _image_rows(product)}


def _media_row(media):
    title = media.get('title')
    sizes = (media.get('media_details') or {}).get('sizes') or {}
    small = (sizes.get('thumbnail') or sizes.get('medium') or {}).get('source_url')
    return {'id': media.get('id'), 'src': str(small or media.get('source_url') or ''),
        'title': str(title.get('rendered') if isinstance(title, dict) else title or '')}


class Bridge:
    def __init__(self, *, env_path=None, data_dir=None, api_factory=None, pick_file=None):
        self._env_path, self._data_dir = env_path, data_dir
        self._api_factory = api_factory
        self._pick_file = pick_file
        self._window = None
        self._lock = threading.RLock()
        self._api = self._journal = None
        self._store, self._demo, self._upload_ready = '', False, False
        self._worker = self._scan_thread = None
        self._job_stop, self._scan_stop = threading.Event(), threading.Event()
        self._active_job, self._job_error = '', ''
        self._generation = 0
        self._reset_selection()

    # ───────────────────────── internal helpers ─────────────────────────

    def _attach(self, window):
        self._window = window

    def _reset_selection(self):
        """A new store or a new old image makes every earlier finding stale."""
        self._generation += 1
        self._scan_stop.set()
        self._old = self._new = None
        self._found, self._thumbs, self._previews = {}, {}, {}
        self._scan = {'state': 'idle', 'progress': None, 'result': None, 'error': ''}

    def _need_connection(self):
        if self._api is None:
            raise ValueError('ابتدا به فروشگاه وصل شوید یا حالت نمایشی را باز کنید.')

    def _job_running(self):
        return bool(self._worker and self._worker.is_alive())

    def _scan_running(self):
        return bool(self._scan_thread and self._scan_thread.is_alive())

    def _need_idle(self):
        if self._job_running():
            raise ValueError('صف در حال اجراست؛ ابتدا آن را متوقف کنید و منتظر پایان درخواست جاری بمانید.')
        if self._scan_running():
            raise ValueError('اسکن در حال اجراست؛ ابتدا آن را متوقف کنید.')

    def _remember(self, images):
        for image in images:
            if image.get('src'):
                self._thumbs[image['id']] = image['src']

    def _choose_file(self, file_types):
        if self._pick_file:
            return self._pick_file(file_types)
        if self._window is None:
            raise ValueError('انتخاب فایل فقط در پنجرهٔ برنامه ممکن است.')
        import webview
        chosen = self._window.create_file_dialog(webview.FileDialog.OPEN, file_types=file_types)
        return chosen[0] if chosen else None

    def _open_store(self, api, store, *, demo, upload_ready):
        if self._api is not None:
            self._api.close()
        self._api, self._store, self._demo, self._upload_ready = api, store, demo, upload_ready
        folder = self._data_dir or data_directory(demo)
        self._journal = JobStore(folder / 'jobs.sqlite3')
        self._active_job, self._job_error = '', ''
        self._reset_selection()

    def _thumb_list(self, ids):
        return [{'id': mid, 'src': self._thumbs.get(mid, '')} for mid in ids]

    # ───────────────────────── connection ─────────────────────────

    @exposed
    def connect(self, path=''):
        """Load the external .env and prove read access with one read-only request."""
        with self._lock:
            self._need_idle()
            config = Config.load(path or self._env_path)
            if self._api_factory:
                api = self._api_factory(config)
            else:
                from .transport import ImageApi
                api = ImageApi(config)
            try:
                # Write permission is deliberately NOT probed: probing would edit the store.
                api.list_products(1, 1, 'any')
            except BaseException:
                api.close()
                raise
            self._open_store(api, config.store, demo=False,
                upload_ready=bool(config.wp_user and config.wp_password))
            log.info('connected to store host %s', urlsplit(config.store).hostname)
            return self._snapshot()

    @exposed
    def choose_env_file(self):
        path = self._choose_file(ENV_FILE_TYPES)
        if not path:
            return None
        result = self.connect(str(path))
        if not result['ok']:
            raise ValueError(result['error'])
        return result['data']

    @exposed
    def start_demo(self):
        with self._lock:
            self._need_idle()
            from .demo import DemoApi
            api = DemoApi()
            self._open_store(api, api.store, demo=True, upload_ready=True)
            return self._snapshot()

    # ───────────────────────── old image ─────────────────────────

    @exposed
    def search_products(self, query):
        with self._lock:
            self._need_connection()
            api = self._api
        rows = [_product_row(row) for row in api.search_products(query) if isinstance(row, dict)]
        with self._lock:
            for row in rows:
                self._found[row['id']] = row
                self._remember(row['images'])
        return rows

    @exposed
    def select_old(self, product_id, image_id):
        with self._lock:
            self._need_connection()
            self._need_idle()
            product = self._found.get(product_id)
            image = next((item for item in (product or {}).get('images', []) if item['id'] == image_id), None)
            if image is None:
                raise ValueError('این عکس در گالری محصول انتخاب‌شده نیست؛ محصول را دوباره جست‌وجو کنید.')
            media_id(image_id)
            found, thumbs = self._found, self._thumbs
            self._reset_selection()
            self._found, self._thumbs = found, thumbs
            self._old = {'id': image_id, 'src': image['src'], 'product_id': product_id, 'product_name': product['name']}
            return self._snapshot()

    # ───────────────────────── scan ─────────────────────────

    @exposed
    def start_scan(self, scope='any'):
        with self._lock:
            self._need_connection()
            self._need_idle()
            if not self._old:
                raise ValueError('ابتدا عکس مشترک قدیمی را انتخاب کنید.')
            self._previews = {}
            self._scan = {'state': 'running', 'progress': {'pages': 0, 'scanned': 0, 'found': 0}, 'result': None, 'error': ''}
            self._scan_stop = threading.Event()
            self._scan_thread = threading.Thread(target=self._run_scan, name='image-scan', daemon=True,
                args=(self._api, self._old['id'], str(scope), self._scan_stop, self._generation))
            self._scan_thread.start()
            return self._snapshot()

    def _run_scan(self, api, old_id, scope, stop, generation):
        def progress(counts):
            with self._lock:
                if generation == self._generation:
                    self._scan['progress'] = counts
        try:
            outcome = {'state': 'done', 'result': scan_products(api, old_id, status=scope, stop=stop, progress=progress)}
        except InterruptedError as error:
            outcome = {'state': 'stopped', 'error': str(error)}
        except (ValueError, ApiError) as error:
            outcome = {'state': 'error', 'error': str(error)}
        except Exception as error:  # noqa: BLE001
            log.error('scan failed: %s', type(error).__name__)
            outcome = {'state': 'error', 'error': 'اسکن با خطای داخلی متوقف شد؛ فهرست ناقص قابل استفاده نیست.'}
        with self._lock:
            if generation != self._generation:
                return  # The selection changed meanwhile; this result describes something else.
            self._scan.update(outcome)
            for product in (outcome.get('result') or {}).get('products', []):
                self._remember(_image_rows(product))

    @exposed
    def stop_scan(self):
        self._scan_stop.set()
        return True

    # ───────────────────────── new image ─────────────────────────

    @exposed
    def search_media(self, query=''):
        with self._lock:
            self._need_connection()
            api = self._api
        rows = [_media_row(row) for row in api.search_media(query) if isinstance(row, dict)]
        with self._lock:
            self._remember(rows)
        return rows

    def _set_new(self, media):
        row = _media_row(media)
        if self._old and row['id'] == self._old['id']:
            raise ValueError('عکس جدید با عکس قدیمی یکسان است؛ رسانهٔ دیگری انتخاب کنید.')
        self._remember([row])
        self._new, self._previews = row, {}

    @exposed
    def select_new(self, image_id):
        with self._lock:
            self._need_connection()
            self._need_idle()
            if self._old and image_id == self._old['id']:
                raise ValueError('عکس جدید با عکس قدیمی یکسان است؛ رسانهٔ دیگری انتخاب کنید.')
            api = self._api
        media = api.get_media(media_id(image_id))
        with self._lock:
            self._set_new(media)
            return self._snapshot()

    @exposed
    def upload_new(self):
        with self._lock:
            self._need_connection()
            self._need_idle()
            if not self._upload_ready:
                raise ValueError('بارگذاری فعال نیست: WP_USERNAME و WP_APPLICATION_PASSWORD در .env خالی‌اند. '
                    'عکس را از پیشخوان سایت بارگذاری و اینجا از «رسانه‌های موجود» انتخاب کنید.')
            api, journal, store = self._api, self._journal, self._store
        path = self._choose_file(IMAGE_FILE_TYPES)
        if not path:
            return None
        media = upload_once(journal, api, store, path)
        with self._lock:
            self._set_new(media)
            return self._snapshot()

    # ───────────────────────── preview and queue ─────────────────────────

    @exposed
    def preview(self):
        with self._lock:
            self._need_connection()
            if not self._old or not self._new:
                raise ValueError('عکس قدیمی و عکس جدید هر دو باید انتخاب شده باشند.')
            if self._scan['state'] != 'done':
                raise ValueError('ابتدا اسکن را کامل کنید؛ فهرست ناقص برای اجرا پذیرفته نمی‌شود.')
            rows = [preview_product(product, self._old['id'], self._new['id'])
                for product in self._scan['result']['products']]
            self._previews = {row['id']: row for row in rows}
            return [dict(row, before_images=self._thumb_list(row['before']),
                after_images=self._thumb_list(row['after'])) for row in rows]

    def _launch(self, job_id):
        if self._job_running():
            raise ValueError('صف دیگری در حال اجراست؛ ابتدا آن را متوقف کنید.')
        if self._journal.job(job_id)['store'] != self._store:
            raise ValueError('این کار متعلق به فروشگاه دیگری است.')
        self._job_stop = threading.Event()
        self._active_job, self._job_error = job_id, ''
        engine = ReplacementEngine(self._journal, self._api, self._store, stop=self._job_stop)
        self._worker = threading.Thread(target=self._run_job, args=(engine, job_id), name='image-queue', daemon=True)
        self._worker.start()

    def _run_job(self, engine, job_id):
        try:
            engine.run(job_id)
        except (ValueError, ApiError, RuntimeError) as error:
            self._job_error = str(error)
        except Exception as error:  # noqa: BLE001
            log.error('queue stopped: %s', type(error).__name__)
            self._job_error = 'صف با خطای داخلی متوقف شد؛ با «ادامه» وضعیت هر محصول دوباره از سایت خوانده می‌شود.'

    @staticmethod
    def _need_confirmation(confirmed):
        if confirmed is not True:
            raise ValueError('اجرای واقعی فقط پس از تأیید صریح شما آغاز می‌شود.')

    @exposed
    def start_job(self, selected, confirmed=False):
        """Queue exactly the previewed rows the user kept, then start writing."""
        self._need_confirmation(confirmed)
        with self._lock:
            self._need_connection()
            self._need_idle()
            if not self._previews:
                raise ValueError('پیش‌نمایش معتبر وجود ندارد؛ پیش‌نمایش را دوباره بسازید.')
            job_id = self._journal.create_job(self._store, self._old['id'], self._new['id'],
                list(self._previews.values()), list(selected or []),
                settings=DEMO_SETTINGS if self._demo else LIVE_SETTINGS, scan=self._scan['result'])
            # A second click must not queue the same approval twice.
            self._previews = {}
            self._launch(job_id)
            log.info('job %s started with %d products', job_id[:8], len(selected))
            return job_id

    @exposed
    def resume_job(self, job_id, confirmed=False):
        self._need_confirmation(confirmed)
        with self._lock:
            self._need_connection()
            self._need_idle()
            self._launch(str(job_id))
            return job_id

    @exposed
    def start_rollback(self, job_id, confirmed=False):
        self._need_confirmation(confirmed)
        with self._lock:
            self._need_connection()
            self._need_idle()
            undo = self._journal.create_rollback(str(job_id))
            self._launch(undo)
            return undo

    @exposed
    def stop_job(self):
        """Stops the NEXT request; the in-flight one may still complete on the server."""
        self._job_stop.set()
        return True

    # ───────────────────────── reading state ─────────────────────────

    def _job_row(self, job, rollbacks):
        running = self._job_running() and job['id'] == self._active_job
        state = job['state']
        if state == 'running' and not running:
            state = 'paused'  # The app closed mid-run; items are reconciled on resume.
        summary = job['summary'] if 'summary' in job else self._journal.summary(job['id'])
        return {'id': job['id'], 'kind': job['kind'], 'created': job['created'], 'state': state,
            'old_id': job['old_id'], 'new_id': job['new_id'], 'parent': job['parent'] or '',
            'summary': summary, 'running': running, 'scan': job['settings'].get('scan') or {},
            'error': self._job_error if job['id'] == self._active_job else '',
            'can_resume': not running and summary['pending'] > 0,
            'can_rollback': (job['kind'] == 'replace' and not running and summary['verified'] > 0
                and job['id'] not in rollbacks)}

    def _job_rows(self):
        jobs = self._journal.jobs(self._store)
        rollbacks = {job['parent'] for job in jobs if job['kind'] == 'rollback'}
        return [self._job_row(job, rollbacks) for job in jobs]

    @exposed
    def jobs(self):
        with self._lock:
            self._need_connection()
            return self._job_rows()

    @exposed
    def job_detail(self, job_id):
        with self._lock:
            self._need_connection()
            job = next((row for row in self._job_rows() if row['id'] == job_id), None)
            if job is None:
                raise ValueError('کار پیدا نشد.')
            items = [{key: row[key] for key in ('pid', 'name', 'state', 'attempts', 'error', 'before', 'after', 'checked_at')}
                for row in self._journal.items(job_id)]
            return {'job': job, 'items': items}

    def _snapshot(self):
        scan = dict(self._scan)
        if scan['result']:
            scan['result'] = {key: scan['result'][key] for key in ('pages', 'scanned', 'scope', 'scanned_at', 'coverage')}
            scan['result']['found'] = len(self._scan['result']['products'])
        return {'connected': self._api is not None, 'demo': self._demo, 'store': self._store,
            'upload_ready': self._upload_ready, 'old': self._old, 'new': self._new, 'scan': scan,
            'preview_ready': bool(self._previews), 'job_running': self._job_running(),
            'scan_running': self._scan_running(), 'active_job': self._active_job}

    @exposed
    def state(self):
        with self._lock:
            return self._snapshot()

    # ───────────────────────── reports and links ─────────────────────────

    @exposed
    def export_report(self, job_id):
        """Write a CSV (for Excel) and a JSON file; returns the folder and names."""
        with self._lock:
            self._need_connection()
            report = self._journal.report(str(job_id))
            folder = self._journal.path.parent / 'reports'
        folder.mkdir(parents=True, exist_ok=True)
        stem = f"report-{report['job']['kind']}-{job_id[:8]}-{datetime.now():%Y%m%d-%H%M%S}"
        (folder / (stem + '.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        with (folder / (stem + '.csv')).open('w', newline='', encoding='utf-8-sig') as handle:
            writer = csv.writer(handle)
            writer.writerow(['شناسهٔ محصول', 'نام', 'وضعیت', 'تعداد تلاش', 'تصاویر قبل', 'تصاویر بعد', 'زمان بررسی', 'توضیح'])
            for item in report['items']:
                writer.writerow([item['pid'], item['name'], item['state'], item['attempts'],
                    ' '.join(map(str, item['before'])), ' '.join(map(str, item['after'])),
                    item['checked_at'] or '', item['error']])
        return {'folder': str(folder), 'files': [stem + '.csv', stem + '.json'], 'summary': report['summary']}

    @exposed
    def open_link(self, url):
        """Open a product page in the default browser, only on the connected store."""
        with self._lock:
            self._need_connection()
            target, own = urlsplit(str(url)), urlsplit(self._store)
            if self._demo:
                raise ValueError('در حالت نمایشی صفحهٔ واقعی برای محصول وجود ندارد.')
            if target.scheme != 'https' or target.netloc.lower() != own.netloc.lower():
                raise ValueError('فقط لینک‌های همین فروشگاه باز می‌شوند.')
        webbrowser.open(str(url))
        return True

    # ───────────────────────── shutdown ─────────────────────────

    def _busy(self):
        return self._job_running()

    def _shutdown(self, timeout=150):
        """Stop sending new requests and wait for the in-flight one to settle."""
        self._scan_stop.set()
        self._job_stop.set()
        worker = self._worker
        if worker and worker.is_alive():
            worker.join(timeout)
        finished = not (worker and worker.is_alive())
        if finished and self._api is not None:
            self._api.close()
        return finished
