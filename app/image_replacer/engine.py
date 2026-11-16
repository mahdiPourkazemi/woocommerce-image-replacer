"""One image-only writer, durable reconciliation, pause and bounded retries."""
import random
import threading
import time

from .core import ApiError, gallery_ids
from .journal import utc_now


class ReplacementEngine:
    def __init__(self, journal, api, store, *, stop=None, wait=None, clock=time.time):
        self.journal, self.api, self.store = journal, api, store
        self.stop = stop or threading.Event()
        self.wait = wait or self.stop.wait
        self.clock = clock

    def run(self, job_id):
        job = self.journal.job(job_id)
        if job['store'] != self.store:
            raise ValueError('این کار متعلق به فروشگاه دیگری است؛ تنظیمات و انتخاب‌ها را بازخوانی کنید.')
        with self.journal.execution_lock():
            self.journal.set_job(job_id, 'running')
            try:
                # Starting a run is an explicit user decision, so the budget of
                # consecutive failures starts fresh instead of re-pausing at once.
                failures = 0
                pending = [row['pid'] for row in self.journal.items(job_id)
                    if row['state'] not in {'verified', 'review', 'failed'}]
                for index, pid in enumerate(pending):
                    if self.stop.is_set() or not self._process(job, pid):
                        break
                    state = self.journal.item(job_id, pid)['state']
                    # 'review' means the site answered and a human must decide;
                    # only real failures signal a struggling server.
                    failures = failures + 1 if state == 'failed' else 0
                    self.journal.set_job(job_id, 'running', failures)
                    if index == len(pending) - 1:
                        break
                    if failures >= 3 or self.wait(max(0, float(job['settings'].get('interval', 1)))):
                        break
                # The final state follows what is actually left, so a stop that
                # lands after the last product is not reported as an unfinished job.
                counts = self.journal.summary(job_id)
                self.journal.set_job(job_id, 'paused' if counts['pending'] else
                    'completed' if counts['verified'] == counts['selected'] else 'partial')
            except BaseException:
                self.journal.set_job(job_id, 'paused')
                raise
        return self.journal.summary(job_id)

    def _finish(self, job, pid, state, message=''):
        self.journal.update_item(job['id'], pid, state=state, error=message, retry_at=0,
            read_attempts=0, checked_at=utc_now())

    def _delay(self, job, attempts, retry_after=0):
        delays = job['settings'].get('delays') or [5, 15, 45]
        base = float(delays[min(max(attempts - 1, 0), len(delays) - 1)])
        return max(float(retry_after), base + (random.uniform(0, .5) if base else 0))

    def _process(self, job, pid):
        max_attempts = 1 + min(3, max(0, int(job['settings'].get('max_retries', 3))))
        while not self.stop.is_set():
            row = self.journal.item(job['id'], pid)
            remaining = row['retry_at'] - self.clock()
            if remaining > 0 and self.wait(remaining):
                return False
            if self.stop.is_set():
                return False
            try:
                current = gallery_ids(self.api.get_product(pid))
            except ValueError:
                self._finish(job, pid, 'review', 'گالری کامل معتبر دریافت نشد؛ تغییر خودکار متوقف شد.')
                return True
            except ApiError as error:
                self.journal.update_item(job['id'], pid, error=str(error))
                if error.code == 'auth':
                    return False
                if error.code == 'invalid':
                    self._finish(job, pid, 'failed', str(error))
                    return True
                reads = row['read_attempts'] + 1
                if reads >= max_attempts:
                    # An unknown previous write stays unknown; never label it failed.
                    self.journal.update_item(job['id'], pid, read_attempts=0)
                    return False
                self.journal.update_item(job['id'], pid, read_attempts=reads,
                    retry_at=self.clock() + self._delay(job, reads, error.retry_after))
                continue
            if row['state'] == 'writing' and current == row['after']:
                self._finish(job, pid, 'verified')
                return True
            if current != row['before']:
                self._finish(job, pid, 'review', 'گالری پس از تأیید تغییر کرده است؛ تغییر دیگران بازنویسی نشد.')
                return True
            if row['attempts'] >= max_attempts:
                self._finish(job, pid, 'failed', 'سقف تلاش تمام شد؛ گالری هنوز با حالت قبل برابر است.')
                return True
            if self.stop.is_set():
                return False
            # COMMIT the unknown-write marker and both snapshots BEFORE HTTP.
            self.journal.mark_writing(job['id'], pid)
            try:
                response = self.api.put_images(pid, row['after'])
            except ApiError as error:
                self.journal.update_item(job['id'], pid, error=str(error))
                if error.code == 'auth':
                    return False
                if error.code == 'invalid':
                    self._finish(job, pid, 'failed', str(error))
                    return True
                self.journal.update_item(job['id'], pid,
                    retry_at=self.clock() + self._delay(job, row['attempts'] + 1, error.retry_after))
                continue  # Next loop READS; never a blind replay of the PUT.
            try:
                matches = response.get('id') == pid and gallery_ids(response) == row['after']
            except (ValueError, AttributeError):
                matches = False
            if matches:
                self._finish(job, pid, 'verified')
                return True
            self.journal.update_item(job['id'], pid, error='پاسخ نوشتن ناقص یا متفاوت بود؛ بازخوانی لازم است.', retry_at=0)
        return False
