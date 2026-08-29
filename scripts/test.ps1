# Runs the real (non-mocked, live-Windows) test suites for both
# subprojects. native-core must already be built (scripts/build.ps1) since
# agent-ts's integration tests spawn the compiled binary.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
. "$PSScriptRoot\_env.ps1"

Write-Host "==> native-core: cargo test" -ForegroundColor Cyan
Push-Location "$root\native-core"
cargo test -- --test-threads=1
Pop-Location

Write-Host "==> agent-ts: npm test" -ForegroundColor Cyan
Push-Location "$root\agent-ts"
npm test
Pop-Location

Write-Host "All tests passed." -ForegroundColor Green
