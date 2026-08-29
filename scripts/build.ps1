# Builds native-core (release) and typechecks agent-ts (no separate JS build -
# Node 22.6+ runs the TypeScript source directly at runtime).
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
. "$PSScriptRoot\_env.ps1"

Write-Host "==> native-core: cargo build --release" -ForegroundColor Cyan
Push-Location "$root\native-core"
cargo build --release
Pop-Location

Write-Host "==> agent-ts: typecheck (tsc --noEmit)" -ForegroundColor Cyan
Push-Location "$root\agent-ts"
npm run typecheck
Pop-Location

Write-Host "Build OK." -ForegroundColor Green
