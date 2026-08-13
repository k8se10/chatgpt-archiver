"""
scan_cache.py — A tiny per-output-folder watermark so repeat Smart Scans
get cheaper over time instead of re-checking the same ~150 conversations
on every run.

Stored as a dotfile next to the exported .md files, so it travels with
the archive folder itself — point the app at a different/empty folder and
there's correctly no cache, falling back to Smart Scan's normal sampling.

The watermark is only ever advanced by callers that can prove full
coverage (see gui.py) — this module just persists whatever value it's
given and never invents one, so it carries no correctness risk itself.
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional

from . import convert

logger = logging.getLogger(__name__)

CACHE_FILENAME = ".chatgpt-archiver-cache.json"

# v2: a cached watermark can only be trusted by the fast Smart Scan path if
# it was written by a version of this app that already checks on-disk files
# for un-embedded image placeholders, not just update_time. A v1 (or
# unversioned) cache predates that check -- everything below its watermark
# was skipped without ever being asked "does this file still have an
# un-embedded image?", so trusting it blindly would permanently hide those
# files from every future Smart Scan. gui.py falls back to a full recheck
# whenever it sees a sub-v2 cache; once that recheck exports cleanly, the
# cache gets re-saved at CACHE_VERSION and the fast path resumes.
CACHE_VERSION = 2

# Scan modes allowed to advance the watermark -- only ones that can *prove*
# full coverage of the window they scanned. "smart_heuristic" (the first-visit
# ratio-based guess) is deliberately excluded: it could be wrong up to
# (1 - SMART_SCAN_MATCH_THRESHOLD) of the time, and a bad watermark would
# silently skip a conversation forever, not just slow down one scan.
PROVABLE_SCAN_MODES = ("full", "smart_cached", "file")


def can_advance(item_status: list, selected_indices, scan_mode: str) -> bool:
    """True if exporting `selected_indices` (from a scan that produced
    `item_status`, one (ExportStatus, stale_days) per scanned conversation)
    can safely advance the watermark: every conversation the scan found
    needing action must be part of what's being exported, and the scan mode
    itself must be one that covers a real, provable window (not a guess)."""
    if scan_mode not in PROVABLE_SCAN_MODES:
        return False
    needs_action = {
        i for i, (status, _) in enumerate(item_status) if status != convert.ExportStatus.CURRENT
    }
    return needs_action.issubset(set(selected_indices))


def load(directory) -> Optional[dict]:
    path = Path(directory) / CACHE_FILENAME
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Ignoring unreadable scan cache %s: %s", path, e)
        return None


def watermark(directory) -> Optional[float]:
    data = load(directory)
    if not data:
        return None
    value = data.get("newest_update_time_seen")
    return float(value) if isinstance(value, (int, float)) else None


def is_image_aware(directory) -> bool:
    """True if the cached watermark was written by a version of this app
    that already checks on-disk files for un-embedded image placeholders --
    i.e. it's safe to trust the fast Smart Scan path without missing files
    that need an image retry. False (including "no cache at all") means the
    caller should force a full recheck instead of the fast path."""
    data = load(directory)
    if not data:
        return False
    return data.get("version", 1) >= CACHE_VERSION


def save(directory, newest_update_time_seen: float):
    path = Path(directory) / CACHE_FILENAME
    data = {
        "version": CACHE_VERSION,
        "newest_update_time_seen": newest_update_time_seen,
        "last_full_sync_at": time.time(),
    }
    try:
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError as e:
        logger.warning("Couldn't write scan cache %s: %s", path, e)
