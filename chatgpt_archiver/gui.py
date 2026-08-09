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

from . import api, browser, convert

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
        self._conversations = []  # list of dicts from the API
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

        self.status_label = ttk.Label(top, text="Not connected")
        self.status_label.pack(side="left", padx=10)

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
            if self._driver is None:
                self._driver = browser.create_driver()
                self._session = api.ChatGPTSession(self._driver, on_wait=self._on_wait)
            api.ensure_on_chatgpt(self._driver)
            self._try_get_token()
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
        self.export_btn.configure(state="disabled")
        threading.Thread(target=self._export_worker, args=(selected, output_dir), daemon=True).start()

    def _export_worker(self, selected, output_dir: Path):
        total = len(selected)
        failures = 0
        skipped = 0
        for i, conv in enumerate(selected, start=1):
            conv_id = conv["id"]
            title = conv.get("title") or "Untitled conversation"
            self._log(f"[{i}/{total}] Exporting: {title}")
            self.after(0, lambda i=i: self._set_progress(i - 1, total, f"Exporting {i}/{total}"))
            try:
                full = self._session.get_conversation(conv_id)
                messages = convert.extract_visible_messages(full)
                if not messages:
                    self._log("  Skipped (no visible messages).")
                    skipped += 1
                    continue
                md = convert.to_markdown(title, conv_id, messages)
                base_name = convert.safe_filename(title, conv_id, create_time=conv.get("create_time"))
                path = convert.unique_path(output_dir, base_name)
                path.write_text(md, encoding="utf-8")
                self._log(f"  Saved: {path.name}")
            except Exception as e:
                logger.exception("Export failed for %s", conv_id)
                self._log(f"  Failed: {e}")
                failures += 1
            self.after(0, lambda i=i: self._set_progress(i, total, f"Exporting {i}/{total}"))
            api.throttle()

        summary = f"Done. Exported to {output_dir}"
        if skipped:
            summary += f" ({skipped} skipped — no visible messages)"
        if failures:
            summary += f" ({failures} failed — see log)"
        self._log(summary)
        self.after(0, lambda: self.export_btn.configure(state="normal"))
        self.after(0, lambda: self._set_progress(0, 1, ""))
        self.after(
            0,
            lambda: messagebox.showinfo(
                "Export complete",
                f"Exported {total - skipped - failures}/{total} conversation(s) to:\n{output_dir}"
                + (f"\n\n{skipped} skipped, {failures} failed — see the log." if (skipped or failures) else ""),
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
