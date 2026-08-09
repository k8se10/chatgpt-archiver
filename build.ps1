# Build a standalone "ChatGPT Archiver.exe" with PyInstaller.
#
# Usage:
#   .\build.ps1            # onedir build (faster startup)
#   .\build.ps1 -OneFile   # single .exe (slower startup, easier to hand out)

param(
    [switch]$OneFile
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path ".venv")) {
    python -m venv .venv
}

& .venv\Scripts\pip install --upgrade pip | Out-Null
& .venv\Scripts\pip install -r requirements-dev.txt

$mode = if ($OneFile) { "--onefile" } else { "--onedir" }
& .venv\Scripts\pyinstaller $mode --windowed --noconfirm --name "ChatGPT Archiver" run.py

Write-Host ""
Write-Host "Build complete: dist\ChatGPT Archiver\"
