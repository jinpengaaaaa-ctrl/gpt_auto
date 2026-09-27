@echo off
chcp 65001 >nul 2>&1

:: ==========================================
:: ChatGPT Auto-Registration Launcher
:: ==========================================
:: This file is pure ASCII to avoid Windows
:: CMD codepage issues. The interactive menu
:: is handled by start_launcher.py (Python).
:: ==========================================

:: Auto-detect Python
set "PYTHON="
if exist "%~dp0.venv\Scripts\python.exe" set "PYTHON=%~dp0.venv\Scripts\python.exe"
if "%PYTHON%"=="" (
    if exist "C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python312\python.exe" (
        set "PYTHON=C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python312\python.exe"
    )
)
if "%PYTHON%"=="" where python >nul 2>&1 && set "PYTHON=python"
if "%PYTHON%"=="" (
    echo [ERROR] Python not found. Please install Python 3.12+.
    pause
    exit /b 1
)

:: Change to project root directory
cd /d "%~dp0"

:: Launch the Python interactive launcher
:: The launcher asks for the browser backend and runs its preflight before
:: starting the batch.
"%PYTHON%" -u start_launcher.py
