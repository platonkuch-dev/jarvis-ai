# cargo clippy for native-core; agent-ts has no separate linter configured
# yet (tsc --noEmit's strict mode catches most of what matters for now).
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
. "$PSScriptRoot\_env.ps1"

Write-Host "==> native-core: cargo clippy" -ForegroundColor Cyan
Push-Location "$root\native-core"
cargo clippy --all-targets -- -D warnings
Pop-Location

Write-Host "==> agent-ts: tsc --noEmit (strict mode)" -ForegroundColor Cyan
Push-Location "$root\agent-ts"
npm run typecheck
Pop-Location

Write-Host "Lint OK." -ForegroundColor Green
