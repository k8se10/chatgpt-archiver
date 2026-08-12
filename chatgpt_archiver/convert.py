"""
convert.py — Turn a ChatGPT conversation API response into clean Markdown.

The API returns every branch ever created (edits, regenerations) as a tree
of nodes. We walk from `current_node` back to the root to get the single
path that's actually shown on screen, then keep only the visible user/
assistant turns — dropping system prompts, tool calls, and hidden
chain-of-thought messages (reasoning models expose these in the same tree
under non-"final" channels even though they never render on screen).
"""

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_VISIBLE_CHANNELS = (None, "final")
_VISIBLE_ROLES = ("user", "assistant")
_ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_METADATA_RE = re.compile(r"^<!--\s*chatgpt-archiver:\s*id=(\S+)\s+update_time=(\S+)\s*-->\s*$")


def _active_path(mapping: dict, current_node: Optional[str]) -> list:
    path = []
    node_id = current_node
    while node_id:
        node = mapping.get(node_id)
        if node is None:
            break
        path.append(node_id)
        node_id = node.get("parent")
    path.reverse()
    return path


def _part_to_text(part) -> str:
    if isinstance(part, str):
        return part
    if isinstance(part, dict):
        if part.get("content_type") == "image_asset_pointer":
            return "*[image attached]*"
        return "*[unsupported attachment]*"
    return ""


def extract_visible_messages(conversation: dict) -> list:
    """Return [{role, text, create_time}] for the path currently shown on screen."""
    mapping = conversation.get("mapping", {})
    path = _active_path(mapping, conversation.get("current_node"))

    messages = []
    for node_id in path:
        message = mapping[node_id].get("message")
        if not message:
            continue
        role = (message.get("author") or {}).get("role")
        if role not in _VISIBLE_ROLES:
            continue
        if message.get("recipient") not in ("all", None):
            continue
        if message.get("channel") not in _VISIBLE_CHANNELS:
            continue
        parts = (message.get("content") or {}).get("parts") or []
        text = "\n\n".join(_part_to_text(p) for p in parts).strip()
        if not text:
            continue
        messages.append({
            "role": role,
            "text": text,
            "create_time": message.get("create_time"),
        })
    return messages


def coerce_timestamp(ts) -> Optional[float]:
    """Normalize a timestamp to a unix epoch float.

    ChatGPT's own API is inconsistent about this: per-message `create_time`
    inside a conversation's node mapping is a numeric epoch, but
    `create_time`/`update_time` on items from /backend-api/conversations are
    ISO 8601 strings (e.g. "2026-08-09T19:19:09.712746Z"). Accept either.
    """
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            try:
                return float(ts)
            except ValueError:
                return None
    return None


def _format_timestamp(ts) -> str:
    epoch = coerce_timestamp(ts)
    if epoch is None:
        return ""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _metadata_comment(conversation_id: str, update_time) -> str:
    """An invisible marker (renders as nothing in any Markdown viewer) recording
    what this export corresponds to, so a later run can tell whether the source
    conversation has changed since — see local_export_status()."""
    epoch = coerce_timestamp(update_time)
    epoch_str = f"{epoch:.6f}" if epoch is not None else "unknown"
    return f"<!-- chatgpt-archiver: id={conversation_id} update_time={epoch_str} -->"


