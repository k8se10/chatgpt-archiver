# Patch Notes

All notable changes to the project, per release.

---

## v0.3.2 (2026-08-13) — Inline image embedding

**Honesty note**: image embedding is unit-tested for the Markdown-generation
side (`convert.extract_visible_messages(fetch_image=...)` — placeholder
fallback on no-fetcher, success, and failure all covered) and the live piece
(`ChatGPTSession.download_file_as_data_url` — the actual
`/backend-api/files/{id}/download` → presigned blob URL → base64 round trip)
is confirmed working against a real ChatGPT-served image on a live account —
images render correctly in a Markdown previewer. Separately, the JS/Selenium
fetch→blob→FileReader→base64 mechanism itself was verified byte-for-byte
lossless with a synthetic PNG round trip (isolated Chrome profile, no
ChatGPT auth needed), ruling out data corruption in that layer specifically.
Note that the base64 payload is only ever meant to render as a picture in an
actual Markdown renderer (VS Code preview, Obsidian, etc.) — opened in a
plain text editor, it correctly shows as a long, unreadable base64 string;
that's expected, not a bug.

### Added
- **Images are now fetched and embedded inline** as base64 `data:` URLs
  directly in the exported `.md`, when exporting via the live browser
  session (not the bulk file-import path — see Known Limitations in the
  README). Skips embedding (falls back to the placeholder) for anything
  over 8 MB.
