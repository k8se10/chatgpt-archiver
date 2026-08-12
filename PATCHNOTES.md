# Patch Notes

All notable changes to the project, per release.

---

## Unreleased

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
