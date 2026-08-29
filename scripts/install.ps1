# Installs dependencies for both new-architecture subprojects (native-core, agent-ts).
# Does NOT touch the Python side - use `pip install -r requirements.txt` for that, as before.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
. "$PSScriptRoot\_env.ps1"

Write-Host "==> native-core: fetching Cargo dependencies" -ForegroundColor Cyan
Push-Location "$root\native-core"
cargo fetch
Pop-Location

Write-Host "==> agent-ts: npm install" -ForegroundColor Cyan
Push-Location "$root\agent-ts"
npm install
Pop-Location

Write-Host "Done." -ForegroundColor Green
