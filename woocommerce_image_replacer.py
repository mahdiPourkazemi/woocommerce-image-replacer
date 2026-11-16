"""WooCommerce shared-image replacer: desktop entry point.

Run:    python woocommerce_image_replacer.py            (reads .env beside this file)
        python woocommerce_image_replacer.py --demo     (offline practice, no keys)
        python woocommerce_image_replacer.py --env PATH (another settings file)

Opening the window never changes the store: a queue starts only after the user
confirms it in the page. Credentials stay in the external .env; they are never
logged, shown, or bundled into the executable.
"""
import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
import tempfile
import time

from app.image_replacer.bridge import Bridge
from app.image_replacer.config import data_directory, entry_directory

APP_TITLE = 'جایگزینی عکس مشترک محصولات ووکامرس'
UI_FILE = Path('app') / 'image_replacer' / 'ui' / 'index.html'


def resource_path(relative):
    """Bundled read-only files live in PyInstaller's unpack folder when frozen."""
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    return base / relative


def configure_logging(demo):
    """Log to the durable data folder; messages carry event names, never secrets."""
    folder = data_directory(demo) / 'logs'
    folder.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(folder / 'app.log', maxBytes=512_000, backupCount=3, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    # urllib3 debug lines would include request URLs.
    logging.getLogger('urllib3').setLevel(logging.WARNING)


def show_error(message):
    if sys.platform == 'win32':
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, APP_TITLE, 0x10)
    else:
        print(message, file=sys.stderr)


def self_check(report_path):
    """Prove a build is complete without opening a window or touching a network.

    Runs the whole offline demo flow inside a temporary folder and writes the
    outcome as JSON, because a windowed executable has no console to print to.
    """
    result = {'ok': False, 'frozen': bool(getattr(sys, 'frozen', False))}
    try:
        html = resource_path(UI_FILE).read_text(encoding='utf-8')
        result['ui_bytes'] = len(html)
        import webview  # noqa: F401 - the window toolkit must be importable in the build.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            bridge = Bridge(data_dir=Path(folder))
            steps = [bridge.start_demo(), bridge.search_products('DEMO-001'), bridge.select_old(1, 200), bridge.start_scan('any')]
            deadline = time.monotonic() + 60
            while bridge.state()['data']['scan_running'] and time.monotonic() < deadline:
                time.sleep(.05)
            steps += [bridge.select_new(900), bridge.preview(), bridge.start_job([1], True)]
            while bridge.state()['data']['job_running'] and time.monotonic() < deadline:
                time.sleep(.05)
            jobs = bridge.jobs()
            steps.append(jobs)
            bridge._shutdown(timeout=10)
            result['steps_ok'] = all(step['ok'] for step in steps)
            result['job_state'] = jobs['data'][0]['state'] if jobs['ok'] and jobs['data'] else ''
        result['ok'] = result['steps_ok'] and result['job_state'] == 'completed' and len(html) > 1000
    except Exception as error:  # noqa: BLE001 - the report must say what broke.
        result['error'] = type(error).__name__ + ': ' + str(error)
    Path(report_path).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    return 0 if result['ok'] else 1


def confirm_close(window, bridge):
    """Closing mid-queue stops new requests and waits for the in-flight one."""
    if bridge._busy():
        if not window.create_confirmation_dialog('پایان برنامه',
                'صف جایگزینی در حال اجراست. با خروج، درخواست بعدی فرستاده نمی‌شود و کار ناتمام برای «ادامه» ذخیره می‌ماند. '
                'تغییرهای انجام‌شده برنمی‌گردند. خارج می‌شوید؟'):
            return False
    if not bridge._shutdown():
        window.create_confirmation_dialog('در انتظار پاسخ سایت',
            'درخواست جاری هنوز از سایت پاسخ نگرفته است. پنجره باز می‌ماند؛ کمی بعد دوباره خارج شوید.')
        return False
    return True


def window_probe(window, report_path):
    """Build check for the real window: did the page reach Python and back?"""
    result = {'ok': False}
    try:
        time.sleep(5)
        result['badge'] = window.evaluate_js("document.getElementById('store-badge').textContent")
        result['sections'] = window.evaluate_js("document.querySelectorAll('main section').length")
        result['demo_connected'] = window.evaluate_js('state.connected === true && state.demo === true')
        result['ok'] = bool(result['demo_connected']) and result['sections'] == 6
    except Exception as error:  # noqa: BLE001
        result['error'] = type(error).__name__
    finally:
        Path(report_path).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        window.destroy()


def run_window(args):
    import webview
    html = resource_path(UI_FILE).read_text(encoding='utf-8')
    bridge = Bridge(env_path=args.env)
    # The page is passed as text (not a file URL) so a non-ASCII install path cannot break loading.
    window = webview.create_window(APP_TITLE, html=html, js_api=bridge,
        width=1180, height=860, min_size=(520, 640), text_select=True)
    bridge._attach(window)
    if args.demo or args.window_check:
        bridge.start_demo()
    window.events.closing += lambda: confirm_close(window, bridge)
    probe = (window_probe, (window, args.window_check)) if args.window_check else (None, None)
    webview.start(*probe, gui='edgechromium', debug=False, private_mode=True)
    bridge._shutdown(timeout=5)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='Replace one shared product image across WooCommerce products.')
    parser.add_argument('--demo', action='store_true', help='open the offline practice store')
    parser.add_argument('--env', help='path of the settings file (default: .env beside the program)')
    parser.add_argument('--self-check', metavar='REPORT.json', help='verify the build offline and write a JSON report')
    parser.add_argument('--window-check', metavar='REPORT.json',
        help='open the real window on the demo store, report whether the page works, then close')
    args = parser.parse_args(argv)
    if args.self_check:
        return self_check(args.self_check)
    try:
        configure_logging(args.demo)
        logging.getLogger(__name__).info('starting; settings folder: %s', entry_directory())
        return run_window(args)
    except Exception as error:  # noqa: BLE001
        logging.getLogger(__name__).error('startup failed: %s', type(error).__name__)
        show_error('راه‌اندازی برنامه ممکن نشد. نصب Microsoft Edge WebView2 Runtime و دسترسی پوشهٔ داده را بررسی کنید.\n'
            'نوع خطا: ' + type(error).__name__)
        return 1
    finally:
        logging.shutdown()


if __name__ == '__main__':
    raise SystemExit(main())
