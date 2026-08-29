# Dot-sourced by every script in this folder. Two jobs:
#
# 1. Make sure cargo/node are actually reachable. A terminal opened before
#    the winget-installed PATH entries were registered (or a CI runner with
#    a minimal PATH) won't see them even though the tools are on disk --
#    this appends the well-known install locations defensively rather than
#    failing with a confusing "cargo is not recognized" error.
# 2. Load the MSVC build environment (link.exe, Windows SDK) so `cargo
#    build`/`cargo check`/`cargo clippy` can find a linker.
#
# Safe to source multiple times.

$cargoBin = Join-Path $env:USERPROFILE ".cargo\bin"
if ((Test-Path $cargoBin) -and ($env:Path -notlike "*$cargoBin*")) {
    $env:Path = "$cargoBin;$env:Path"
}
$nodeDir = "C:\Program Files\nodejs"
if ((Test-Path $nodeDir) -and ($env:Path -notlike "*$nodeDir*")) {
    $env:Path = "$nodeDir;$env:Path"
}

if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
    Write-Warning "cargo not found even after checking the default rustup install location. Install it: winget install Rustlang.Rustup"
}
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Write-Warning "node not found even after checking the default install location. Install it: winget install OpenJS.NodeJS.LTS"
}

$vcvarsall = Get-ChildItem "C:\Program Files*\Microsoft Visual Studio\*\*\VC\Auxiliary\Build\vcvarsall.bat" -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $vcvarsall) {
    Write-Warning "vcvarsall.bat not found - install the MSVC C++ Build Tools (see readme.md Troubleshooting)."
    return
}
# Built as a plain string first (rather than an interpolated string with
# escaped quotes) - much less fragile across PowerShell versions/hosts.
$cmdLine = '"' + $vcvarsall.FullName + '" x64 >nul && set'
$envDump = cmd /c $cmdLine
foreach ($line in $envDump -split "`n") {
    if ($line -match "^([^=]+)=(.*)$") {
        [System.Environment]::SetEnvironmentVariable($matches[1], $matches[2].TrimEnd("`r"))
    }
}