- **Existing exports with an un-embedded image placeholder are now flagged
  as outdated** (yellow, "[image not embedded]") even though the
  conversation itself hasn't changed — so Smart Scan / Select Outdated /
  "Skip it" all correctly offer to fill them in now that embedding exists,
  instead of only ever catching genuinely-changed conversations. The
  "too large to embed" case (over 8 MB) is deliberately excluded from this
  — it'll never fit under the cap, so it isn't repeatedly re-flagged.
  Unit-tested (retryable placeholder → outdated; oversize placeholder →
  not re-flagged; genuine timestamp staleness still takes priority and
  isn't swallowed by the placeholder check).

### Fixed
- **Smart Scan's fast cached path could never actually find those
  now-outdated placeholder files.** Reported directly after shipping the
  above: on an account with an existing watermark cache from before image
  support existed, Smart Scan's fast path only ever looks at conversations
  updated *after* the cached watermark — it never re-opens the older files
  that were already trusted as "fully archived," so it could never see
  their placeholder text no matter how many times you ran it. Fixed by
  versioning the cache (`scan_cache.CACHE_VERSION`): a cache written before
  this fix is no longer trusted for the fast path. Smart Scan now does one
  full recheck the first time it sees an old cache, then re-saves it at
  the current version — after that one-time pass, the fast path resumes
  as normal. Unit-tested (`scan_cache.is_image_aware`).
  **Known residual limitation**: this closes the gap for the one-time
  migration, but if a *specific* image fails to embed later (a transient
  network error during an otherwise all-successful export) the watermark
  can still advance past it, since a placeholder alone isn't currently
  treated as an export failure. Re-running a Full Scan will always catch
  it regardless — just not necessarily the next fast Smart Scan. Flagging
  this rather than solving it now, since blocking the watermark on "zero
  images pending, ever" would make Smart Scan permanently slow on any
  account if the image API integration itself has an issue — worse than
  the gap it'd close, especially before that integration is confirmed
  working live.

## v0.3.1 (2026-08-12) — Watermark cache for repeat scans

**Honesty note**: `scan_cache.can_advance()` — the gate deciding whether a
watermark is allowed to advance — is unit-tested for every safety-relevant
case (partial selection, heuristic-mode exclusion, unknown-mode-fails-
closed, vacuous empty-scan case). The cached fast-path's pagination
stop-early logic is verified against the real live API (simulated a
watermark at conversation #2's `update_time`; correctly returned exactly
the single newest conversation as "changed", nothing more, nothing less).
Not yet verified: a full end-to-end cycle (export with the cache unset,
confirm it gets written, run Smart Scan again and confirm it actually
uses the fast path) — the pieces are each verified, the full chain hasn't
been watched end to end yet.

### Added
- **Smart Scan gets faster every time you use it on the same output
  folder**, instead of re-checking the same ~150 conversations on every
  run. After a scan proves full coverage — a Full Scan, a bulk import, or
  a Smart Scan itself once it's already using the cache — and that batch
  exports with zero failures, a small `.chatgpt-archiver-cache.json` is
  written next to your `.md` files recording how far the archive is
  caught up to. The next Smart Scan reads it back and only fetches
  conversations updated since then, stopping the moment it reaches
  already-covered ones — for a regularly-used archive this is usually a
  single page instead of a 150-conversation sample.
- **This only ever advances when it can be proven safe.** The first-visit
  ratio-based heuristic (no cache yet, sampling ~150 and inferring the
  rest) never writes the cache — it's a best-effort guess, and a wrong
  guess there would silently skip a conversation forever rather than just
  slowing down one scan. Only a scan that covers a real, provable window
  (Full Scan, an already-cached Smart Scan, or a bulk import), fully
  exported with nothing missed, is allowed to move the watermark forward.
  A partial export (only some of what needed updating got selected) never
  advances it either.
- The cache lives inside the output folder itself (`.chatgpt-archiver-
  cache.json`), so it travels with the archive and a different/empty
  folder correctly starts fresh rather than reading stale state from
  elsewhere.

### Fixed
- **Full Scan / Smart Scan could crash with `main thread is not in main
  loop`, or just hang.** The root cause: background threads were calling
  `self.after(0, ...)` directly to schedule UI updates, which is not
  reliably safe in Tkinter when used to *register a new callback* from a
  non-main thread — it can raise that exact `RuntimeError`, and depending
  on timing could also just leave the window looking frozen instead.
  Reproduced directly with a real `threading.Thread`-driven test (a
  single-threaded direct-call test earlier had missed this entirely, since
  it never exercised real cross-thread scheduling) and confirmed against a
  live 1058-conversation account.
  **Honesty note**: verified via a real background-thread Full Scan
  against a live account (1058 conversations, 1054 correctly matched as
  already-current, 4 correctly detected as new) with no crash.
- Fixed a related silent bug where `output_dir` was read via
  `self.output_var.get()` (a `tk.StringVar`) from background threads —
  not thread-safe, and could silently resolve to the wrong folder, making
  an entire existing archive look "missing." `output_dir` is now always
  read on the main thread inside each button handler and passed
  explicitly into every background-thread function.

## v0.3.0 (2026-08-12) — Smart Scan + update detection

**Honesty note**: the staleness-detection logic (`local_export_status`) is
verified by unit tests covering missing/current/stale/legacy-file cases,
and the filename-matching side of Smart Scan is verified against a real
account (150 live conversations checked against 1052 real, previously
exported files — 147 matched, and the 3 that didn't were genuinely new
conversations, zero false positives). What's *not* yet verified live: the
STALE path specifically (re-exporting a conversation that changed since
last archived), since none of the 1052 real files on hand predate this
feature. Logically sound and unit-tested, but flagging it as the one path
without a real-world confirmation yet.

### Added
- **Smart Scan**, a new default listing mode. Instead of paginating
  through your entire account (the only option before — "Full Scan",
  still available), it fetches just your ~150 most-recently-updated
  conversations, checks each against what's already in your output
  folder, and — if most of them already match — infers the rest of your
  (older) history is archived too and skips listing it at all. Falls back
  to a Full Scan automatically if the match rate is low (first run, or a
  different output folder). This is what actually cuts the hundreds of
  API calls: not just avoiding re-fetching content (Skip it, already
  fixed last release) but avoiding *listing* most of a large account in
  the first place.
- **Update detection.** Every exported `.md` now carries an invisible
  marker (an HTML comment, renders as nothing) recording the source
  conversation's `id` and `update_time`. Future scans read it back and
  compare against the live `update_time` to tell whether the conversation
  has changed since — not just "does a same-named file exist" like
  before. This makes **Skip it** actually mean "skip it *unless it's
  changed*": a stale file now gets refreshed instead of silently left
  behind. Files from before this feature (no marker) are treated as
  "assume current" rather than forced to re-export.
- **Outdated conversations are shown in the list, color-coded.** Yellow
  for changed recently (≤3 days), red for longer ago; a new **Select
  Outdated** button (next to Select All / Select None) selects everything
  that's either new or stale in one click.
- Export summary and per-item log lines now distinguish "Updated (was N
  days out of date)" from a fresh "Saved" and from "Skipped (up to
  date)".

