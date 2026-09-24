[CmdletBinding()]
param(
    [switch]$Autostart,
    [switch]$Shortcut,
    [switch]$Launch
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root ".venv"
$python = Join-Path $venv "Scripts\python.exe"
$pythonw = Join-Path $venv "Scripts\pythonw.exe"

Write-Host "==> Джарвис: Python-окружение" -ForegroundColor Cyan
if (-not (Test-Path $python)) {
    Write-Host "Виртуальное окружение не найдено -- создаю новое..." -ForegroundColor Cyan
    $systemPython = Get-Command python -ErrorAction SilentlyContinue
    if (-not $systemPython) {
        throw "Python не найден в PATH. Установите Python 3.11 или 3.12: https://www.python.org/downloads/"
    }
    & $systemPython.Source -m venv $venv
    if (-not (Test-Path $python)) {
        throw "Не удалось создать виртуальное окружение в $venv"
    }
}

Write-Host "==> Зависимости" -ForegroundColor Cyan
& $python -m pip install --upgrade pip
& $python -m pip install -r (Join-Path $root "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Не удалось установить зависимости (pip install -r requirements.txt)."
}

if (-not (Test-Path (Join-Path $root ".env"))) {
    Write-Warning "Файл .env не найден -- скопируйте .env.example в .env и заполните ключи API перед запуском."
}

if ($Autostart) {
    Write-Host "==> Автозапуск при входе в Windows" -ForegroundColor Cyan
    & $python (Join-Path $root "install_autostart.py")
}

if ($Shortcut) {
    Write-Host "==> Ярлык на рабочем столе" -ForegroundColor Cyan
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "create_shortcut.ps1")
}

Write-Host ""
Write-Host "Джарвис установлен." -ForegroundColor Green

if ($Launch) {
    Write-Host "Запускаю Джарвис..." -ForegroundColor Cyan
    Start-Process -FilePath $pythonw -ArgumentList "`"$(Join-Path $root 'app.py')`""
}
