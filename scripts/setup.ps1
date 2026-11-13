# Aether-OS — machine setup for the terminal-run flow (no installer).
#
# One idempotent command that reconciles this machine to the two
# lockfiles and then proves it:
#   uv.lock            -> the engine's Python environment (.venv)
#   package-lock.json  -> the desktop app's node_modules
#
# Run it on a NEW machine after cloning, and run it AGAIN on an older
# machine after `git pull` — that re-run IS the update/sync flow.
# `uv sync --locked` and `npm ci` both reconcile exactly to their lock
# (adding, removing and re-pinning as needed), and neither ever edits
# a lockfile. If a step fails, the script stops and names the step.
#
# Prerequisites are only checked, never installed silently: Git, uv,
# Node 24 + npm. Python 3.12 is provisioned by uv itself when missing.
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')

function Fail([string]$Msg) {
    Write-Host "setup: FAILED — $Msg" -ForegroundColor Red
    exit 1
}
function Step([string]$Msg) { Write-Host ""; Write-Host "==> $Msg" }
function Run([string]$StepName, [scriptblock]$Cmd) {
    & $Cmd
    if ($LASTEXITCODE -ne 0) { Fail $StepName }
}

# --- 1. prerequisites ---------------------------------------------------
Step "Checking prerequisites"
if (-not (Get-Command git  -ErrorAction SilentlyContinue)) { Fail "git not found. Install: winget install --id Git.Git -e" }
if (-not (Get-Command uv   -ErrorAction SilentlyContinue)) { Fail "uv not found. Install: winget install --id astral-sh.uv -e" }
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { Fail "node not found. Install Node 24: winget install --id OpenJS.NodeJS.LTS -e" }
if (-not (Get-Command npm  -ErrorAction SilentlyContinue)) { Fail "npm not found. It ships with Node — reinstall Node 24." }
$nodeVersion = (node --version)
$nodeMajor = $nodeVersion -replace '^v(\d+).*', '$1'
if ($nodeMajor -ne '24') { Fail "Node 24 required (see .nvmrc), found $nodeVersion. Install Node 24: winget install --id OpenJS.NodeJS.LTS -e" }
Write-Host "git $((git --version) -replace 'git version ',''), uv $((uv --version) -replace 'uv ',''), node $nodeVersion, npm $(npm --version)"

# --- 2. Python venv ------------------------------------------------------
Step "Creating the Python venv (3.12) if needed"
if (-not (Test-Path '.venv\Scripts\python.exe')) {
    Run "uv venv" { uv venv .venv --python 3.12 }
} else {
    Write-Host ".venv already exists — keeping it; the sync below reconciles it."
}

# --- 3. Engine dependencies, exactly from uv.lock ------------------------
Step "Installing engine dependencies from uv.lock (all extras)"
Run "uv sync --locked --all-extras" { uv sync --locked --all-extras }

# --- 4. Desktop dependencies, exactly from package-lock.json -------------
Step "Installing desktop dependencies from package-lock.json (inside desktop_app)"
Push-Location desktop_app
Run "npm ci" { npm ci }
Pop-Location

# --- 5. Smoke checks ------------------------------------------------------
Step "Smoke check 1/3: locked venv imports (same list CI verifies)"
Run "import smoke test" { .venv\Scripts\python.exe -c "import sys; assert sys.version_info[:2] == (3, 12), sys.version; import uvicorn, google.genai, apscheduler, ddgs, keyring, aiosqlite, faster_whisper, kokoro, onnxruntime, silero_vad; print('locked venv import smoke: ok')" }

Step "Smoke check 2/3: frontend builds (vite resolves every import)"
Push-Location desktop_app
Run "npm run build" { npm run build }
Pop-Location

Step "Smoke check 3/3: the whole pytest suite collects (engine imports clean)"
$collectLog = Join-Path $env:TEMP 'aether-pytest-collect.txt'
.venv\Scripts\python.exe -m pytest --collect-only -q *> $collectLog
if ($LASTEXITCODE -ne 0) { Get-Content $collectLog -Tail 30; Fail "pytest collection" }
Get-Content $collectLog -Tail 2

Write-Host ""
Write-Host "setup: OK — this machine matches the locks."
Write-Host "  Python: $(.venv\Scripts\python.exe --version) (.venv)   Node: $nodeVersion"
Write-Host "  Run Aether with:  scripts\run.ps1"
