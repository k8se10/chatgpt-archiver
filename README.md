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
3. Each conversation comes back as a full tree of every edit/regeneration
   branch. The tool walks the single path that's actually shown on screen
   (from `current_node` back to the root) and drops anything that never
   renders — system prompts, tool calls, hidden reasoning/thinking-channel
   messages — so the export matches what you'd see in the UI.
4. The result is written as one `.md` file per conversation: a `## You` /
   `## ChatGPT` heading per turn, original Markdown formatting (code blocks,
   lists, etc.) preserved exactly as authored, no lossy HTML round-trip.

## Large accounts: use ChatGPT's own bulk export instead

The live path above makes one request per conversation, which is fine for
a handful of chats but will eventually hit ChatGPT's rate limits on an
account with hundreds or thousands of them (the app backs off and
retries automatically, but there's a real wall-clock cost — see Known
Limitations).

For a full archive, sidestep that entirely: in chatgpt.com, go to
**Settings → Data controls → Export data**. OpenAI emails you a `.zip`
(can take minutes to hours to generate) containing `conversations.json` —
every conversation you have, in the same format the live API returns,
generated once, server-side, with zero requests from this app. Click
**Import Export File…** in the app and pick that `.zip` (or the
`conversations.json` inside it) — same conversation list, same selection
UI, same Markdown output, but reading a local file instead of making any
network requests at all.

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
Select the conversations you want, pick an output folder, and hit
**Export Selected**.

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
- Attachments (images, files) are exported as a `*[image attached]*` /
  `*[unsupported attachment]*` placeholder, not the actual file — the API
  returns a pointer, not the binary.
- Only exports conversations, not Projects/custom GPT metadata.
- **Large accounts are slow.** Every conversation is a separate authenticated
  request through the browser, throttled to stay polite to the API — a
  library of ~1000 conversations takes a while to both list and export.
  There's a progress bar and a running log so it's clear the app hasn't
  hung, but there's no fast path yet. If you only need a handful of
  conversations, select just those instead of "Select All".
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
