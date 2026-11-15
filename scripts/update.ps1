# Aether-OS - update this machine: pull the latest source, then
# reconcile the machine to the locks.
#
# This is THE way a source checkout stays current: `git pull
# --ff-only` brings new code AND new lockfiles/pins, and setup.ps1
# then rebuilds the environments from them - clean-installing
# (.venv, node_modules, engine-payload wiped and rebuilt) whenever
# the lock fingerprint moved, per the clean-install rule in
# setup.ps1's header. User data (~/.aether) is never touched.
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')

Write-Host "==> Pulling the latest source (fast-forward only)"
git pull --ff-only
if ($LASTEXITCODE -ne 0) { Write-Host "update: FAILED - git pull" -ForegroundColor Red; exit 1 }

& (Join-Path $PSScriptRoot 'setup.ps1')
