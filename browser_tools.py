'''Headless browser tools for the agent (Playwright-driven).

justai dispatches tools synchronously, so we use playwright.sync_api. sync_api
cannot run inside an asyncio event loop, so a BrowserSession owns a dedicated
worker thread that holds the Playwright context and serves callables submitted
via a queue. Each tool blocks on the worker until the operation returns.
'''
import os
import queue
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from justlog import lg
from playwright.sync_api import sync_playwright, Page


SNAPSHOT_TRUNCATE = 6000
SCREENSHOT_DIR = Path(tempfile.gettempdir()) / 'mailprocessor-shots'


def _save_screenshot_sync(page: Page, label: str) -> str | None:
    '''Save a screenshot for debugging. Returns the path or None.'''
    try:
        SCREENSHOT_DIR.mkdir(exist_ok=True)
        path = SCREENSHOT_DIR / f'{label}-{os.getpid()}-{int(time.time() * 1000)}.png'
        page.screenshot(path=str(path), full_page=True, timeout=5000)
        return str(path)
    except Exception:
        return None


class BrowserSession:
    '''Single-page Playwright session running on its own thread.'''

    _SENTINEL = object()

    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._started = False
        self._start_lock = threading.Lock()

    def _worker(self) -> None:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--no-sandbox',
                ],
            )
            ctx = browser.new_context(
                accept_downloads=True,
                user_agent=(
                    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/130.0.0.0 Safari/537.36'
                ),
                viewport={'width': 1366, 'height': 900},
                locale='nl-NL',
                timezone_id='Europe/Amsterdam',
            )
            # Hide the navigator.webdriver flag that headless Chromium sets.
            ctx.add_init_script(
                'Object.defineProperty(navigator, "webdriver", {get: () => undefined});'
            )
            page = ctx.new_page()
            try:
                while True:
                    item = self._queue.get()
                    if item is self._SENTINEL:
                        return
                    fn, args, kwargs, fut = item
                    try:
                        fut['result'] = fn(page, *args, **kwargs)
                    except Exception as e:
                        fut['exc'] = e
                    finally:
                        fut['done'].set()
            finally:
                try:
                    browser.close()
                except Exception:
                    pass

    def _start(self) -> None:
        with self._start_lock:
            if self._started:
                return
            self._thread = threading.Thread(
                target=self._worker, name='browser-session', daemon=True,
            )
            self._thread.start()
            self._started = True

    def submit(self, fn: Callable[..., Any], *args, **kwargs) -> Any:
        '''Run `fn(page, *args, **kwargs)` on the worker. Blocks for result.'''
        if not self._started:
            self._start()
        fut: dict = {'done': threading.Event()}
        self._queue.put((fn, args, kwargs, fut))
        fut['done'].wait()
        if 'exc' in fut:
            raise fut['exc']
        return fut['result']

    def close(self) -> None:
        if not self._started:
            return
        self._queue.put(self._SENTINEL)
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._started = False
        self._thread = None


# ----- page helpers (run on the worker thread, take `page` as first arg) -----

def _settle(page: Page) -> None:
    try:
        page.wait_for_load_state('domcontentloaded', timeout=10000)
    except Exception:
        pass
    try:
        page.wait_for_load_state('networkidle', timeout=5000)
    except Exception:
        pass


def _snapshot_text(page: Page) -> str:
    try:
        snap = page.locator('body').aria_snapshot()
    except Exception as exc:
        snap = f'(snapshot failed: {type(exc).__name__}: {exc})'
    if len(snap) > SNAPSHOT_TRUNCATE:
        snap = snap[:SNAPSHOT_TRUNCATE] + '\n... (truncated)'
    return f'URL: {page.url}\n\n{snap}'


def _resolve_clickable(page: Page, target: str):
    # Escape quotes in target for has-text expression
    escaped = target.replace('"', '\\"')
    candidates = [
        # Form submit buttons first (avoid nav buttons that look identical)
        page.locator(f'form button[type="submit"]:has-text("{escaped}")').first,
        page.locator(f'form input[type="submit"][value*="{escaped}" i]').first,
        page.locator(f'form button:has-text("{escaped}")').first,
        # Then accessibility-tree role matches
        page.get_by_role('button', name=target),
        page.get_by_role('link', name=target),
        page.get_by_role('button', name=target, exact=False),
        page.get_by_role('link', name=target, exact=False),
        page.get_by_text(target, exact=False).first,
    ]
    for loc in candidates:
        try:
            if loc.count() > 0:
                return loc.first if hasattr(loc, 'first') else loc
        except Exception:
            continue
    return None


