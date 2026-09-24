@echo off
setlocal
cd /d "%~dp0"
title Jarvis control panel

if not exist ".venv\Scripts\python.exe" (
    echo First run: creating the Python environment and installing packages...
    where python >nul 2>nul
    if errorlevel 1 (
        echo Python was not found. Install Python 3.11 or 3.12 from https://www.python.org/downloads/
        echo and tick "Add python.exe to PATH" in the installer, then run this file again.
        pause
        exit /b 1
    )
    python -m venv .venv
    if errorlevel 1 goto :fail
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto :fail
)

if not exist ".env" copy ".env.example" ".env" >nul

echo Opening the control panel in your browser. Close this window to stop it.
".venv\Scripts\python.exe" panel.py
exit /b 0

:fail
echo.
echo Setup failed - read the messages above, fix the problem and run this file again.
pause
exit /b 1
