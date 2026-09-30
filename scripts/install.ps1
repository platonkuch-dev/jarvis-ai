[CmdletBinding()]
param(
	[switch]$Autostart,
	[switch]$IncludeExperimental,
	[switch]$SkipOptional,
	[switch]$Launch
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root ".venv"
$python = Join-Path $venv "Scripts\python.exe"

function New-JarvisVenv {
	if (Get-Command py -ErrorAction SilentlyContinue) {
		foreach ($version in @("3.12", "3.11")) {
			& py "-$version" -c "import sys" 2>$null
			if ($LASTEXITCODE -eq 0) {
				& py "-$version" -m venv $venv
				return
			}
		}
	}

	$systemPython = Get-Command python -ErrorAction SilentlyContinue
	if ($systemPython) {
		& $systemPython.Source -c "import sys; raise SystemExit(0 if sys.version_info[:2] in ((3, 11), (3, 12)) else 1)"
		if ($LASTEXITCODE -eq 0) {
			& $systemPython.Source -m venv $venv
			return
		}
	}

	throw "Python 3.11 or 3.12 is required. Install it from https://www.python.org/downloads/"
}

Write-Host "==> MARK XLVIII Python environment" -ForegroundColor Cyan
if (-not (Test-Path $python)) {
	New-JarvisVenv
}

& $python -m pip install --upgrade pip
& $python -m pip install -r (Join-Path $root "requirements.txt")
& $python -m playwright install chromium

if (-not $SkipOptional) {
	Write-Host "==> Optional JARVIS integrations" -ForegroundColor Cyan
	& $python -m pip install 'fastapi' 'uvicorn[standard]' 'cryptography' 'telethon' 'faster-whisper' 'openwakeword' 'transformers'
	Write-Host "==> Local Fast Path runtime" -ForegroundColor Cyan
	& $python -m pip install torch --index-url https://download.pytorch.org/whl/cu128
}

if ($Autostart) {
	Write-Host "==> Registering Windows autostart" -ForegroundColor Cyan
	& $python (Join-Path $root "main.py") --install-autostart
}

if ($IncludeExperimental) {
	. "$PSScriptRoot\_env.ps1"
	if (Get-Command cargo -ErrorAction SilentlyContinue) {
		Write-Host "==> native-core: fetching Cargo dependencies" -ForegroundColor Cyan
		Push-Location "$root\native-core"
		cargo fetch
		Pop-Location
	} else {
		Write-Warning "Skipping native-core: Rust/Cargo is not installed."
	}

	if (Get-Command npm -ErrorAction SilentlyContinue) {
		Write-Host "==> agent-ts: npm install" -ForegroundColor Cyan
		Push-Location "$root\agent-ts"
		npm install
		Pop-Location
	} else {
		Write-Warning "Skipping agent-ts: Node.js/npm is not installed."
	}
}

Write-Host "" 
Write-Host "MARK XLVIII is installed." -ForegroundColor Green
Write-Host "Start it with: $python $root\main.py"

if ($Launch) {
	Write-Host "==> Starting JARVIS" -ForegroundColor Cyan
	Start-Process -FilePath $python -ArgumentList (Join-Path $root "main.py") -WorkingDirectory $root
}
