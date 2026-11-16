"""Developer-only page preview: the real UI and bridge, demo store, in a browser.

The shipped program has no HTTP server; the page talks to Python through the
native window. This script exists so the page can be inspected in an ordinary
browser. It binds to 127.0.0.1 only, always uses the offline demo store, keeps
its data in a temporary folder, and is never bundled into the executable.

Run:  python tools/image_replacer_preview.py [port]
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from app.image_replacer.bridge import Bridge  # noqa: E402

PAGE = PROJECT / 'app' / 'image_replacer' / 'ui' / 'index.html'
ALLOWED = {'state', 'start_demo', 'search_products', 'select_old', 'start_scan', 'stop_scan', 'search_media',
    'select_new', 'upload_new', 'preview', 'start_job', 'resume_job', 'start_rollback', 'stop_job', 'jobs',
    'job_detail', 'export_report', 'open_link', 'connect', 'choose_env_file'}
# Stands in for the native window's script bridge.
SHIM = '''<script>
window.pywebview = { api: new Proxy({}, { get: (_, name) => async (...args) =>
  (await fetch('/api/' + name, { method: 'POST', body: JSON.stringify(args) })).json() }) };
</script>'''


def build_handler(bridge):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, body, kind):
            self.send_response(200)
            self.send_header('Content-Type', kind + '; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.split('?')[0] != '/':
                return self.send_error(404)
            html = PAGE.read_text(encoding='utf-8').replace('<script>', SHIM + '<script>', 1)
            self._send(html.encode('utf-8'), 'text/html')

        def do_POST(self):
            name = self.path.rsplit('/', 1)[-1]
            if not self.path.startswith('/api/') or name not in ALLOWED:
                return self.send_error(404)
            args = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'[]')
            if name in ('connect', 'choose_env_file'):
                answer = {'ok': False, 'error': 'پیش‌نمایش توسعه فقط حالت نمایشی دارد؛ اتصال واقعی در پنجرهٔ برنامه انجام می‌شود.'}
            else:
                answer = getattr(bridge, name)(*args)
            self._send(json.dumps(answer, ensure_ascii=False).encode('utf-8'), 'application/json')

        def log_message(self, *args):
            pass
    return Handler


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    folder = Path(tempfile.mkdtemp(prefix='image-replacer-preview-'))
    sample = folder / 'sample.webp'
    sample.write_bytes(b'RIFFpreviewWEBP')
    bridge = Bridge(data_dir=folder, pick_file=lambda types: str(sample))
    server = ThreadingHTTPServer(('127.0.0.1', port), build_handler(bridge))
    print(f'preview: http://127.0.0.1:{port}  (demo store only)')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        bridge._shutdown(timeout=5)


if __name__ == '__main__':
    main()
