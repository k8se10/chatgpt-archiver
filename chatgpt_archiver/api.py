"""
api.py — Calls ChatGPT's own backend API from inside the automated Chrome tab.

Every call below runs as JavaScript in the page (via
driver.execute_async_script), so it uses the browser's real, already
authenticated fetch() — the same requests chatgpt.com's own frontend makes.
We never read or decrypt anything from disk; we just ask the page to fetch
on our behalf and hand the JSON back to Python.
"""

import json
import logging
import time
from typing import Iterator

from selenium.webdriver.chrome.webdriver import WebDriver

logger = logging.getLogger(__name__)

CHATGPT_URL = "https://chatgpt.com/"
REQUEST_DELAY_SECS = 0.4  # be polite to the API when bulk-exporting


class AuthError(RuntimeError):
    """Raised when there's no logged-in ChatGPT session in the automation profile."""


class ApiError(RuntimeError):
    """Raised when the ChatGPT API returns something unexpected."""


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


def _authed_fetch(driver: WebDriver, token: str, path: str) -> dict:
    js_headers = json.dumps({"Authorization": f"Bearer {token}"})
    js_path = json.dumps(path)
    expr = (
        f"fetch({js_path}, {{credentials: 'include', headers: {js_headers}}})"
        f".then(r => r.json())"
    )
    return _run_async_fetch(driver, expr)


def iter_conversations(driver: WebDriver, token: str, page_size: int = 28) -> Iterator[dict]:
    """Yield {id, title, update_time, create_time, ...} newest-updated first."""
    offset = 0
    while True:
        path = (
            f"/backend-api/conversations?offset={offset}&limit={page_size}"
            f"&order=updated&is_archived=false"
        )
        data = _authed_fetch(driver, token, path)
        items = data.get("items", [])
        if not items:
            return
        for item in items:
            yield item
        offset += len(items)
        if offset >= data.get("total", offset):
            return
        time.sleep(REQUEST_DELAY_SECS)


def get_conversation(driver: WebDriver, token: str, conversation_id: str) -> dict:
    """Fetch the full node mapping + active branch pointer for one conversation."""
    return _authed_fetch(driver, token, f"/backend-api/conversation/{conversation_id}")


def throttle():
    time.sleep(REQUEST_DELAY_SECS)
