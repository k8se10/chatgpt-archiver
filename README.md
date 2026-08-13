# ChatGPT Archiver

ChatGPT's "select all" / copy on chatgpt.com only grabs whatever's currently
rendered on screen, not the whole conversation. This tool sidesteps that
entirely: instead of scraping the page, it asks the same backend API the
ChatGPT web app itself uses for the conversation's raw message data, and
writes it out as clean Markdown.

## Download

Grab the latest `ChatGPT-Archiver.exe` from
[Releases](https://github.com/k8se10/chatgpt-archiver/releases) — a single
file, no installer, no Python required. Double-click it and go. The only
thing it needs already on your machine is Google Chrome (it drives your
own Chrome, in its own separate automation profile, rather than bundling
a browser — see How it works below); if Chrome isn't found it'll tell you
clearly instead of failing silently.

## How it works

1. A small, dedicated Chrome window (its own profile — not your everyday
   Chrome) opens to chatgpt.com. You log in there once; the session persists
   across future runs like any normal browser profile.
2. From inside that page, the tool calls the exact same endpoints the
   ChatGPT frontend calls (`/api/auth/session` for a token, then
   `/backend-api/conversations` and `/backend-api/conversation/{id}`), using
   the browser's own authenticated `fetch()`. Nothing is decrypted, no
   cookie files are read off disk — it's just the browser making requests it
   already has permission to make.
3. **Smart Scan** (the default) fetches just your ~150 most-recently-updated
   conversations and checks each against what's already in your output
   folder. If most already match, it infers the rest of your (older)
   history is archived too and skips listing it — this is what actually
   avoids hundreds of API calls on a large account, not just avoiding
   re-fetching content. **Full Scan** lists your entire account instead, if
   you want to check everything explicitly. Once a scan has fully synced a
   folder, a small cache file records how far it got, so every Smart Scan
   after that only checks what's changed since — usually one page, not 150.
4. Each conversation comes back as a full tree of every edit/regeneration
   branch. The tool walks the single path that's actually shown on screen
   (from `current_node` back to the root) and drops anything that never
   renders — system prompts, tool calls, hidden reasoning/thinking-channel
   messages — so the export matches what you'd see in the UI.
5. The result is written as one `.md` file per conversation: a `## You` /
   `## ChatGPT` heading per turn, original Markdown formatting (code blocks,
   lists, etc.) preserved exactly as authored, no lossy HTML round-trip. An
   invisible marker at the top records the conversation's `id` and
   `update_time`, so a later scan can tell whether it's changed since —
   changed/new conversations show up color-coded (yellow = recently,
   red = longer ago) in the list, with a **Select Outdated** button to
   grab all of them in one click. **Skip it** (the default conflict
   policy) means "skip it unless it's changed" — a stale file gets
   refreshed, not silently left behind. An existing export that still has
   an un-embedded image placeholder (see Known Limitations) also shows up
   as outdated, even if the conversation itself hasn't changed — so
   re-running an export after an update that adds image support fills
   images in automatically instead of leaving old exports behind.

## Large accounts: use ChatGPT's own bulk export instead

The live path above makes one request per conversation, which is fine for
a handful of chats but will eventually hit ChatGPT's rate limits on an
account with hundreds or thousands of them (the app backs off and
retries automatically, but there's a real wall-clock cost — see Known
Limitations).

For a full archive, sidestep that entirely: in chatgpt.com, go to
**Settings → Data controls → Export data**. OpenAI emails you a `.zip`
containing every conversation you have — as one or more
`conversations*.json` files (OpenAI shards this across several files,
e.g. `conversations-000.json`, `conversations-001.json`, … for any
non-trivial account) — in the same format the live API returns. Click
**Import Export File…** in the app and pick that `.zip` — same
conversation list, same selection UI, same Markdown output, but reading
a local file instead of making any network requests at all, so it's
effectively instant once the file exists.

The only catch: generating that `.zip` is **not instant on OpenAI's
side** — it's a background job and the email can take a few days to
arrive, not minutes. Worth requesting it well ahead of when you actually
need the archive.

## Requirements

- **Prebuilt `.exe` (Download above):** Windows + Google Chrome. Nothing else.
- **Running from source / building it yourself:** also Python 3.10+.

## Running from source

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python run.py
```

Click **Launch Chrome & Connect**. If it's your first run, a Chrome window
opens to chatgpt.com — log in, then click **I've logged in** back in the app.
Click **Smart Scan** (or **Full Scan** to check your entire account),
select the conversations you want (or **Select Outdated** to grab
everything new/changed), pick an output folder, and hit **Export
Selected**.

## Building the standalone .exe

```powershell
.\build.ps1            # dist\ChatGPT Archiver\ChatGPT Archiver.exe (onedir, faster startup)
.\build.ps1 -OneFile   # dist\ChatGPT Archiver.exe (single file — what's attached to Releases)
```

## Privacy

- Everything runs locally. The tool only talks to `chatgpt.com`, using your
  own logged-in session, to fetch your own conversations.
- Nothing is uploaded anywhere else.
- The automation Chrome profile lives at
  `%LOCALAPPDATA%\ChatGPTArchiver\chrome-profile` — delete that folder to
  fully log out / reset it.

## Known limitations

- Windows only for now (the `.exe` build and default profile path assume
  Windows; the Python code itself is mostly cross-platform if you adjust
  `browser.profile_dir()`).
- **Images are fetched and embedded inline** (as base64, right in the
  `.md` file — no separate image files to keep track of) when exporting
  via the live browser session. This only works for the live path — the
  bulk **Import Export File…** path has no browser session to fetch
  through, so images there (and anything over 8 MB, and non-image
  attachments like other files) still show as a `*[image attached]*` /
  `*[unsupported attachment]*` placeholder instead.
- Only exports conversations, not Projects/custom GPT metadata.
- **Large accounts are slow — Smart Scan and "Skip it" both help, but
  don't eliminate this.** Every conversation still exported is a separate
  authenticated request through the browser, throttled to stay polite to
  the API. Smart Scan avoids *listing* most of a large, already-archived
  account, and "Skip it" avoids re-fetching content you already have —
  but the first time you archive a large account (or a Smart Scan that
  falls back to Full Scan), you're still looking at one request per
  conversation. There's a progress bar and a running log so it's clear
  the app hasn't hung, but there's no way around the wall-clock cost of
  that first full pass other than Import Export File… (see above).
- **Rate limiting on large archivals via the live path is a permanent
  external constraint, not a bug this app can fix.** `/backend-api/*` is
  ChatGPT's own internal endpoint, not a public API with published
  quotas — for a library in the hundreds/thousands, hitting HTTP 429
  partway through is expected, not exceptional. The app backs off and
  retries automatically (see `ChatGPTSession` in `api.py`), which gets a
  full export through eventually, but "eventually" can mean a genuinely
  long wall-clock time for very large accounts, and that ceiling is set
  by OpenAI's rate limits, not by anything tunable here. Two ways around
  it: use **Import Export File…** instead (see above — zero live
  requests, so no rate limit to hit at all), or if you do run the live
  path and it gets interrupted, rerun it with **Skip it** selected under
  file-conflict handling to resume without re-fetching what already
  succeeded.

## Changelog

See [PATCHNOTES.md](PATCHNOTES.md) for what's changed release to release.

## Contributing

Issues and PRs welcome. If ChatGPT changes its API shape, the surface area
to fix is small and isolated: `chatgpt_archiver/api.py` (the endpoints) and
`chatgpt_archiver/convert.py` (the tree-walk and Markdown rendering).

## License

Custom source-available license — free to use, modify, and fork, but not to
resell, paywall, or otherwise charge for. See [LICENSE](LICENSE) for the
full terms, including a required-attribution clause and a disclaimer about
relying on an undocumented, internal ChatGPT API endpoint.
