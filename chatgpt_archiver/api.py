"""
api.py — Calls ChatGPT's own backend API from inside the automated Chrome tab.

Every call below runs as JavaScript in the page (via
driver.execute_async_script), so it uses the browser's real, already
authenticated fetch() — the same requests chatgpt.com's own frontend makes.
We never read or decrypt anything from disk; we just ask the page to fetch
on our behalf and hand the JSON back to Python.

Two failure modes matter a lot for a full-account export (hundreds+ of
conversations, each a separate request):

- The bearer token from /api/auth/session is short-lived and can expire
  mid-export. ChatGPTSession detects a 401/403 and transparently refreshes
  + retries once.
- The API itself rate-limits (429) under sustained request volume.
  ChatGPTSession backs off (honouring Retry-After when present) and
  retries several times rather than giving up on the first hit.

Both are read from the real HTTP status code, not guessed from the shape
of the response body — the earlier body-shape heuristic couldn't tell an
expired token apart from a rate limit apart from any other error.
"""

import json
import logging
import time
from typing import Callable, Iterator, Optional

from selenium.webdriver.chrome.webdriver import WebDriver

logger = logging.getLogger(__name__)

CHATGPT_URL = "https://chatgpt.com/"
REQUEST_DELAY_SECS = 0.6  # be polite to the API when bulk-exporting
RATE_LIMIT_MAX_RETRIES = 6
RATE_LIMIT_BASE_DELAY_SECS = 5.0
RATE_LIMIT_MAX_DELAY_SECS = 120.0


class AuthError(RuntimeError):
    """Raised when there's no logged-in ChatGPT session in the automation profile."""


class ApiError(RuntimeError):
    """Raised when the ChatGPT API returns something a refresh/backoff couldn't fix."""


def _run_async_fetch(driver: WebDriver, js_fetch_expression: str):
    """Run a JS expression evaluating to a Promise<JSON value> and return the result."""
    script = f"""
        const callback = arguments[arguments.length - 1];
        (async () => {{
            try {{
                const result = await ({js_fetch_expression});
                callback(JSON.stringify({{ ok: true, value: result }}));
            }} catch (e) {{
                callback(JSON.stringify({{ ok: false, error: String(e) }}));
            }}
        }})();
    """
    raw = driver.execute_async_script(script)
    envelope = json.loads(raw)
    if not envelope["ok"]:
        raise ApiError(envelope["error"])
    return envelope["value"]


def ensure_on_chatgpt(driver: WebDriver):
    if "chatgpt.com" not in (driver.current_url or ""):
        driver.get(CHATGPT_URL)


def get_access_token(driver: WebDriver) -> str:
    """Fetch a bearer token for the currently logged-in session, if any."""
    ensure_on_chatgpt(driver)
    data = _run_async_fetch(
        driver,
        "fetch('/api/auth/session', {credentials: 'include'}).then(r => r.json())",
    )
    token = data.get("accessToken") if isinstance(data, dict) else None
    if not token:
        raise AuthError("Not logged in yet.")
    return token


def _fetch_with_status(driver: WebDriver, token: str, path: str) -> dict:
    """Returns {status, ok, retryAfter, body} — the real HTTP outcome, not just the JSON."""
    js_headers = json.dumps({"Authorization": f"Bearer {token}"})
    js_path = json.dumps(path)
    expr = f"""
        fetch({js_path}, {{credentials: 'include', headers: {js_headers}}}).then(async r => ({{
            status: r.status,
            ok: r.ok,
            retryAfter: r.headers.get('retry-after'),
            body: await r.json().catch(() => null),
        }}))
    """
    return _run_async_fetch(driver, expr)


class ChatGPTSession:
    """A driver + bearer token, resilient to token expiry and rate limiting mid-export."""

    def __init__(
        self,
        driver: WebDriver,
        token: Optional[str] = None,
        on_wait: Optional[Callable[[str], None]] = None,
    ):
        self.driver = driver
        self.token = token
        # Called with a human-readable status line whenever a request has to
        # pause (token refresh, rate-limit backoff) — a packaged --windowed
        # exe has no console, so without this the GUI has no way to show
        # that anything is happening during a multi-second/minute wait.
        self.on_wait = on_wait

    def _notify(self, message: str):
        logger.warning(message)
        if self.on_wait:
            try:
                self.on_wait(message)
            except Exception:
                pass

    def ensure_token(self) -> str:
        if not self.token:
            self.token = get_access_token(self.driver)
        return self.token

    def _get(self, path: str, _auth_retried: bool = False, _rate_limit_attempt: int = 0) -> dict:
        token = self.ensure_token()
        resp = _fetch_with_status(self.driver, token, path)
        if resp.get("ok"):
            return resp.get("body") or {}

        status = resp.get("status")
        body = resp.get("body")
        detail = body.get("detail") if isinstance(body, dict) else body

        if status in (401, 403) and not _auth_retried:
            self._notify(f"Session expired (HTTP {status}) — refreshing and retrying…")
            self.token = None
            return self._get(path, _auth_retried=True, _rate_limit_attempt=_rate_limit_attempt)

        if status == 429:
            if _rate_limit_attempt >= RATE_LIMIT_MAX_RETRIES:
                raise ApiError(
                    f"{path} still rate-limited after {_rate_limit_attempt} retries: {detail!r}"
                )
            retry_after = resp.get("retryAfter")
            try:
                wait = float(retry_after) if retry_after else None
            except (TypeError, ValueError):
                wait = None
            if wait is None:
                wait = RATE_LIMIT_BASE_DELAY_SECS * (2 ** _rate_limit_attempt)
            wait = min(wait, RATE_LIMIT_MAX_DELAY_SECS)
            self._notify(
                f"Rate limited by ChatGPT — waiting {wait:.0f}s before retrying "
                f"(attempt {_rate_limit_attempt + 1}/{RATE_LIMIT_MAX_RETRIES})…"
            )
            time.sleep(wait)
            return self._get(
                path, _auth_retried=_auth_retried, _rate_limit_attempt=_rate_limit_attempt + 1
            )

        raise ApiError(f"{path} failed with HTTP {status}: {detail!r}")

    def iter_conversations(
        self, page_size: int = 28, on_page: Optional[Callable[[int, int], None]] = None
    ) -> Iterator[dict]:
        """Yield {id, title, update_time, create_time, ...} newest-updated first."""
        offset = 0
        total = None
        while True:
            path = (
                f"/backend-api/conversations?offset={offset}&limit={page_size}"
                f"&order=updated&is_archived=false"
            )
            data = self._get(path)
            items = data.get("items", [])
            total = data.get("total", total)
            if not items:
                return
            for item in items:
                yield item
            offset += len(items)
            if on_page:
                on_page(offset, total if total is not None else offset)
            if total is not None and offset >= total:
                return
            time.sleep(REQUEST_DELAY_SECS)

    def get_conversation(self, conversation_id: str) -> dict:
        """Fetch the full node mapping + active branch pointer for one conversation."""
        return self._get(f"/backend-api/conversation/{conversation_id}")


def throttle():
    time.sleep(REQUEST_DELAY_SECS)
