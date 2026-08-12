"""
gui.py — Minimal Tkinter front end for chatgpt_archiver.

Flow: launch a dedicated automated Chrome window -> log in there once if
needed -> list conversations -> pick some -> export to Markdown. All
network/browser calls run on a background thread so the UI never freezes.
"""

import logging
import queue
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import api, browser, bulk_import, convert

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "ChatGPT Archive"


class ArchiverApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ChatGPT Archiver")
        self.geometry("640x580")
        self.minsize(520, 460)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._driver = None
        self._session: api.ChatGPTSession | None = None
        self._conversations = []  # summary dicts: id/title/create_time/update_time
        self._source = None  # "live" (browser session) or "file" (bulk export)
        self._bulk_conversations = {}  # id -> full conversation dict, "file" source only
        self._log_queue = queue.Queue()

        self._build_widgets()
        self.after(100, self._drain_log_queue)

    # ── UI construction ──────────────────────────────────────────────────

    def _build_widgets(self):
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")

        self.connect_btn = ttk.Button(top, text="Launch Chrome & Connect", command=self._on_connect)
        self.connect_btn.pack(side="left")

        self.login_btn = ttk.Button(top, text="I've logged in", command=self._on_confirm_login, state="disabled")
        self.login_btn.pack(side="left", padx=6)

        self.import_btn = ttk.Button(top, text="Import Export File…", command=self._on_import_file)
        self.import_btn.pack(side="left", padx=6)

        self.status_label = ttk.Label(top, text="Not connected")
        self.status_label.pack(side="left", padx=10)

        hint_frame = ttk.Frame(self, padding=(10, 0))
        hint_frame.pack(fill="x")
        ttk.Label(
            hint_frame,
            text=(
                "Large account? ChatGPT's own bulk export avoids rate limits entirely: "
                "Settings → Data controls → Export data, then import the .zip it emails you here."
            ),
            foreground="#666666",
        ).pack(side="left")

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

        out_frame = ttk.Frame(self, padding=10)
        out_frame.pack(fill="x")
        ttk.Label(out_frame, text="Save to:").pack(side="left")
        self.output_var = tk.StringVar(value=str(DEFAULT_OUTPUT_DIR))
        ttk.Entry(out_frame, textvariable=self.output_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(out_frame, text="Browse…", command=self._browse_output).pack(side="left")

        conflict_frame = ttk.Frame(self, padding=(10, 0))
        conflict_frame.pack(fill="x")
        ttk.Label(conflict_frame, text="If a file already exists:").pack(side="left")
        self.conflict_var = tk.StringVar(value=convert.OnConflict.RENAME)
        ttk.Radiobutton(
            conflict_frame, text="Keep both (rename)", variable=self.conflict_var,
            value=convert.OnConflict.RENAME,
        ).pack(side="left", padx=(6, 0))
        ttk.Radiobutton(
            conflict_frame, text="Replace it", variable=self.conflict_var,
            value=convert.OnConflict.REPLACE,
        ).pack(side="left", padx=6)
        ttk.Radiobutton(
            conflict_frame, text="Skip it", variable=self.conflict_var,
            value=convert.OnConflict.SKIP,
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

    def _set_progress(self, done: int, total: int, label: str = ""):
        self.progress.configure(maximum=max(total, 1), value=done)
        self.progress_label.configure(text=label or (f"{done}/{total}" if total else ""))

    def _on_wait(self, message: str):
        """Called from a background thread whenever a request has to pause
        (token refresh, rate-limit backoff) — surface it so a multi-second/
        minute wait doesn't look like the app has frozen."""
        self._log(message)
        self.after(0, lambda: self.progress_label.configure(text=message))

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
            self.after(0, lambda: messagebox.showerror("Chrome not found", str(e)))
            self.after(0, lambda: self.connect_btn.configure(state="normal"))
        except Exception as e:
            logger.exception("Connect failed")
            self._log(f"Unexpected error: {e}")
            self.after(0, lambda: messagebox.showerror("Error", str(e)))
            self.after(0, lambda: self.connect_btn.configure(state="normal"))

    def _try_get_token(self):
        try:
            self._session.token = api.get_access_token(self._driver)
        except api.AuthError:
            self._log("Not logged in yet. Log into ChatGPT in the Chrome window that opened, "
                       "then click \"I've logged in\".")
            self.after(0, lambda: self.status_label.configure(text="Waiting for login…"))
            self.after(0, lambda: self.login_btn.configure(state="normal"))
            self.after(0, lambda: self.connect_btn.configure(state="normal"))
            return

        self._log("Connected to your ChatGPT session.")
        self.after(0, lambda: self.login_btn.configure(state="disabled"))
        self._load_conversations()

    def _on_confirm_login(self):
        self.login_btn.configure(state="disabled")
        threading.Thread(target=self._try_get_token, daemon=True).start()

    def _load_conversations(self):
        self._log("Fetching conversation list…")
        self.after(0, lambda: self._set_progress(0, 1, "Listing conversations…"))

        def on_page(done, total):
            self._log(f"  …{done}/{total} conversations listed")
            self.after(0, lambda: self._set_progress(done, total, f"Listing: {done}/{total}"))

        try:
            convs = list(self._session.iter_conversations(on_page=on_page))
        except Exception as e:
            logger.exception("Failed to list conversations")
            self._log(f"Failed to list conversations: {e}")
            self.after(0, lambda: messagebox.showerror("Error", str(e)))
            self.after(0, lambda: self.connect_btn.configure(state="normal"))
            return

        self._conversations = convs
        self._source = "live"
        self._log(f"Found {len(convs)} conversations.")
        self.after(0, self._populate_list)
        self.after(0, lambda: self._set_progress(0, 1, ""))
        self.after(0, lambda: self.status_label.configure(text=f"Connected — {len(convs)} conversations"))
        self.after(0, lambda: self.export_btn.configure(state="normal"))
        self.after(0, lambda: self.connect_btn.configure(state="normal"))

    def _populate_list(self):
        self.listbox.delete(0, "end")
        for conv in self._conversations:
            title = conv.get("title") or "Untitled conversation"
            label = title
            epoch = convert.coerce_timestamp(conv.get("update_time"))
            if epoch is not None:
                label += f"   ({datetime.fromtimestamp(epoch).strftime('%Y-%m-%d')})"
            self.listbox.insert("end", label)

    def _select_all(self):
        self.listbox.selection_set(0, "end")

    # ── Bulk import (no live requests, no rate limits) ──────────────────

    def _on_import_file(self):
        path = filedialog.askopenfilename(
            title="Select a ChatGPT data export",
            filetypes=[("ChatGPT export", "*.zip *.json"), ("All files", "*.*")],
        )
        if not path:
            return
        self.import_btn.configure(state="disabled")
        self.connect_btn.configure(state="disabled")
        threading.Thread(target=self._import_worker, args=(Path(path),), daemon=True).start()

    def _import_worker(self, path: Path):
        self._log(f"Reading export file: {path.name}")
        try:
            raw = list(bulk_import.iter_conversations(path))
        except Exception as e:
            logger.exception("Failed to read export file")
            self._log(f"Failed to read export file: {e}")
            self.after(0, lambda: messagebox.showerror("Import failed", str(e)))
            self.after(0, lambda: self.import_btn.configure(state="normal"))
            self.after(0, lambda: self.connect_btn.configure(state="normal"))
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

        self._conversations = summaries
        self._source = "file"
        self._log(f"Loaded {len(summaries)} conversations from {path.name} — no API calls made.")
        self.after(0, self._populate_list)
        self.after(0, lambda: self.status_label.configure(text=f"Loaded from file — {len(summaries)} conversations"))
        self.after(0, lambda: self.export_btn.configure(state="normal"))
        self.after(0, lambda: self.import_btn.configure(state="normal"))
        self.after(0, lambda: self.connect_btn.configure(state="normal"))

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
        self.export_btn.configure(state="disabled")
        threading.Thread(
            target=self._export_worker, args=(selected, output_dir, on_conflict), daemon=True
        ).start()

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

    def _export_worker(self, selected, output_dir: Path, on_conflict: str):
        total = len(selected)
        failures = 0
        empty = 0
        already_existed = 0
        stopped_early = False
        for i, conv in enumerate(selected, start=1):
            conv_id = conv["id"]
            title = conv.get("title") or "Untitled conversation"
            self.after(0, lambda i=i: self._set_progress(i - 1, total, f"Exporting {i}/{total}"))

            # Resolve the output path BEFORE touching the network — for
            # "Skip it" this is the whole point (resuming a large export
            # shouldn't re-fetch conversations you already have), and it's
            # a cheap local check regardless of policy.
            base_name = convert.safe_filename(title, conv_id, create_time=conv.get("create_time"))
            path = convert.resolve_output_path(output_dir, base_name, on_conflict=on_conflict)
            if path is None:
                self._log(f"[{i}/{total}] Skipped (already exists, no API call made): {base_name}.md")
                already_existed += 1
                self.after(0, lambda i=i: self._set_progress(i, total, f"Exporting {i}/{total}"))
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
                self.after(0, lambda i=i: self._set_progress(i, total, f"Exporting {i}/{total}"))
                if self._source == "live":
                    api.throttle()
                continue

            try:
                messages = convert.extract_visible_messages(full)
                if not messages:
                    self._log("  Skipped (no visible messages).")
                    empty += 1
                    continue
                md = convert.to_markdown(title, conv_id, messages)
                path.write_text(md, encoding="utf-8")
                self._log(f"  Saved: {path.name}")
            except Exception as e:
                logger.exception("Export failed for %s", conv_id)
                self._log(f"  Failed: {e}")
                failures += 1
            self.after(0, lambda i=i: self._set_progress(i, total, f"Exporting {i}/{total}"))
            if self._source == "live":
                api.throttle()

        skipped = empty + already_existed
        summary = "Stopped early (Chrome closed)." if stopped_early else "Done."
        summary += f" Exported to {output_dir}"
        if empty:
            summary += f" ({empty} skipped — no visible messages)"
        if already_existed:
            summary += f" ({already_existed} skipped — already existed)"
        if failures:
            summary += f" ({failures} failed — see log)"
        self._log(summary)
        self.after(0, lambda: self.export_btn.configure(state="normal"))
        self.after(0, lambda: self._set_progress(0, 1, ""))
        self.after(
            0,
            lambda: messagebox.showinfo(
                "Export stopped early" if stopped_early else "Export complete",
                f"Exported {total - skipped - failures}/{total} conversation(s) to:\n{output_dir}"
                + (f"\n\n{skipped} skipped, {failures} failed — see the log." if (skipped or failures) else "")
                + ("\n\nChrome was closed partway through. Reconnect and export the "
                   "remaining conversations again — use \"Skip it\" to avoid re-fetching "
                   "what already succeeded." if stopped_early else ""),
            ),
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
