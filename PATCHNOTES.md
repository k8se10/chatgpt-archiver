# Patch Notes

All notable changes to the project, per release.

---

## Unreleased

**Honesty note**: the token-refresh fix below is verified by a unit test
against a fake driver (stale-token response detected, refreshed, retried,
and a persistent failure correctly raised instead of silently swallowed) —
it has not yet been confirmed live against a real ~1000-conversation export.

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
