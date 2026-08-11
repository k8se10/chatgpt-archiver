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
from typing import Optional

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

logger = logging.getLogger(__name__)


class ChromeClosedError(RuntimeError):
    """Raised when an operation needs the automated Chrome window and it's gone."""


def profile_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ChatGPTArchiver" / "chrome-profile"
    base.mkdir(parents=True, exist_ok=True)
    return base


def create_driver(headless: bool = False) -> webdriver.Chrome:
    """Launch Chrome under the dedicated automation profile."""
    options = Options()
    options.add_argument(f"--user-data-dir={profile_dir()}")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    if headless:
        options.add_argument("--headless=new")

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
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
