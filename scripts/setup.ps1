# Aether-OS - machine setup for the terminal-run flow (no installer).
#
# One idempotent command that reconciles this machine EXACTLY to the
# project's pinned sources of truth, and then proves it:
#   uv.lock                    -> the engine's Python environment (.venv)
#   package-lock.json          -> the repo-root node_modules (commit tools)
#   desktop_app/package-lock.json -> the desktop app's node_modules
#   .python-version            -> the exact CPython (3.12.16)
#   .nvmrc                     -> the exact Node (24.22.1)
#
# Run it on a NEW machine after cloning, and run it AGAIN on an older
# machine after `git pull` (scripts\update.ps1 does both) - that re-run
# IS the update/sync flow. Nothing here ever installs from a version
# range or edits a lockfile.
#
# CLEAN-INSTALL RULE (owner directive, 15 Nov 2026): the script keeps
# a fingerprint of the lockfiles + version pins in
# `.aether-setup-stamp`. If the fingerprint does not match the last
# successful setup - the locks moved, a pin changed, or the stamp is
# missing - the old environments are WIPED (.venv, both node_modules,
# any stale engine-payload) and rebuilt from scratch. A drifted
# environment is never patched in place: a stale mixed install is
# exactly how a broken litellm/mcp pair once shipped in an installer.
# When the fingerprint matches, `uv sync --locked` + `npm ci` still
# reconcile exactly (npm ci is a clean install by definition).
#
# Prerequisites are only checked, never installed silently: Git, uv,
# and the exact Node from .nvmrc. Python is provisioned by uv itself,
# at the exact .python-version. If a step fails, the script stops
# and names the step.
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')

function Fail([string]$Msg) {
    Write-Host "setup: FAILED - $Msg" -ForegroundColor Red
    exit 1
}
function Step([string]$Msg) { Write-Host ""; Write-Host "==> $Msg" }
function Run([string]$StepName, [scriptblock]$Cmd) {
    & $Cmd
    if ($LASTEXITCODE -ne 0) { Fail $StepName }
}

$expectedNode = 'v' + (Get-Content '.nvmrc' -Raw).Trim()
$expectedPython = (Get-Content '.python-version' -Raw).Trim()
$pinnedUv = '0.13.6'

# --- 1. prerequisites ---------------------------------------------------
Step "Checking prerequisites"
if (-not (Get-Command git  -ErrorAction SilentlyContinue)) { Fail "git not found. Install: winget install --id Git.Git -e" }
if (-not (Get-Command uv   -ErrorAction SilentlyContinue)) { Fail "uv not found. Install: winget install --id astral-sh.uv -e" }
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { Fail "node not found. Install the pinned Node $expectedNode (see .nvmrc), e.g. with nvm-windows: nvm install $($expectedNode.TrimStart('v'))" }
if (-not (Get-Command npm  -ErrorAction SilentlyContinue)) { Fail "npm not found. It ships with Node - reinstall Node." }
$nodeVersion = (node --version).Trim()
if ($nodeVersion -ne $expectedNode) { Fail "Node $expectedNode required (see .nvmrc), found $nodeVersion. Versions are pinned exactly - install it with nvm-windows: nvm install $($expectedNode.TrimStart('v')) ; nvm use $($expectedNode.TrimStart('v'))" }
$uvVersion = ((uv --version) -replace 'uv ', '').Trim()
if ($uvVersion -ne $pinnedUv) {
    Write-Host "setup: WARNING - uv $uvVersion found, the pinned uv is $pinnedUv (workflows pin it exactly). Continuing; if a sync misbehaves, install uv $pinnedUv first."
}
Write-Host "git $((git --version) -replace 'git version ',''), uv $uvVersion, node $nodeVersion, npm $(npm --version)"

# --- 2. fingerprint: clean-install on any mismatch -----------------------
Step "Checking this machine against the lock fingerprint"
$lockBytes = @()
foreach ($f in 'uv.lock', 'package-lock.json', 'desktop_app\package-lock.json', '.python-version', '.nvmrc') {
    $lockBytes += [System.IO.File]::ReadAllBytes((Join-Path $PWD $f))
}
$sha256 = [System.Security.Cryptography.SHA256]::Create()
$fingerprint = ($sha256.ComputeHash($lockBytes) | ForEach-Object { $_.ToString('x2') }) -join ''
$stampFile = '.aether-setup-stamp'
$stamped = ''
if (Test-Path $stampFile) { $stamped = (Get-Content $stampFile -Raw).Trim() }
if ($fingerprint -ne $stamped -or -not (Test-Path '.venv\Scripts\python.exe') -or -not (Test-Path 'node_modules') -or -not (Test-Path 'desktop_app\node_modules')) {
    Write-Host "Lock fingerprint changed (or first setup) - CLEAN INSTALL: wiping .venv, node_modules, desktop_app\node_modules and any stale desktop_app\engine-payload, then rebuilding exactly from the locks."
    foreach ($d in '.venv', 'node_modules', 'desktop_app\node_modules', 'desktop_app\engine-payload') {
        if (Test-Path $d) { Remove-Item $d -Recurse -Force }
    }
} else {
    Write-Host "Fingerprint matches the last setup - reconciling in place (uv sync --locked / npm ci are exact)."
}

# --- 3. Python venv, at the exact pinned CPython --------------------------
Step "Creating the Python venv ($expectedPython) if needed"
if (-not (Test-Path '.venv\Scripts\python.exe')) {
    Run "uv venv" { uv venv .venv --python $expectedPython }
} else {
    Write-Host ".venv already exists - keeping it; the sync below reconciles it."
}

# --- 4. Engine dependencies, exactly from uv.lock ------------------------
Step "Installing engine dependencies from uv.lock (all extras)"
Run "uv sync --locked --all-extras" { uv sync --locked --all-extras }

# --- 5. Node dependencies, exactly from the package-locks -----------------
Step "Installing repo-root dependencies from package-lock.json (commit tooling)"
Run "npm ci (root)" { npm ci }
Step "Installing desktop dependencies from package-lock.json (inside desktop_app)"
Push-Location desktop_app
Run "npm ci" { npm ci }
Pop-Location

# --- 6. Smoke checks ------------------------------------------------------
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

# --- 7. Stamp the fingerprint this machine now matches ---------------------
Set-Content -Path $stampFile -Value $fingerprint

Write-Host ""
Write-Host "setup: OK - this machine matches the locks exactly."
Write-Host "  Python: $(.venv\Scripts\python.exe --version) (.venv)   Node: $nodeVersion"
Write-Host "  Run Aether with:  scripts\run.ps1    Update later with:  scripts\update.ps1"