### Docs
- **Corrected how long OpenAI's own data export takes to arrive.**
  Previously said "minutes to hours" — in practice it can take a few
  days, not minutes. Fixed in the README, the in-app hint, and
  `bulk_import.py`'s module docstring.

## v0.2.1 (2026-08-12) — Bulk import didn't actually work

**Honesty note**: v0.2.0's bulk-import format assumptions were "confirmed"
only by cross-referencing other people's write-ups, since we had no real
export to test against yet — flagged explicitly as unverified at the time.
That gap turned out to matter: it didn't handle the real format. This
release is verified against an actual ~200MB, 1051-conversation ChatGPT
export downloaded by the user — every single conversation in it now loads
and converts correctly, in well under a second.

### Fixed
- **Import Export File… didn't find anything in a real export.** OpenAI
  shards conversation data across multiple files for any non-trivial
  account — `conversations-000.json` through `conversations-010.json` in
  the real export tested, not a single `conversations.json` — but the
  importer only ever looked for the exact unsharded filename. Reworked to
  match any `conversations*.json` file in the zip (covering both the
  sharded and unsharded forms) and concatenate all of them. Verified
  against the real 1051-conversation export: all 11 shards found, all
  1051 conversations loaded and flowed through the conversion pipeline
  with zero failures.

## v0.2.0 (2026-08-12) — First public release

**Honesty note**: the token-refresh-under-load fix below is verified by a
unit test against a fake driver (stale-token/rate-limit responses detected,
refreshed/backed-off, retried, and a persistent failure correctly raised
instead of silently swallowed) — it has not been re-run against a real
1000+ conversation live export since. Everything else below was confirmed
live against the real chatgpt.com API and/or a real Chrome launch.

### Fixed
- **Exports silently skipped every conversation as "no visible messages"
  partway through large accounts (reported: after ~500 of 1049).** Root
  cause: the bearer token from `/api/auth/session` is short-lived, and a
  full export of a large account easily outlives it — an expired-token
  response has no `mapping` key, which was being treated as "conversation
  has no messages" instead of a real error. `ChatGPTSession` now detects an
  auth-error-shaped response, transparently refreshes the token, and
  retries once; a failure that survives the retry is now surfaced in the
  log as a real error instead of being silently counted as a skip.
- **Bulk export hit "too many requests" (HTTP 429) partway through a
  1049-conversation account and had no recovery path.** The previous
  retry logic only handled auth expiry (guessed from a missing `mapping`/
  `items` key in the body) and had no concept of rate limiting at all.
  Reworked to read the real HTTP status code instead of guessing from
  response shape: a 401/403 still triggers one token-refresh-and-retry,
  and a 429 now backs off (honouring `Retry-After` when the API sends
  one, otherwise exponential backoff from 5s up to a 120s cap) and
  retries up to 6 times before giving up with a real error. Also bumped
  the base per-request delay slightly (0.4s -> 0.6s) to make hitting the
  limit less likely in the first place.
- **Every export failed with `argument must be int or float, not str`.**
  Root cause: `create_time`/`update_time` on items from
  `/backend-api/conversations` are ISO 8601 strings
  (`"2026-08-09T19:19:09.712746Z"`), but per-message `create_time` inside a
  conversation's own node mapping is a numeric unix epoch — confirmed by
  querying both endpoints live. The filename code assumed the numeric
  shape everywhere. Added `convert.coerce_timestamp()` to accept either
  shape, used by both the filename and message-header formatters. This
  also silently affected the conversation list's date display the whole
  time (masked by a bare `except: pass`), now fixed too.
- **Rate-limit/token-refresh waits were invisible in the packaged app —
  looked frozen for up to 120s per retry.** The backoff messages only went
  through Python's `logging` module, which has nowhere to go in a
  PyInstaller `--windowed` build (no console attached) and was never
  routed to the GUI's own log box either. `ChatGPTSession` now takes an
  `on_wait` callback, wired to both the scrolling log and the progress
  label, so "Rate limited by ChatGPT — waiting 10s… (attempt 2/6)" is
  actually visible while it's happening.
