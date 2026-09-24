$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
$appPy = Join-Path $root "app.py"
$iconPath = Join-Path $root "assets\jarvis.ico"
$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop "Джарвис.lnk"

if (-not (Test-Path $pythonw)) {
    throw "Не найден $pythonw. Сначала запустите INSTALL_JARVIS.bat (создаёт виртуальное окружение и зависимости)."
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = "`"$appPy`""
$shortcut.WorkingDirectory = $root
if (Test-Path $iconPath) {
    $shortcut.IconLocation = "$iconPath,0"
}
$shortcut.Description = "Запустить голосового ассистента Джарвис"
$shortcut.Save()

Write-Host "Ярлык создан: $shortcutPath" -ForegroundColor Green
