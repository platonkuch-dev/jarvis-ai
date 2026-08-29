# Type/compile-checks both subprojects without producing build artifacts.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
. "$PSScriptRoot\_env.ps1"

Write-Host "==> native-core: cargo check" -ForegroundColor Cyan
Push-Location "$root\native-core"
cargo check
Pop-Location

Write-Host "==> agent-ts: tsc --noEmit" -ForegroundColor Cyan
Push-Location "$root\agent-ts"
npm run typecheck
Pop-Location

Write-Host "Typecheck OK." -ForegroundColor Green
