# Aether-OS — run from the terminal.
#
# Starts the engine, waits for its /health to answer (120s budget, the
# same one the packaged app's lifecycle allows), then launches the
# desktop app in dev mode. Closing the app stops the engine again.
#
# WARNING: do not use this script when testing the PACKAGED app's own
# engine startup — it pre-starts an engine on :8000, which a packaged
# app would adopt, masking payload/lifecycle faults.
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')

function Fail([string]$Msg) {
    Write-Host "run: FAILED — $Msg" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path '.venv\Scripts\python.exe')) { Fail "no .venv yet — run scripts\setup.ps1 first." }

Write-Host "run: starting the engine on 127.0.0.1:8000 ..."
$engine = Start-Process -FilePath '.venv\Scripts\python.exe' -ArgumentList 'run_engine.py' -PassThru -NoNewWindow
try {
    $deadline = (Get-Date).AddSeconds(120)
    $healthy = $false
    while ((Get-Date) -lt $deadline) {
        if ($engine.HasExited) { Fail "the engine exited during startup (see its output above)." }
        try {
            $resp = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8000/health' -TimeoutSec 2
            if ($resp.StatusCode -eq 200) { $healthy = $true; break }
        } catch {
            Start-Sleep -Seconds 1
        }
    }
    if (-not $healthy) { Fail "the engine did not answer /health within 120s." }
    Write-Host "run: engine healthy — launching the desktop app."
    Set-Location desktop_app
    npm run electron:dev
} finally {
    if (-not $engine.HasExited) {
        Stop-Process -Id $engine.Id -Force -ErrorAction SilentlyContinue
    }
}