- **Reconnecting after closing the Chrome window errored instead of
  relaunching.** `self._driver` kept referencing the dead Selenium session
  after the user closed the automated Chrome window, so clicking "Launch
  Chrome & Connect" again tried to use a browser that no longer existed.
  Added `browser.is_alive()` (a cheap liveness probe, verified live: True
  before quitting the driver, False after, without raising) and the
  connect flow now detects a dead driver and relaunches a fresh one
  instead of erroring.
- **"Skip it" (file-conflict handling) still fetched the full conversation
  over the network before checking whether it needed to.** For a large
  account this defeated the point of skipping — resuming an export after
  hitting a rate limit re-spent exactly the API calls "Skip it" was meant
  to avoid. The output path is now resolved from the local filesystem
  *before* any network call, so a skip is a pure local check with zero
  API cost. The export loop also now checks Chrome is still alive before
  each conversation and stops cleanly (instead of failing every remaining
  item one by one) if the window was closed mid-export.
- **Missing Chrome or a slow first-run chromedriver download both looked
  like the app was broken/frozen.** Chrome is a genuine external
  prerequisite this app can't bundle (it drives your own Chrome
  deliberately, rather than shipping a browser). It now checks the
  standard install locations directly before doing anything else and
  raises a clear "install Chrome from google.com/chrome" message instead
  of a raw Selenium stack trace if it's missing (verified against a
  simulated no-Chrome-installed case). The one-time chromedriver download
  on first run (needs internet) now reports through the same visible
  status mechanism used for rate-limit waits, instead of blocking
  silently — verified live: "Setting up the Chrome driver…" / "Launching
  Chrome…" both actually show up.

### Added
- **Import from ChatGPT's own bulk data export — the real fix for rate
  limits on large accounts, not a workaround.** The live path makes one
  request per conversation and will always eventually hit ChatGPT's rate
  limits on a large enough account; there's no way to "sidestep" that
  while still making per-conversation live requests, since the limiting
  is on OpenAI's side. Instead, a new **Import Export File…** button
  reads `conversations.json` out of the `.zip` OpenAI's own Settings →
  Data controls → Export data feature emails you — confirmed (via
  independent write-ups of the format, cross-referenced since we can't
  request-and-wait for a real export inside this session) to be the same
  tree schema the live API returns. It flows through the exact same
  `extract_visible_messages`/`to_markdown` pipeline unchanged (verified
  by a unit test), so behavior is identical to the live path — just zero
  network requests, and therefore zero rate limiting, for the whole
  archive. New `bulk_import.py` module; the live path is unchanged and
  still useful for exporting a handful of recent conversations without
  waiting for OpenAI's export email.
- **File-conflict handling.** A new "If a file already exists:" control
  next to the output folder lets you choose what happens when an export
  would overwrite an existing `.md` file: keep both (rename with a
  `(2)`/`(3)` suffix — the previous, only, behavior), replace it, or skip
  it and leave the existing file untouched. Useful for re-running an
  export over an existing archive without duplicating everything.
- **Progress bar + incremental log lines while listing conversations.**
  Listing a 1000+ conversation account previously gave zero feedback for
  the entire ~38-page fetch; it now logs and updates a progress bar every
  page, and again per-conversation during export.
- **Exported filenames are now dated.** Each `.md` file is prefixed with
  the conversation's start date/time (`YYYY-MM-DD HH-MM - Title.md`) so a
  folder of exports sorts chronologically in a normal file browser.
- **Export summary now reports skipped/failed counts** instead of just
  "done", so a partial export is visible at a glance.

### Docs
- Noted the large-account performance characteristics (throttled,
  per-conversation requests) under Known Limitations in `README.md`.
- **Documented rate limiting as a permanent external constraint on large
  archivals, not a bug.** `/backend-api/*` is ChatGPT's own internal
  endpoint with no published quota — HTTP 429 partway through a
  hundreds/thousands-conversation export is expected. The app's backoff
  (see Fixed, above) gets it through eventually, but the wall-clock
  ceiling for very large accounts is set by OpenAI's rate limits, not
  anything tunable in this project. Added under Known Limitations in
  `README.md`, with a pointer to the Skip-existing-files option and the
  bulk-import path for resuming/avoiding rate limits entirely.
