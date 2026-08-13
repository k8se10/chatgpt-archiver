"""
gui.py — Minimal Tkinter front end for chatgpt_archiver.

Flow: launch a dedicated automated Chrome window -> log in there once if
needed -> Smart Scan (checks your most-recent conversations against what's
already on disk and infers the rest) or Full Scan (lists everything) ->
pick some -> export to Markdown. All network/browser calls run on a
background thread so the UI never freezes.
"""

import logging
import queue
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from . import api, browser, bulk_import, convert, scan_cache

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "ChatGPT Archive"

SMART_SCAN_SAMPLE_SIZE = 150
SMART_SCAN_MATCH_THRESHOLD = 0.8  # fraction of the sample that must already be CURRENT

STALE_YELLOW_MAX_DAYS = 3  # 0 < age <= this -> yellow; beyond -> red
COLOR_STALE_YELLOW = "#9a7d0a"
COLOR_STALE_RED = "#b3261e"
COLOR_NEW = "#2f5fa8"  # never exported at all

MAX_IMAGE_EMBED_BYTES = 8 * 1024 * 1024  # skip inline-embedding anything larger; placeholder instead


class ArchiverApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ChatGPT Archiver")
        self.geometry("680x620")
        self.minsize(560, 480)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._driver = None
        self._session: api.ChatGPTSession | None = None
        self._conversations = []  # summary dicts: id/title/create_time/update_time
        self._item_status = []  # parallel to _conversations: (ExportStatus, stale_days|None)
        self._source = None  # "live" (browser session) or "file" (bulk export)
        self._bulk_conversations = {}  # id -> full conversation dict, "file" source only
        self._scan_mode = None  # "full" / "smart_cached" / "smart_heuristic" / "file"
        self._scan_newest_update_time = None  # newest update_time seen in the current scan
        self._log_queue = queue.Queue()
        self._ui_queue = queue.Queue()

        self._build_widgets()
        self.after(100, self._drain_log_queue)
        self.after(50, self._drain_ui_queue)

    # ── UI construction ──────────────────────────────────────────────────

    def _build_widgets(self):
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")

        self.connect_btn = ttk.Button(top, text="Launch Chrome & Connect", command=self._on_connect)
        self.connect_btn.pack(side="left")

        self.login_btn = ttk.Button(top, text="I've logged in", command=self._on_confirm_login, state="disabled")
        self.login_btn.pack(side="left", padx=6)

        self.status_label = ttk.Label(top, text="Not connected")
        self.status_label.pack(side="left", padx=10)

        scan_frame = ttk.Frame(self, padding=(10, 0))
        scan_frame.pack(fill="x")
        self.smart_scan_btn = ttk.Button(
            scan_frame, text="Smart Scan (recommended)", command=self._on_smart_scan, state="disabled"
        )
        self.smart_scan_btn.pack(side="left")
        self.full_scan_btn = ttk.Button(
            scan_frame, text="Full Scan", command=self._on_full_scan, state="disabled"
        )
        self.full_scan_btn.pack(side="left", padx=6)
        self.import_btn = ttk.Button(scan_frame, text="Import Export File…", command=self._on_import_file)
        self.import_btn.pack(side="left", padx=6)

        hint_frame = ttk.Frame(self, padding=(10, 4))
        hint_frame.pack(fill="x")
        ttk.Label(
            hint_frame,
            text=(
                "Smart Scan checks your ~150 most recent conversations against this output folder and "
                "infers the rest of your history is archived too if most already match — much faster "
                "than listing everything. Full Scan lists your whole account. Large one-off backfill? "
                "ChatGPT's own bulk export avoids rate limits entirely: Settings → Data controls → Export "
                "data, then Import Export File… (request it ahead of time — can take a few days, not minutes)."
            ),
            foreground="#666666",
            wraplength=640,
            justify="left",
        ).pack(side="left", fill="x")

        list_frame = ttk.Frame(self, padding=(10, 0))
        list_frame.pack(fill="both", expand=True)

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical")
        self.listbox = tk.Listbox(list_frame, selectmode="extended", yscrollcommand=scrollbar.set)
        scrollbar.config(command=self.listbox.yview)
        self.listbox.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        select_frame = ttk.Frame(self, padding=(10, 5))
        select_frame.pack(fill="x")
        ttk.Button(select_frame, text="Select All", command=self._select_all).pack(side="left")
        ttk.Button(
            select_frame, text="Select None", command=lambda: self.listbox.selection_clear(0, "end")
        ).pack(side="left", padx=6)
        ttk.Button(select_frame, text="Select Outdated", command=self._select_outdated).pack(side="left")
        ttk.Label(
            select_frame,
            text="  Outdated = new or changed since last export (yellow = recent, red = longer ago)",
            foreground="#666666",
        ).pack(side="left")

        out_frame = ttk.Frame(self, padding=10)
        out_frame.pack(fill="x")
        ttk.Label(out_frame, text="Save to:").pack(side="left")
        self.output_var = tk.StringVar(value=str(DEFAULT_OUTPUT_DIR))
        ttk.Entry(out_frame, textvariable=self.output_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(out_frame, text="Browse…", command=self._browse_output).pack(side="left")

        conflict_frame = ttk.Frame(self, padding=(10, 0))
        conflict_frame.pack(fill="x")
        ttk.Label(conflict_frame, text="If a file already exists:").pack(side="left")
        self.conflict_var = tk.StringVar(value=convert.OnConflict.SKIP)
        ttk.Radiobutton(
            conflict_frame, text="Skip it (unless outdated)", variable=self.conflict_var,
            value=convert.OnConflict.SKIP,
        ).pack(side="left", padx=(6, 0))
        ttk.Radiobutton(
            conflict_frame, text="Keep both (rename)", variable=self.conflict_var,
            value=convert.OnConflict.RENAME,
        ).pack(side="left", padx=6)
        ttk.Radiobutton(
            conflict_frame, text="Replace it", variable=self.conflict_var,
            value=convert.OnConflict.REPLACE,
        ).pack(side="left")

        action_frame = ttk.Frame(self, padding=10)
        action_frame.pack(fill="x")
        self.export_btn = ttk.Button(
            action_frame, text="Export Selected", command=self._on_export, state="disabled"
        )
        self.export_btn.pack(side="left")

        progress_frame = ttk.Frame(self, padding=(10, 0, 10, 5))
        progress_frame.pack(fill="x")
        self.progress = ttk.Progressbar(progress_frame, mode="determinate")
        self.progress.pack(fill="x")
        self.progress_label = ttk.Label(progress_frame, text="")
        self.progress_label.pack(anchor="w")

        log_frame = ttk.Frame(self, padding=(10, 0, 10, 10))
        log_frame.pack(fill="both", expand=False)
        self.log_text = tk.Text(log_frame, height=8, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True)

    # ── Logging / progress helpers (thread-safe: workers push, UI thread drains) ──

    def _log(self, message: str):
        self._log_queue.put(message)

    def _drain_log_queue(self):
        try:
            while True:
                message = self._log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", message + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(150, self._drain_log_queue)

    def _ui(self, fn):
        """Thread-safe way for a background thread to schedule a UI update.
        self.after(0, ...) is NOT reliably safe when called from a non-main
        thread to register a new callback -- it can raise 'RuntimeError:
        main thread is not in main loop'. Pushing onto this queue and
        draining it from the main thread's own recurring self.after() poll
        loop (same pattern already used for _log_queue) never crosses
        threads, so it's the only mechanism confirmed safe here."""
        self._ui_queue.put(fn)

    def _drain_ui_queue(self):
        try:
            while True:
                fn = self._ui_queue.get_nowait()
                try:
                    fn()
                except Exception:
                    logger.exception("Queued UI callback failed")
        except queue.Empty:
            pass
        self.after(50, self._drain_ui_queue)

    def _set_progress(self, done: int, total: int, label: str = ""):
        self.progress.configure(maximum=max(total, 1), value=done)
        self.progress_label.configure(text=label or (f"{done}/{total}" if total else ""))

    def _on_wait(self, message: str):
        """Called from a background thread whenever a request has to pause
        (token refresh, rate-limit backoff) — surface it so a multi-second/
        minute wait doesn't look like the app has frozen."""
        self._log(message)
        self._ui(lambda: self.progress_label.configure(text=message))

    # ── Connect / login flow ────────────────────────────────────────────

    def _on_connect(self):
        self.connect_btn.configure(state="disabled")
        self.status_label.configure(text="Launching Chrome…")
        threading.Thread(target=self._connect_worker, daemon=True).start()

    def _connect_worker(self):
        try:
            if self._driver is not None and not browser.is_alive(self._driver):
                self._log("The Chrome window was closed — relaunching it.")
                try:
                    self._driver.quit()
                except Exception:
                    pass
                self._driver = None
                self._session = None
            if self._driver is None:
                self._driver = browser.create_driver(on_status=self._on_wait)
                self._session = api.ChatGPTSession(self._driver, on_wait=self._on_wait)
            api.ensure_on_chatgpt(self._driver)
            self._try_get_token()
        except browser.ChromeNotFoundError as e:
            self._log(str(e))
            self._ui(lambda: messagebox.showerror("Chrome not found", str(e)))
            self._ui(lambda: self.connect_btn.configure(state="normal"))
        except Exception as e:
            logger.exception("Connect failed")
            self._log(f"Unexpected error: {e}")
            self._ui(lambda: messagebox.showerror("Error", str(e)))
            self._ui(lambda: self.connect_btn.configure(state="normal"))

    def _try_get_token(self):
        try:
            self._session.token = api.get_access_token(self._driver)
        except api.AuthError:
            self._log("Not logged in yet. Log into ChatGPT in the Chrome window that opened, "
                       "then click \"I've logged in\".")
            self._ui(lambda: self.status_label.configure(text="Waiting for login…"))
            self._ui(lambda: self.login_btn.configure(state="normal"))
            self._ui(lambda: self.connect_btn.configure(state="normal"))
            return

        self._log("Connected to your ChatGPT session.")
        self._ui(lambda: self.login_btn.configure(state="disabled"))
        self._ui(lambda: self.connect_btn.configure(state="normal"))
        self._ui(lambda: self.status_label.configure(text="Connected — choose Smart Scan or Full Scan"))
        self._ui(lambda: self.smart_scan_btn.configure(state="normal"))
        self._ui(lambda: self.full_scan_btn.configure(state="normal"))

    def _on_confirm_login(self):
        self.login_btn.configure(state="disabled")
        threading.Thread(target=self._try_get_token, daemon=True).start()

    # ── Scanning (live source) ──────────────────────────────────────────

    def _on_smart_scan(self):
        output_dir = Path(self.output_var.get())  # read on the UI thread -- see _smart_scan_worker
        self.smart_scan_btn.configure(state="disabled")
        self.full_scan_btn.configure(state="disabled")
        threading.Thread(target=self._smart_scan_worker, args=(output_dir,), daemon=True).start()

    def _smart_scan_worker(self, output_dir: Path):
        # output_dir is passed in, read on the UI thread by the caller --
        # tk.StringVar.get() is not safe to call from a background thread
        # (can silently return a stale/wrong value, which previously showed
        # up as "didn't find my existing archive": every file looked missing
        # because the check ran against the wrong directory).
        #
        # Top-level safety net: whatever else goes wrong below, the buttons
        # must come back and the failure must be visible -- an uncaught
        # exception on a background thread otherwise just leaves the UI
        # stuck forever with no explanation, indistinguishable from a crash.
        try:
            cached_watermark = scan_cache.watermark(output_dir)
            if cached_watermark is not None and scan_cache.is_image_aware(output_dir):
                self._smart_scan_cached(output_dir, cached_watermark)
            elif cached_watermark is not None:
                # The cache exists but predates image-embedding support --
                # everything below its watermark was skipped without ever
                # being checked for an un-embedded image placeholder. Trusting
                # the fast path here would permanently hide those files from
                # every future Smart Scan. Do one full recheck instead; once
                # it exports cleanly the cache gets re-saved at the current
                # version and the fast path resumes on the next Smart Scan.
                self._log(
                    "Smart Scan: your cached sync point predates image embedding support -- "
                    "doing one full recheck so any existing exports with an un-embedded image "
                    "get picked up. This is a one-time thing; future Smart Scans go back to fast."
                )
                self._load_conversations(output_dir)
            else:
                self._smart_scan_heuristic(output_dir)
        except Exception as e:
            logger.exception("Smart Scan crashed")
            self._log(f"Smart Scan hit an unexpected error: {e}")
            self._ui(lambda: messagebox.showerror("Smart Scan error", str(e)))
            self._ui(lambda: self.smart_scan_btn.configure(state="normal"))
            self._ui(lambda: self.full_scan_btn.configure(state="normal"))

    def _smart_scan_cached(self, output_dir: Path, cached_watermark: float):
        """Fast repeat-visit path: a prior scan proved everything up to
        `cached_watermark` is fully archived, so only fetch conversations
        updated since then — stopping the moment we see one at or before it,
        since order=updated guarantees everything past that point is already
        covered. Cheap for a regularly-used archive: usually just 1 page."""
        self._log(f"Smart Scan: using the cached watermark for {output_dir} — only checking what's changed since your last full sync…")
        self._ui(lambda: self._set_progress(0, 1, "Smart Scan: checking for new activity…"))

        changed = []
        newest_seen = None
        try:
            for conv in self._session.iter_conversations(limit=2000):
                epoch = convert.coerce_timestamp(conv.get("update_time"))
                if newest_seen is None and epoch is not None:
                    newest_seen = epoch  # first item is the newest (order=updated desc)
                if epoch is not None and epoch <= cached_watermark:
                    break
                changed.append(conv)
        except Exception as e:
            logger.exception("Smart Scan (cached) failed to list conversations")
            self._log(f"Smart Scan failed: {e}")
            self._ui(lambda: messagebox.showerror("Error", str(e)))
            self._ui(lambda: self.smart_scan_btn.configure(state="normal"))
            self._ui(lambda: self.full_scan_btn.configure(state="normal"))
            return

        self._conversations = changed
        self._item_status = self._compute_item_statuses(changed, output_dir)
        self._source = "live"
        self._scan_mode = "smart_cached"
        self._scan_newest_update_time = newest_seen if newest_seen is not None else cached_watermark

        if changed:
            self._log(f"Smart Scan: {len(changed)} conversation(s) changed or new since your last full sync.")
        else:
            self._log("Smart Scan: nothing new since your last full sync — you're up to date.")
        self._ui(self._populate_list)
        self._ui(self._select_all)  # everything returned here needs action, by construction
        self._ui(lambda: self.status_label.configure(text=f"Smart Scan — {len(changed)} new/changed"))
        self._ui(lambda: self.export_btn.configure(state="normal" if changed else "disabled"))
        self._ui(lambda: self._set_progress(0, 1, ""))
        self._ui(lambda: self.smart_scan_btn.configure(state="normal"))
        self._ui(lambda: self.full_scan_btn.configure(state="normal"))

    def _smart_scan_heuristic(self, output_dir: Path):
        """First-visit path (no cache yet for this folder): sample the most
        recent conversations and infer whether the rest of the account is
        probably already archived. This is a best-effort guess, so unlike
        the cached path above, it never advances the watermark cache — only
        a provably-complete sync (this scan's full export, or a Full Scan)
        does that, in _export_worker."""
        self._log(
            f"Smart Scan: no cache yet for {output_dir} — checking your {SMART_SCAN_SAMPLE_SIZE} "
            f"most recently updated conversations…"
        )
        self._ui(lambda: self._set_progress(0, 1, "Smart Scan: listing recent conversations…"))
        try:
            sample = list(self._session.iter_conversations(limit=SMART_SCAN_SAMPLE_SIZE))
        except Exception as e:
            logger.exception("Smart Scan failed to list conversations")
            self._log(f"Smart Scan failed: {e}")
            self._ui(lambda: messagebox.showerror("Error", str(e)))
            self._ui(lambda: self.smart_scan_btn.configure(state="normal"))
            self._ui(lambda: self.full_scan_btn.configure(state="normal"))
            return

        item_status = self._compute_item_statuses(sample, output_dir)
        current = sum(1 for status, _ in item_status if status == convert.ExportStatus.CURRENT)
        ratio = current / len(sample) if sample else 0
        needs_action = len(sample) - current
        self._log(
            f"Smart Scan: {current}/{len(sample)} of your most recent conversations are "
            f"already archived and current ({ratio:.0%})."
        )

        self._conversations = sample
        self._item_status = item_status
        self._source = "live"

        if ratio >= SMART_SCAN_MATCH_THRESHOLD:
            self._scan_mode = "smart_heuristic"
            self._scan_newest_update_time = convert.coerce_timestamp(sample[0].get("update_time")) if sample else None
            self._log(
                f"Looks up to date — assuming conversations older than these {len(sample)} are "
                f"archived too. {needs_action} need exporting/updating; selecting those. Run Full "
                f"Scan instead if you want to check your entire history. (Export everything shown "
                f"here to start building a fast cache for next time.)"
            )
            self._ui(self._populate_list)
            self._ui(self._select_outdated)
            self._ui(lambda: self.status_label.configure(text=f"Smart Scan — {needs_action} need export"))
            self._ui(lambda: self.export_btn.configure(state="normal" if needs_action else "disabled"))
            self._ui(lambda: self._set_progress(0, 1, ""))
            self._ui(lambda: self.smart_scan_btn.configure(state="normal"))
            self._ui(lambda: self.full_scan_btn.configure(state="normal"))
        else:
            self._log(
                f"Only {current}/{len(sample)} matched — {output_dir} doesn't look like it already "
                f"has your full archive. Falling back to a Full Scan of everything…"
            )
            self._load_conversations(output_dir)

    def _on_full_scan(self):
        output_dir = Path(self.output_var.get())  # read on the UI thread
        self.smart_scan_btn.configure(state="disabled")
        self.full_scan_btn.configure(state="disabled")
        threading.Thread(target=self._load_conversations, args=(output_dir,), daemon=True).start()

    def _load_conversations(self, output_dir: Path):
        # Same safety-net reasoning as _smart_scan_worker: guarantee the
        # buttons come back and the failure is visible, no matter what breaks.
        try:
            self._load_conversations_inner(output_dir)
        except Exception as e:
            logger.exception("Full Scan crashed")
            self._log(f"Full Scan hit an unexpected error: {e}")
            self._ui(lambda: messagebox.showerror("Full Scan error", str(e)))
            self._ui(lambda: self.smart_scan_btn.configure(state="normal"))
            self._ui(lambda: self.full_scan_btn.configure(state="normal"))

    def _load_conversations_inner(self, output_dir: Path):
        self._log("Full Scan: fetching your entire conversation list…")
        self._ui(lambda: self._set_progress(0, 1, "Listing conversations…"))

        def on_page(done, total):
            self._log(f"  …{done}/{total} conversations listed")
            self._ui(lambda: self._set_progress(done, total, f"Listing: {done}/{total}"))

        try:
            convs = list(self._session.iter_conversations(on_page=on_page))
        except Exception as e:
            logger.exception("Failed to list conversations")
            self._log(f"Failed to list conversations: {e}")
            self._ui(lambda: messagebox.showerror("Error", str(e)))
            self._ui(lambda: self.smart_scan_btn.configure(state="normal"))
            self._ui(lambda: self.full_scan_btn.configure(state="normal"))
            return

        self._log(f"Found {len(convs)} conversations. Checking against {output_dir}…")
        self._ui(lambda: self._set_progress(0, 1, "Checking against your output folder…"))
        item_status = self._compute_item_statuses(convs, output_dir)

        self._conversations = convs
        self._item_status = item_status
        self._source = "live"
        self._scan_mode = "full"
        self._scan_newest_update_time = convert.coerce_timestamp(convs[0].get("update_time")) if convs else None
        self._ui(self._populate_list)
        self._ui(lambda: self._set_progress(0, 1, ""))
        self._ui(lambda: self.status_label.configure(text=f"Connected — {len(convs)} conversations"))
        self._ui(lambda: self.export_btn.configure(state="normal"))
        self._ui(lambda: self.smart_scan_btn.configure(state="normal"))
        self._ui(lambda: self.full_scan_btn.configure(state="normal"))

    # ── List display (shared by Smart Scan / Full Scan / file import) ──

    def _compute_item_statuses(self, conversations, output_dir: Path) -> list:
        """Checks each conversation against the output folder on disk. This is
        file I/O (open + read a line per conversation) -- always call this from
        a background thread and store the result before scheduling
        _populate_list, never from _populate_list itself. A Full Scan can be
        1000+ conversations; doing this synchronously on the Tk main thread
        would freeze the whole window for the duration.

        `output_dir` must be passed in (read on the UI thread by the caller),
        never read here via self.output_var.get() -- that's a tk.StringVar,
        not safe to read from a background thread."""
        statuses = []
        for conv in conversations:
            title = conv.get("title") or "Untitled conversation"
            base_name = convert.safe_filename(title, conv.get("id"), create_time=conv.get("create_time"))
            statuses.append(convert.local_export_status(output_dir, base_name, conv.get("update_time")))
        return statuses

    def _populate_list(self):
        """Pure UI rendering — no file I/O. Reads self._conversations paired
        with self._item_status, which the caller must have already computed
        (via _compute_item_statuses) before scheduling this via self.after."""
        self.listbox.delete(0, "end")
        for conv, (status, stale_days) in zip(self._conversations, self._item_status):
            title = conv.get("title") or "Untitled conversation"
            label = title
            epoch = convert.coerce_timestamp(conv.get("update_time"))
            if epoch is not None:
                try:
                    label += f"   ({datetime.fromtimestamp(epoch).strftime('%Y-%m-%d')})"
                except (OSError, OverflowError, ValueError):
                    pass  # some accounts have odd timestamps; don't let one bad date break the list

            if status == convert.ExportStatus.MISSING:
                label += "  [new]"
            elif status == convert.ExportStatus.STALE:
                if stale_days is not None:
                    label += f"  [changed ~{max(stale_days, 1):.0f}d ago]"
                else:
                    # STALE with no stale_days means the conversation itself
                    # hasn't changed -- it just still has an un-embedded
                    # image placeholder from before image support existed.
                    label += "  [image not embedded]"

            index = self.listbox.size()
            self.listbox.insert("end", label)
            color = self._status_color(status, stale_days)
            if color:
                self.listbox.itemconfig(index, foreground=color)

    def _status_color(self, status, stale_days) -> Optional[str]:
        if status == convert.ExportStatus.MISSING:
            return COLOR_NEW
        if status == convert.ExportStatus.STALE:
            if stale_days is None:
                return COLOR_STALE_YELLOW  # pending image embed, not a real staleness age
            return COLOR_STALE_RED if stale_days > STALE_YELLOW_MAX_DAYS else COLOR_STALE_YELLOW
        return None

    def _select_all(self):
        self.listbox.selection_set(0, "end")

    def _select_outdated(self):
        self.listbox.selection_clear(0, "end")
        for i, (status, _) in enumerate(self._item_status):
            if status in (convert.ExportStatus.MISSING, convert.ExportStatus.STALE):
                self.listbox.selection_set(i)

    # ── Bulk import (no live requests, no rate limits) ──────────────────

    def _on_import_file(self):
        path = filedialog.askopenfilename(
            title="Select a ChatGPT data export",
            filetypes=[("ChatGPT export", "*.zip *.json"), ("All files", "*.*")],
        )
        if not path:
            return
        output_dir = Path(self.output_var.get())  # read on the UI thread
        self.import_btn.configure(state="disabled")
        threading.Thread(target=self._import_worker, args=(Path(path), output_dir), daemon=True).start()

    def _import_worker(self, path: Path, output_dir: Path):
        try:
            self._import_worker_inner(path, output_dir)
        except Exception as e:
            logger.exception("Import crashed")
            self._log(f"Import hit an unexpected error: {e}")
            self._ui(lambda: messagebox.showerror("Import error", str(e)))
            self._ui(lambda: self.import_btn.configure(state="normal"))

    def _import_worker_inner(self, path: Path, output_dir: Path):
        self._log(f"Reading export file: {path.name}")
        try:
            raw = list(bulk_import.iter_conversations(path))
        except Exception as e:
            logger.exception("Failed to read export file")
            self._log(f"Failed to read export file: {e}")
            self._ui(lambda: messagebox.showerror("Import failed", str(e)))
            self._ui(lambda: self.import_btn.configure(state="normal"))
            return

        self._bulk_conversations = {}
        summaries = []
        for c in raw:
            cid = c.get("id") or c.get("conversation_id")
            self._bulk_conversations[cid] = c
            summaries.append({
                "id": cid,
                "title": c.get("title"),
                "create_time": c.get("create_time"),
                "update_time": c.get("update_time"),
            })

        self._log(f"Loaded {len(summaries)} conversations from {path.name}. Checking against {output_dir}…")
        item_status = self._compute_item_statuses(summaries, output_dir)

        self._conversations = summaries
        self._item_status = item_status
        self._source = "file"
        self._scan_mode = "file"
        # Unlike the live listing endpoint, export shards aren't guaranteed to be
        # ordered by update_time -- take the max across all of them.
        update_epochs = [
            e for e in (convert.coerce_timestamp(s.get("update_time")) for s in summaries) if e is not None
        ]
        self._scan_newest_update_time = max(update_epochs) if update_epochs else None
        self._ui(self._populate_list)
        self._ui(lambda: self.status_label.configure(text=f"Loaded from file — {len(summaries)} conversations"))
        self._ui(lambda: self.export_btn.configure(state="normal"))
        self._ui(lambda: self.import_btn.configure(state="normal"))

    def _browse_output(self):
        chosen = filedialog.askdirectory(initialdir=self.output_var.get() or str(Path.home()))
        if chosen:
            self.output_var.set(chosen)

    # ── Export ───────────────────────────────────────────────────────────

    def _on_export(self):
        selection = self.listbox.curselection()
        if not selection:
            messagebox.showinfo("Nothing selected", "Select at least one conversation to export.")
            return
        output_dir = Path(self.output_var.get())
        output_dir.mkdir(parents=True, exist_ok=True)
        selected = [self._conversations[i] for i in selection]
        on_conflict = self.conflict_var.get()  # read on the UI thread, passed into the worker

        # The watermark cache can only advance if this export covers every
        # conversation this scan found needing action -- a partial selection
        # can't safely claim "everything up to here is archived". The
        # ratio-based heuristic scan never qualifies regardless (see
        # scan_cache.PROVABLE_SCAN_MODES) since it's a guess, not a proof.
        can_advance_watermark = (
            scan_cache.can_advance(self._item_status, selection, self._scan_mode)
            and self._scan_newest_update_time is not None
        )

        self.export_btn.configure(state="disabled")
        threading.Thread(
            target=self._export_worker,
            args=(selected, output_dir, on_conflict, can_advance_watermark, self._scan_newest_update_time),
            daemon=True,
        ).start()

    def _make_image_fetcher(self, counts: dict):
        """Returns a callable for convert.extract_visible_messages(fetch_image=...)
        that resolves image_asset_pointer parts to inline data: URLs via the live
        browser session. Returns None from that callable for a retryable miss
        (no live session, or the download failed this time) -- convert.py falls
        back to a placeholder that local_export_status() will flag as worth
        retrying on a later scan. Returns False for a permanent skip (over the
        size cap) -- a placeholder that's deliberately *not* flagged for retry,
        since it'll never fit regardless of how many times we try.
        `counts` is mutated in place ({"attempted", "embedded"}) so the caller can
        log a per-conversation summary."""
        if self._source != "live" or self._session is None:
            return None

        def fetch_image(part: dict):
            asset_pointer = part.get("asset_pointer")
            if not asset_pointer:
                return None
            size_bytes = part.get("size_bytes")
            if isinstance(size_bytes, (int, float)) and size_bytes > MAX_IMAGE_EMBED_BYTES:
                return False
            counts["attempted"] += 1
            data_url = self._session.download_file_as_data_url(asset_pointer)
            if data_url:
                counts["embedded"] += 1
            return data_url

        return fetch_image

    def _fetch_full_conversation(self, conv_id: str) -> dict:
        """Returns the full node-mapping dict for one conversation, from whichever
        source is active. Raises browser.ChromeClosedError specifically when the
        live source's browser window is gone, so the caller can stop cleanly
        instead of failing every remaining item one at a time."""
        if self._source == "file":
            full = self._bulk_conversations.get(conv_id)
            if full is None:
                raise KeyError(f"{conv_id} not found in the loaded export file")
            return full
        if not browser.is_alive(self._driver):
            raise browser.ChromeClosedError("Chrome window was closed")
        return self._session.get_conversation(conv_id)

    def _export_worker(self, *args, **kwargs):
        try:
            self._export_worker_inner(*args, **kwargs)
        except Exception as e:
            logger.exception("Export crashed")
            self._log(f"Export hit an unexpected error: {e}")
            self._ui(lambda: messagebox.showerror("Export error", str(e)))
            self._ui(lambda: self.export_btn.configure(state="normal"))
            self._ui(lambda: self._set_progress(0, 1, ""))

    def _export_worker_inner(
        self, selected, output_dir: Path, on_conflict: str,
        can_advance_watermark: bool = False, scan_newest_update_time=None,
    ):
        total = len(selected)
        failures = 0
        empty = 0
        already_existed = 0
        updated = 0
        stopped_early = False
        for i, conv in enumerate(selected, start=1):
            conv_id = conv["id"]
            title = conv.get("title") or "Untitled conversation"
            update_time = conv.get("update_time")
            self._ui(lambda i=i: self._set_progress(i - 1, total, f"Exporting {i}/{total}"))

            # Resolve the output path BEFORE touching the network — for
            # "Skip it" this is the whole point (resuming a large export
            # shouldn't re-fetch conversations you already have), and it's
            # a cheap local check regardless of policy. Also check ahead of
            # time whether the existing copy (if any) is stale, purely for
            # a clearer "Saved" vs "Updated" log line below.
            base_name = convert.safe_filename(title, conv_id, create_time=conv.get("create_time"))
            status_before, stale_days_before = convert.local_export_status(output_dir, base_name, update_time)
            path = convert.resolve_output_path(
                output_dir, base_name, on_conflict=on_conflict, live_update_time=update_time
            )
            if path is None:
                self._log(f"[{i}/{total}] Skipped (up to date, no API call made): {base_name}.md")
                already_existed += 1
                self._ui(lambda i=i: self._set_progress(i, total, f"Exporting {i}/{total}"))
                continue

            self._log(f"[{i}/{total}] Exporting: {title}")
            try:
                full = self._fetch_full_conversation(conv_id)
            except browser.ChromeClosedError:
                self._log(
                    "  The Chrome window was closed — stopping here. Reconnect and export "
                    "again to pick up where this left off (use \"Skip it\" to avoid "
                    "re-fetching what already succeeded)."
                )
                stopped_early = True
                break
            except Exception as e:
                logger.exception("Export failed for %s", conv_id)
                self._log(f"  Failed: {e}")
                failures += 1
                self._ui(lambda i=i: self._set_progress(i, total, f"Exporting {i}/{total}"))
                if self._source == "live":
                    api.throttle()
                continue

            try:
                image_counts = {"attempted": 0, "embedded": 0}
                messages = convert.extract_visible_messages(
                    full, fetch_image=self._make_image_fetcher(image_counts)
                )
                if not messages:
                    self._log("  Skipped (no visible messages).")
                    empty += 1
                    continue
                md = convert.to_markdown(title, conv_id, messages, update_time=update_time)
                path.write_text(md, encoding="utf-8")
                if status_before == convert.ExportStatus.STALE:
                    if stale_days_before is not None:
                        self._log(f"  Updated (was ~{max(stale_days_before, 1):.0f} day(s) out of date): {path.name}")
                    else:
                        self._log(f"  Updated (filling in a previously un-embedded image): {path.name}")
                    updated += 1
                else:
                    self._log(f"  Saved: {path.name}")
                if image_counts["attempted"]:
                    self._log(f"    Images: {image_counts['embedded']}/{image_counts['attempted']} embedded")
            except Exception as e:
                logger.exception("Export failed for %s", conv_id)
                self._log(f"  Failed: {e}")
                failures += 1
            self._ui(lambda i=i: self._set_progress(i, total, f"Exporting {i}/{total}"))
            if self._source == "live":
                api.throttle()

        if can_advance_watermark and not stopped_early and failures == 0 and scan_newest_update_time is not None:
            scan_cache.save(output_dir, scan_newest_update_time)
            self._log(f"Cached this sync point — future Smart Scans of {output_dir} will be fast.")

        skipped = empty + already_existed
        summary = "Stopped early (Chrome closed)." if stopped_early else "Done."
        summary += f" Exported to {output_dir}"
        if updated:
            summary += f" ({updated} updated — had changed since last export)"
        if empty:
            summary += f" ({empty} skipped — no visible messages)"
        if already_existed:
            summary += f" ({already_existed} skipped — already up to date)"
        if failures:
            summary += f" ({failures} failed — see log)"
        self._log(summary)
        self._ui(lambda: self.export_btn.configure(state="normal"))
        self._ui(lambda: self._set_progress(0, 1, ""))
        self._ui(
            lambda: messagebox.showinfo(
                "Export stopped early" if stopped_early else "Export complete",
                f"Exported {total - skipped - failures}/{total} conversation(s) to:\n{output_dir}"
                + (f"\n\n{skipped} skipped, {failures} failed — see the log." if (skipped or failures) else "")
                + ("\n\nChrome was closed partway through. Reconnect and export the "
                   "remaining conversations again — use \"Skip it\" to avoid re-fetching "
                   "what already succeeded." if stopped_early else ""),
            )
        )

    # ── Lifecycle ────────────────────────────────────────────────────────

    def _on_close(self):
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:
                pass
        self.destroy()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = ArchiverApp()
    app.mainloop()
