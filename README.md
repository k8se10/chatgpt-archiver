# ChatGPT Archiver

ChatGPT's "select all" / copy on chatgpt.com only grabs whatever's currently
rendered on screen, not the whole conversation. This tool sidesteps that
entirely: instead of scraping the page, it asks the same backend API the
ChatGPT web app itself uses for the conversation's raw message data, and
writes it out as clean Markdown.

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

## Requirements

- Windows, with Google Chrome installed
- Python 3.10+

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
.\build.ps1            # dist\ChatGPT Archiver\ChatGPT Archiver.exe
.\build.ps1 -OneFile   # single-file exe (slower to start, easier to share)
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

## Contributing

Issues and PRs welcome. If ChatGPT changes its API shape, the surface area
to fix is small and isolated: `chatgpt_archiver/api.py` (the endpoints) and
`chatgpt_archiver/convert.py` (the tree-walk and Markdown rendering).

## License

Custom source-available license — free to use, modify, and fork, but not to
resell, paywall, or otherwise charge for. See [LICENSE](LICENSE) for the
full terms, including a required-attribution clause and a disclaimer about
relying on an undocumented, internal ChatGPT API endpoint.
