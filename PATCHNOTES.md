# Patch Notes

All notable changes to the project, per release.

---

## Unreleased

**Honesty note**: the token-refresh fix below is verified by a unit test
against a fake driver (stale-token response detected, refreshed, retried,
and a persistent failure correctly raised instead of silently swallowed) —
it has not yet been confirmed live against a real ~1000-conversation export.
The dated-filenames crash below *was* confirmed live (reported failing on
every conversation) and the fix was verified against the real API response
shapes that caused it, pulled live from chatgpt.com.

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
- **Every export failed with `argument must be int or float, not str`**,
  introduced by the same-day dated-filenames feature below. Root cause:
  `create_time`/`update_time` on items from `/backend-api/conversations`
  are ISO 8601 strings (`"2026-08-09T19:19:09.712746Z"`), but per-message
  `create_time` inside a conversation's own node mapping is a numeric unix
  epoch — confirmed by querying both endpoints live. The filename code
  assumed the numeric shape everywhere. Added `convert.coerce_timestamp()`
  to accept either shape, used by both the filename and message-header
  formatters. This also silently affected the conversation list's date
  display the whole time (masked by a bare `except: pass`), now fixed too.

### Added
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
