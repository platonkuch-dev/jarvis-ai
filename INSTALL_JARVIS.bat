@echo off
setlocal
cd /d "%~dp0"

if /I "%~1"=="/?" goto :help
if /I "%~1"=="-h" goto :help
if /I "%~1"=="--help" goto :help

echo.
echo MARK XLVIII installer
echo This installs Python dependencies, browser automation, local voice tools,
echo enables autostart for the current user, and starts JARVIS.
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" -Autostart -Launch
if errorlevel 1 (
    echo.
    echo Installation failed. Read the message above and run this file again after fixing it.
    pause
    exit /b 1
)

echo.
echo Installation completed. JARVIS is starting in a separate window.
pause
exit /b 0

:help
echo Usage: Double-click INSTALL_JARVIS.bat to install and start MARK XLVIII.
echo.
echo Advanced options are available through scripts\install.ps1:
echo   -SkipOptional          Install only required Python packages.
echo   -IncludeExperimental   Also install the unused Rust/TypeScript prototype.
exit /b 0