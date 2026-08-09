"""
api.py — Calls ChatGPT's own backend API from inside the automated Chrome tab.

Every call below runs as JavaScript in the page (via
driver.execute_async_script), so it uses the browser's real, already
authenticated fetch() — the same requests chatgpt.com's own frontend makes.
We never read or decrypt anything from disk; we just ask the page to fetch
on our behalf and hand the JSON back to Python.

The bearer token handed out by /api/auth/session is short-lived. A full
export of a large account (hundreds+ of conversations) can easily outlive
it, so ChatGPTSession transparently refreshes and retries once whenever a
response comes back auth-error-shaped, instead of letting a stale token
silently masquerade as "this conversation has no messages".
"""

import json
import logging
import time
from typing import Callable, Iterator, Optional

from selenium.webdriver.chrome.webdriver import WebDriver

logger = logging.getLogger(__name__)

CHATGPT_URL = "https://chatgpt.com/"
REQUEST_DELAY_SECS = 0.4  # be polite to the API when bulk-exporting


class AuthError(RuntimeError):
    """Raised when there's no logged-in ChatGPT session in the automation profile."""


class ApiError(RuntimeError):
    """Raised when the ChatGPT API returns something unexpected that a token
    refresh + retry couldn't fix."""


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


def _fetch_json(driver: WebDriver, token: str, path: str) -> dict:
    js_headers = json.dumps({"Authorization": f"Bearer {token}"})
    js_path = json.dumps(path)
    expr = (
        f"fetch({js_path}, {{credentials: 'include', headers: {js_headers}}})"
        f".then(r => r.json())"
    )
    return _run_async_fetch(driver, expr)


def _looks_like_error(data: dict, required_key: str) -> bool:
    return not (isinstance(data, dict) and required_key in data)


class ChatGPTSession:
    """A driver + bearer token, refreshed automatically when it expires mid-export."""

    def __init__(self, driver: WebDriver, token: Optional[str] = None):
        self.driver = driver
        self.token = token

    def ensure_token(self) -> str:
        if not self.token:
            self.token = get_access_token(self.driver)
        return self.token

    def _get(self, path: str, required_key: str, retry: bool = True) -> dict:
        token = self.ensure_token()
        data = _fetch_json(self.driver, token, path)
        if _looks_like_error(data, required_key):
            detail = data.get("detail") if isinstance(data, dict) else data
            if retry:
                logger.warning(
                    "Session token appears to have expired mid-export "
                    "(response for %s was: %r) — refreshing and retrying once.",
                    path, detail,
                )
                self.token = None
                return self._get(path, required_key, retry=False)
            raise ApiError(f"{path} failed after a token refresh: {detail!r}")
        return data

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
            data = self._get(path, "items")
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
        return self._get(f"/backend-api/conversation/{conversation_id}", "mapping")


def throttle():
    time.sleep(REQUEST_DELAY_SECS)
