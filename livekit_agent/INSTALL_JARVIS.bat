@echo off
setlocal
cd /d "%~dp0"

echo.
echo Jarvis voice assistant setup
echo This creates a virtual environment if needed, installs Python
echo dependencies, enables autostart on Windows sign-in, and adds a
echo "Jarvis" shortcut to the Desktop.
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" -Autostart -Shortcut
if errorlevel 1 (
    echo.
    echo Setup failed. Read the message above, fix it, and run this file again.
    pause
    exit /b 1
)

echo.
if not exist ".env" copy ".env.example" ".env" >nul
echo Setup complete. Opening the control panel: enter your API keys there, then press "Start".
".venv\Scripts\python.exe" panel.py
exit /b 0