def _resolve_input(page: Page, target: str):
    candidates = [
        page.get_by_label(target),
        page.get_by_placeholder(target),
        page.get_by_role('textbox', name=target),
        page.locator(f'input[name="{target}"]'),
        page.locator(f'input[id="{target}"]'),
        page.locator(f'input[aria-label="{target}"]'),
    ]
    for loc in candidates:
        try:
            if loc.count() > 0:
                return loc.first if hasattr(loc, 'first') else loc
        except Exception:
            continue
    return None


# ----- tool factories (called on the agent thread) ----------------------------


def make_browser_open(session: BrowserSession) -> Callable:
    def browser_open(url: str) -> str:
        '''Open `url` in the headless browser. Returns an accessibility snapshot
        of the loaded page (truncated). Use this once per flow, then continue
        with browser_click / browser_fill / browser_snapshot.'''
        def _do(page: Page) -> str:
            page.goto(url, timeout=30000, wait_until='domcontentloaded')
            _settle(page)
            lg.info('browser open', url=url, title=page.title())
            return _snapshot_text(page)
        try:
            return session.submit(_do)
        except Exception as exc:
            shot = session.submit(_save_screenshot_sync, 'open-fail') if session._started else None
            lg.error('browser_open failed', url=url, error=str(exc), screenshot=shot)
            return f'error: {type(exc).__name__}: {exc}'
    return browser_open


def make_browser_snapshot(session: BrowserSession) -> Callable:
    def browser_snapshot() -> str:
        '''Return the current page's accessibility tree as text. Use this when
        you need to see what's on the page after a click or navigation.'''
        def _do(page: Page) -> str:
            _settle(page)
            return _snapshot_text(page)
        try:
            return session.submit(_do)
        except Exception as exc:
            return f'error: {type(exc).__name__}: {exc}'
    return browser_snapshot


def make_browser_click(session: BrowserSession) -> Callable:
    def browser_click(target: str) -> str:
        '''Click a button or link whose visible label/role-name matches `target`.
        Use the exact visible text where possible (e.g. "Inloggen", "Accepteren").
        Returns a fresh accessibility snapshot of the page after the click.'''
        def _do(page: Page) -> dict:
            loc = _resolve_clickable(page, target)
            if loc is None:
                shot = _save_screenshot_sync(page, f'click-nomatch-{target[:30]}')
                lg.warning('browser_click no match', target=target, screenshot=shot)
                return {'error': f'no clickable element matched "{target}"'}
            loc.click(timeout=10000)
            _settle(page)
            lg.info('browser click', target=target, url=page.url)
            return {'snapshot': _snapshot_text(page)}
        try:
            r = session.submit(_do)
            return r.get('snapshot') or f'error: {r.get("error")}'
        except Exception as exc:
            shot = session.submit(_save_screenshot_sync, f'click-fail-{target[:30]}')
            lg.error('browser_click failed', target=target, error=str(exc), screenshot=shot)
            return f'error: {type(exc).__name__}: {exc}'
    return browser_click


def make_browser_fill(session: BrowserSession) -> Callable:
    def browser_fill(target: str, value: str) -> str:
        '''Fill an input identified by `target` (label, placeholder, or name) with
        `value`. Use this for non-secret values like a 6-digit verification code.
        For passwords, use browser_fill_credential.'''
        def _do(page: Page) -> str:
            loc = _resolve_input(page, target)
            if loc is None:
                lg.warning('browser_fill no match', target=target)
                return f'error: no input matched "{target}"'
            loc.fill(value, timeout=10000)
            lg.info('browser fill', target=target, chars=len(value))
            return f'filled: {target}'
        try:
            return session.submit(_do)
        except Exception as exc:
            lg.error('browser_fill failed', target=target, error=str(exc))
            return f'error: {type(exc).__name__}: {exc}'
    return browser_fill


