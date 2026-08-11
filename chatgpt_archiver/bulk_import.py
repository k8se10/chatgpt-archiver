"""
bulk_import.py — Read conversations from ChatGPT's own data export instead
of the live API.

chatgpt.com's Settings -> Data controls -> Export data sends you an email
with a downloadable .zip containing conversations.json: an array of the
same tree-shaped conversation objects the live /backend-api/conversation/{id}
endpoint returns (title, create_time, update_time, mapping, current_node) --
confirmed to be the same schema by cross-referencing multiple independent
write-ups of the export format, since we can't request-and-wait for a real
export inside this session to inspect one directly.

Reading it means zero live requests and zero rate limiting for a full
archive -- the right tool for a large account, at the cost of the export
not being instantaneous (OpenAI generates it in the background and emails
a download link, which can take anywhere from minutes to hours).
"""

import json
import logging
import zipfile
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

CONVERSATIONS_FILENAME = "conversations.json"


class ExportFileError(RuntimeError):
    """Raised when the selected file doesn't look like a ChatGPT data export."""


def _load_raw(path: Path) -> list:
    path = Path(path)
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if n.endswith(CONVERSATIONS_FILENAME)]
            if not names:
                raise ExportFileError(
                    f"{path.name} doesn't contain {CONVERSATIONS_FILENAME} — "
                    "is this a ChatGPT data export zip (Settings > Data controls > Export data)?"
                )
            with zf.open(names[0]) as f:
                data = json.load(f)
    else:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ExportFileError(f"{path.name} isn't valid JSON: {e}") from e

    if not isinstance(data, list):
        raise ExportFileError(
            f"Expected a list of conversations in {path.name}, got {type(data).__name__} — "
            "is this really a conversations.json from a ChatGPT export?"
        )
    return data


def iter_conversations(path: Path) -> Iterator[dict]:
    """Yield each conversation object as-is (same shape as ChatGPTSession.get_conversation())
    from a ChatGPT data-export .zip, or the conversations.json extracted from one."""
    for item in _load_raw(path):
        if not (item.get("id") or item.get("conversation_id")):
            raise ExportFileError(f"Conversation is missing an id: keys were {list(item.keys())!r}")
        yield item