def read_exported_metadata(path) -> Optional[dict]:
    """Read back {id, update_time} from a previously-exported .md file's
    marker line, if present. None for files with no marker (exported by an
    older version, or not one of ours) — callers should treat that as
    "can't tell, assume it's fine" rather than forcing a re-export."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            first_line = f.readline()
    except OSError:
        return None
    m = _METADATA_RE.match(first_line.strip())
    if not m:
        return None
    conv_id, update_time_str = m.groups()
    try:
        update_time = float(update_time_str)
    except ValueError:
        update_time = None
    return {"id": conv_id, "update_time": update_time}


def to_markdown(title: str, conversation_id: str, messages: list, update_time=None) -> str:
    lines = [_metadata_comment(conversation_id, update_time), f"# {title or 'Untitled conversation'}", ""]
    lines.append(f"*Exported from [chatgpt.com/c/{conversation_id}](https://chatgpt.com/c/{conversation_id})*")
    lines.append("")

    role_labels = {"user": "You", "assistant": "ChatGPT"}
    for msg in messages:
        label = role_labels.get(msg["role"], msg["role"])
        ts = _format_timestamp(msg.get("create_time"))
        lines.append(f"## {label}" + (f" — {ts}" if ts else ""))
        lines.append("")
        lines.append(msg["text"])
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _format_filename_timestamp(ts) -> str:
    """Local-time date/time for filenames — colon-free so it's filesystem-safe."""
    epoch = coerce_timestamp(ts)
    if epoch is None:
        return ""
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H-%M")


def safe_filename(title: str, conversation_id: str, create_time=None, max_len: int = 80) -> str:
    base = (title or "Untitled conversation").strip()
    base = _ILLEGAL_FILENAME_CHARS.sub(" ", base)
    base = re.sub(r"\s+", " ", base).strip().rstrip(".")
    if not base:
        base = "Untitled conversation"
    if len(base) > max_len:
        base = base[:max_len].rstrip()

    ts = _format_filename_timestamp(create_time)
    return f"{ts} - {base}" if ts else base


class ExportStatus:
    """Where a conversation's on-disk export stands relative to the live version."""
    MISSING = "missing"  # never exported (or no exact filename match)
    STALE = "stale"      # exported, but the conversation changed since
    CURRENT = "current"  # exported and (as far as we can tell) still up to date


def local_export_status(directory, base_name: str, live_update_time, suffix: str = ".md"):
    """Returns (status, stale_days). stale_days is a float (>= 0) only when
    status == STALE — how long ago the conversation changed relative to
    when it was last exported — and None otherwise. Used by both Smart Scan
    (does this look already archived?) and "Skip it" (should this stale copy
    actually get refreshed instead of left alone?).

    A file with no embedded metadata (exported by an older version of this
    tool, or not one of ours) or a live_update_time we can't parse is
    reported CURRENT — we'd rather under-flag than force surprise
    re-exports of an existing archive we can't actually verify against.
    """
    path = Path(directory) / f"{base_name}{suffix}"
    if not path.exists():
        return ExportStatus.MISSING, None

    meta = read_exported_metadata(path)
    if meta is None or meta.get("update_time") is None:
        return ExportStatus.CURRENT, None

    live_epoch = coerce_timestamp(live_update_time)
    if live_epoch is None or meta["update_time"] >= live_epoch:
        return ExportStatus.CURRENT, None

    return ExportStatus.STALE, (live_epoch - meta["update_time"]) / 86400


class OnConflict:
    """What to do when the target filename already exists."""
    RENAME = "rename"    # keep both: append " (2)", " (3)", ...
    REPLACE = "replace"  # overwrite the existing file
    SKIP = "skip"        # leave the existing file alone *if it's still current*


def resolve_output_path(
    directory, base_name: str, on_conflict: str = OnConflict.RENAME, suffix: str = ".md",
    live_update_time=None,
) -> Optional[Path]:
    """Return the Path to write to under `directory` for `base_name`, applying
    `on_conflict` if that name is already taken. Returns None only for SKIP
    when the existing file is still CURRENT — a STALE one (conversation
    changed since it was exported) is returned for overwriting even under
    SKIP, since "skip" is meant to mean "don't redo unnecessary work", not
    "never update anything"."""
    directory = Path(directory)
    candidate = directory / f"{base_name}{suffix}"
    if not candidate.exists():
        return candidate

    if on_conflict == OnConflict.REPLACE:
        return candidate
    if on_conflict == OnConflict.SKIP:
        status, _ = local_export_status(directory, base_name, live_update_time, suffix=suffix)
        return None if status == ExportStatus.CURRENT else candidate

    n = 2
    while True:
        candidate = directory / f"{base_name} ({n}){suffix}"
        if not candidate.exists():
            return candidate
        n += 1
