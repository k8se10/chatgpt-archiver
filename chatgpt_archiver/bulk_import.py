"""
bulk_import.py — Read conversations from ChatGPT's own data export instead
of the live API.

chatgpt.com's Settings -> Data controls -> Export data sends you an email
with a downloadable .zip. For any account with a non-trivial number of
conversations, OpenAI shards the data across multiple files rather than
one conversations.json -- confirmed against a real ~200MB export:
conversations-000.json, conversations-001.json, ... conversations-010.json
alongside ads.json, chat.html, codex.json, per-attachment file_*.dat, etc.
Each shard is a JSON array of the same tree-shaped conversation objects
the live /backend-api/conversation/{id} endpoint returns (id, title,
create_time, update_time, mapping, current_node) -- also confirmed
against that same real export.

Reading it means zero live requests and zero rate limiting for a full
archive -- the right tool for a large account, at the cost of the export
not being instantaneous (OpenAI generates it in the background and emails
a download link, which can take anywhere from minutes to hours).
"""

import json
import logging
import re
import zipfile
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

# Matches both the sharded form ("conversations-000.json") and the
# unsharded form ("conversations.json"), wherever it lands in the zip.
_CONVERSATIONS_SHARD_RE = re.compile(r"^conversations.*\.json$", re.IGNORECASE)


class ExportFileError(RuntimeError):
    """Raised when the selected file doesn't look like a ChatGPT data export."""


def _load_shard(raw: bytes, label: str) -> list:
    try:
        shard = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ExportFileError(f"{label} isn't valid JSON: {e}") from e
    if not isinstance(shard, list):
        raise ExportFileError(
            f"Expected a list of conversations in {label}, got {type(shard).__name__} — "
            "is this really a ChatGPT export?"
        )
    return shard


def _load_raw(path: Path) -> list:
    path = Path(path)
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            names = sorted(
                n for n in zf.namelist() if _CONVERSATIONS_SHARD_RE.match(Path(n).name)
            )
            if not names:
                raise ExportFileError(
                    f"{path.name} doesn't contain any conversations*.json files — "
                    "is this a ChatGPT data export zip (Settings > Data controls > Export data)?"
                )
            data = []
            for name in names:
                with zf.open(name) as f:
                    data.extend(_load_shard(f.read(), name))
            return data
    else:
        return _load_shard(path.read_bytes(), path.name)


def iter_conversations(path: Path) -> Iterator[dict]:
    """Yield each conversation object as-is (same shape as ChatGPTSession.get_conversation())
    from a ChatGPT data-export .zip (sharded or not), or a single conversations*.json
    extracted from one."""
    for item in _load_raw(path):
        if not (item.get("id") or item.get("conversation_id")):
            raise ExportFileError(f"Conversation is missing an id: keys were {list(item.keys())!r}")
        yield item
