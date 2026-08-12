"""
browser.py — Owns the Selenium-controlled Chrome instance.

Uses a dedicated automation profile (separate from the user's everyday
Chrome profile) so this tool never touches your regular browser process or
its cookie store directly. You log into chatgpt.com once inside this
profile; the session persists across runs like any normal Chrome profile.
"""

import logging
import os
from pathlib import Path
from typing import Callable, Optional

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

logger = logging.getLogger(__name__)

# Standard per-machine and per-user install locations. Checked directly
# rather than relying on Selenium's own discovery so a missing Chrome
# produces a clear, actionable message instead of a raw WebDriverException.
_CHROME_CANDIDATE_PATHS = [
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
]


class ChromeClosedError(RuntimeError):
    """Raised when an operation needs the automated Chrome window and it's gone."""


class ChromeNotFoundError(RuntimeError):
    """Raised when Google Chrome isn't installed anywhere this tool knows to look."""


def find_chrome_binary() -> Optional[str]:
    for template in _CHROME_CANDIDATE_PATHS:
        candidate = os.path.expandvars(template)
        if "%" not in candidate and Path(candidate).is_file():
            return candidate
    return None


def profile_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ChatGPTArchiver" / "chrome-profile"
    base.mkdir(parents=True, exist_ok=True)
    return base


def create_driver(
    headless: bool = False, on_status: Optional[Callable[[str], None]] = None
) -> webdriver.Chrome:
    """Launch Chrome under the dedicated automation profile.

    `on_status`, if given, is called with human-readable progress lines —
    in particular around the chromedriver download, which needs internet
    access on first run and can otherwise look like the app has frozen.
    """
    def status(message: str):
        logger.info(message)
        if on_status:
            on_status(message)

    chrome_path = find_chrome_binary()
    if chrome_path is None:
        raise ChromeNotFoundError(
            "Google Chrome doesn't appear to be installed. This tool drives your "
            "own Chrome to read ChatGPT — install it from google.com/chrome, "
            "then try again."
        )

    options = Options()
    options.binary_location = chrome_path
    options.add_argument(f"--user-data-dir={profile_dir()}")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    if headless:
        options.add_argument("--headless=new")

    status("Setting up the Chrome driver (first run only — needs internet)…")
    try:
        driver_path = ChromeDriverManager().install()
    except Exception as e:
        raise RuntimeError(
            f"Couldn't download the matching Chrome driver (needs internet on first "
            f"run): {e}"
        ) from e

    status("Launching Chrome…")
    service = Service(driver_path)
    try:
        driver = webdriver.Chrome(service=service, options=options)
    except WebDriverException as e:
        raise RuntimeError(f"Chrome failed to start: {e}") from e
    driver.set_script_timeout(30)
    logger.info("Chrome launched (profile=%s)", profile_dir())
    return driver


def is_alive(driver: Optional[webdriver.Chrome]) -> bool:
    """Best-effort liveness probe. Selenium keeps a driver object usable-looking
    even after its browser window/process is gone (closed by the user, crashed,
    etc.) — any call into it just raises. Touching a cheap property is the
    standard way to find out before actually trying to do something with it."""
    if driver is None:
        return False
    try:
        _ = driver.title
        return True
    except Exception:
        return False