def make_browser_fill_credential(session: BrowserSession) -> Callable:
    def browser_fill_credential(target: str, env_var_name: str) -> str:
        '''Fill an input identified by `target` with the value of environment
        variable `env_var_name`. The secret value never travels through the LLM:
        only the env var NAME is passed in.'''
        value = os.getenv(env_var_name)
        if not value:
            return f'error: env var {env_var_name} not set'
        def _do(page: Page) -> str:
            loc = _resolve_input(page, target)
            if loc is None:
                lg.warning('browser_fill_credential no match',
                           target=target, env_var=env_var_name)
                return f'error: no input matched "{target}"'
            loc.fill(value, timeout=10000)
            lg.info('browser fill credential', target=target, env_var=env_var_name)
            return f'filled: {target} (from {env_var_name})'
        try:
            return session.submit(_do)
        except Exception as exc:
            lg.error('browser_fill_credential failed', target=target, error=str(exc))
            return f'error: {type(exc).__name__}: {exc}'
    return browser_fill_credential


def make_browser_fill_otp(session: BrowserSession) -> Callable:
    def browser_fill_otp(code: str) -> str:
        '''Fill an OTP / verification-code field that splits the digits across
        N separate `<input>` boxes (each maxlength=1). Pass the full code as a
        single string. Returns the count of inputs filled.'''
        def _do(page: Page) -> str:
            digits = ''.join(ch for ch in code if ch.isdigit())
            if not digits:
                return 'error: no digits in code'
            selectors = [
                'input[maxlength="1"][inputmode="numeric"]',
                'input[maxlength="1"]',
                'input[type="tel"][maxlength="1"]',
                'input[autocomplete="one-time-code"]',
            ]
            for sel in selectors:
                loc = page.locator(sel)
                count = loc.count()
                if count >= len(digits):
                    for i, d in enumerate(digits):
                        loc.nth(i).fill(d, timeout=5000)
                    lg.info('browser fill otp', digits=len(digits),
                            inputs=count, selector=sel)
                    return f'filled {len(digits)} digits across {count} inputs'
                if count > 0:
                    return (
                        f'error: only {count} OTP inputs found for selector "{sel}" '
                        f'but code has {len(digits)} digits'
                    )
            return 'error: no OTP inputs found on page'
        try:
            return session.submit(_do)
        except Exception as exc:
            lg.error('browser_fill_otp failed', error=str(exc))
            return f'error: {type(exc).__name__}: {exc}'
    return browser_fill_otp


def make_browser_download(session: BrowserSession) -> Callable:
    def browser_download(target: str) -> str:
        '''Click `target` and wait for the resulting download. Saves to a temp
        path and returns its absolute path (string). Pass this path to
        apply_downloaded as `source_path`. If clicking triggers a navigation
        to a download URL instead of a download event, use browser_download_url
        with the href instead.'''
        def _do(page: Page) -> str:
            loc = _resolve_clickable(page, target)
            if loc is None:
                return f'error: no clickable element matched "{target}"'
            with page.expect_download(timeout=60000) as dl_info:
                loc.click(timeout=10000)
            download = dl_info.value
            tmp_dir = Path(tempfile.mkdtemp(prefix='mailprocessor-dl-'))
            tmp_path = tmp_dir / (download.suggested_filename or 'download.bin')
            download.save_as(str(tmp_path))
            lg.info('browser download', target=target, path=str(tmp_path),
                    filename=download.suggested_filename)
            return str(tmp_path)
        try:
            return session.submit(_do)
        except Exception as exc:
            lg.error('browser_download failed', target=target, error=str(exc))
            return f'error: {type(exc).__name__}: {exc}'
    return browser_download


def make_browser_download_url(session: BrowserSession) -> Callable:
    def browser_download_url(url: str) -> str:
        '''Fetch `url` via the browser's HTTP context (cookies/session inherited
        from the current page) and save the response body to a temp path.
        Use this when a download URL is known directly — e.g. an href from
        a button — and clicking it triggers a navigation rather than a download
        event. Returns the absolute temp path.'''
        def _do(page: Page) -> str:
            resp = page.context.request.get(url, timeout=60000)
            if not resp.ok:
                return f'error: HTTP {resp.status}'
            body = resp.body()
            cd = resp.headers.get('content-disposition', '')
            m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd, re.I)
            filename = m.group(1) if m else 'download.bin'
            tmp_dir = Path(tempfile.mkdtemp(prefix='mailprocessor-dl-'))
            tmp_path = tmp_dir / filename
            tmp_path.write_bytes(body)
            lg.info('browser download url', url=url, path=str(tmp_path),
                    bytes=len(body))
            return str(tmp_path)
        try:
            return session.submit(_do)
        except Exception as exc:
            lg.error('browser_download_url failed', url=url, error=str(exc))
            return f'error: {type(exc).__name__}: {exc}'
    return browser_download_url
