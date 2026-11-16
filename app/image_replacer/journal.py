"""SQLite durable snapshots, retry budgets and upload intent; no credentials."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import uuid


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS jobs(
                  id TEXT PRIMARY KEY, store TEXT NOT NULL, old_id INTEGER, new_id INTEGER,
                  created TEXT NOT NULL, state TEXT NOT NULL, settings TEXT NOT NULL,
                  kind TEXT NOT NULL DEFAULT 'replace', parent TEXT, failures INTEGER NOT NULL DEFAULT 0,
                  UNIQUE(parent,kind));
                CREATE TABLE IF NOT EXISTS items(
                  job TEXT NOT NULL REFERENCES jobs(id), pid INTEGER NOT NULL, name TEXT NOT NULL,
                  before_ids TEXT NOT NULL, after_ids TEXT NOT NULL, state TEXT NOT NULL,
                  attempts INTEGER NOT NULL DEFAULT 0, read_attempts INTEGER NOT NULL DEFAULT 0,
                  retry_at REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', checked_at TEXT,
                  PRIMARY KEY(job,pid));
                CREATE TABLE IF NOT EXISTS uploads(
                  store TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL, media_id INTEGER,
                  created TEXT NOT NULL, PRIMARY KEY(store,digest));
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def execution_lock(self):
        """OS lock survives threads/processes; process death releases it safely."""
        handle = open(self.path.with_suffix('.worker.lock'), 'a+b')
        handle.seek(0)
        try:
            if __import__('os').name == 'nt':
                import msvcrt
                if handle.read(1) == b'':
                    handle.write(b'0')
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            handle.close()
            raise RuntimeError('صف دیگری در حال اجراست؛ ابتدا آن را متوقف کنید.') from None
        try:
            yield
        finally:
            handle.close()

    def create_job(self, store, old_id, new_id, previews, selected, *, settings=None, scan=None):
        """Queue only approved preview rows; `scan` records the coverage they came from."""
        from .core import media_id
        settings = dict(settings or {})
        if scan:
            settings['scan'] = {key: scan[key] for key in ('scope', 'scanned_at', 'pages', 'scanned') if key in scan}
        media_id(old_id)
        media_id(new_id)
        if old_id == new_id:
            raise ValueError('عکس‌ها یکسان‌اند.')
        by_id = {row['id']: row for row in previews}
        if not selected or len(set(selected)) != len(selected) or any(pid not in by_id for pid in selected):
            raise ValueError('حداقل یک محصول معتبر و بدون تکرار انتخاب کنید.')
        rows = []
        for pid in selected:
            row = by_id[pid]
            if (row['blocked'] or old_id not in row['before'] or new_id in row['before']
                    or row['after'] != [new_id if value == old_id else value for value in row['before']]):
                raise ValueError('محصول متعارض نمی‌تواند وارد صف شود.')
            rows.append(row)
        return self._insert_job(store, old_id, new_id, rows, settings)

    def _insert_job(self, store, old_id, new_id, rows, settings, kind='replace', parent=None):
        job_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('INSERT INTO jobs(id,store,old_id,new_id,created,state,settings,kind,parent) VALUES(?,?,?,?,?,?,?,?,?)',
                (job_id, store, old_id, new_id, utc_now(), 'queued', json.dumps(settings), kind, parent))
            db.executemany('INSERT INTO items(job,pid,name,before_ids,after_ids,state) VALUES(?,?,?,?,?,?)',
                [(job_id, row['id'], str(row.get('name', '')), json.dumps(row['before']),
                    json.dumps(row['after']), 'queued') for row in rows])
        return job_id

    def create_rollback(self, job_id):
        original = self.job(job_id)
        if original['kind'] != 'replace':
            raise ValueError('برگشتِ برگشت در نسخهٔ فعلی پشتیبانی نمی‌شود.')
        rows = [{'id': row['pid'], 'name': row['name'], 'before': row['after'], 'after': row['before']}
            for row in self.items(job_id) if row['state'] == 'verified']
        if not rows:
            raise ValueError('مورد موفقی برای برگشت وجود ندارد.')
        try:
            return self._insert_job(original['store'], original['new_id'], original['old_id'], rows,
                original['settings'], 'rollback', job_id)
        except sqlite3.IntegrityError:
            raise ValueError('کار برگشت قبلاً ساخته شده؛ همان کار را ادامه دهید.') from None

    def job(self, job_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise ValueError('کار پیدا نشد.')
        result = dict(row)
        result['settings'] = json.loads(result['settings'])
        return result

    def jobs(self, store):
        with self.connect() as db:
            ids = [row[0] for row in db.execute('SELECT id FROM jobs WHERE store=? ORDER BY created DESC', (store,))]
        return [dict(self.job(job_id), summary=self.summary(job_id)) for job_id in ids]

    def items(self, job_id):
        with self.connect() as db:
            rows = [dict(row) for row in db.execute('SELECT * FROM items WHERE job=? ORDER BY rowid', (job_id,))]
        for row in rows:
            row['before'] = json.loads(row.pop('before_ids'))
            row['after'] = json.loads(row.pop('after_ids'))
        return rows

    def item(self, job_id, pid):
        return next(row for row in self.items(job_id) if row['pid'] == pid)

    def update_item(self, job_id, pid, **fields):
        allowed = {'state', 'attempts', 'read_attempts', 'retry_at', 'error', 'checked_at'}
        if not fields or set(fields) - allowed:
            raise ValueError('فیلد دفتر عملیات معتبر نیست.')
        with self.connect() as db:
            db.execute('UPDATE items SET ' + ','.join(f'{key}=?' for key in fields) + ' WHERE job=? AND pid=?',
                (*fields.values(), job_id, pid))

    def mark_writing(self, job_id, pid):
        with self.connect() as db:
            db.execute("UPDATE items SET state='writing',attempts=attempts+1,checked_at=? WHERE job=? AND pid=?",
                (utc_now(), job_id, pid))

    def set_job(self, job_id, state, failures=None):
        with self.connect() as db:
            if failures is None:
                db.execute('UPDATE jobs SET state=? WHERE id=?', (state, job_id))
            else:
                db.execute('UPDATE jobs SET state=?,failures=? WHERE id=?', (state, failures, job_id))

    def summary(self, job_id):
        rows = self.items(job_id)
        result = {state: sum(row['state'] == state for row in rows) for state in ('verified', 'review', 'failed')}
        result.update(selected=len(rows), pending=len(rows) - sum(result.values()))
        return result

    def report(self, job_id):
        return {'job': self.job(job_id), 'summary': self.summary(job_id), 'items': self.items(job_id)}

    def upload(self, store, digest):
        with self.connect() as db:
            row = db.execute('SELECT * FROM uploads WHERE store=? AND digest=?', (store, digest)).fetchone()
        return dict(row) if row else None

    def reserve_upload(self, store, digest):
        with self.connect() as db:
            db.execute('INSERT INTO uploads(store,digest,state,created) VALUES(?,?,?,?)', (store, digest, 'uncertain', utc_now()))

    def release_upload(self, store, digest):
        """Forget an unfinished reservation; a completed upload is never forgotten."""
        with self.connect() as db:
            db.execute("DELETE FROM uploads WHERE store=? AND digest=? AND state='uncertain'", (store, digest))

    def complete_upload(self, store, digest, media_id):
        with self.connect() as db:
            db.execute("UPDATE uploads SET state='complete',media_id=? WHERE store=? AND digest=?", (media_id, store, digest))
