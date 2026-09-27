@echo off
chcp 65001 >nul 2>&1
setlocal

:: ==========================================
:: ChatGPT Auto-Registration - One-click Setup
:: ==========================================

cd /d "%~dp0"

echo ==========================================
echo   ChatGPT Auto-Registration - Setup
echo ==========================================
echo.

:: ---------- 1. Locate Python ----------
set "PYTHON="
where py >nul 2>&1 && for /f "delims=" %%i in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "PYTHON=%%i"
if "%PYTHON%"=="" (
    if exist "C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python312\python.exe" (
        set "PYTHON=C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python312\python.exe"
    )
)
if "%PYTHON%"=="" where python >nul 2>&1 && set "PYTHON=python"
if "%PYTHON%"=="" (
    echo [ERROR] Python not found.
    echo Please install Python 3.12 or newer from https://www.python.org/downloads/
    echo IMPORTANT: check "Add python.exe to PATH" during installation.
    pause
    exit /b 1
)
echo [1/4] Python found: %PYTHON%
echo.

:: ---------- 2. Create virtual environment ----------
if not exist ".venv\Scripts\python.exe" (
    echo [2/4] Creating virtual environment...
    "%PYTHON%" -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
) else (
    echo [2/4] Virtual environment already exists.
)
echo.

:: ---------- 3. Install dependencies ----------
echo [3/4] Installing dependencies (first run may take a few minutes)...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul 2>&1
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Dependency installation failed. Check your network and try again.
    pause
    exit /b 1
)
echo.

:: ---------- 4. Runtime dependency self-check ----------
echo [4/5] Checking runtime dependencies (greenlet / playwright / VC++ runtime)...
".venv\Scripts\python.exe" -m gpt_automation --check-dependencies
if errorlevel 1 (
    echo [ERROR] Runtime dependency check failed.
    echo        The most common cause is a missing VC++ Redistributable 2015-2022.
    echo        Install it from: https://aka.ms/vs/17/release/vc_redist.x64.exe
    echo        Then re-run this installer, or run:
    echo            python -m pip install --only-binary=:all: --force-reinstall greenlet
    pause
    exit /b 1
)
echo.

:: ---------- 5. CloakBrowser preflight (auto-downloads browser binary) ----------
echo [5/5] Checking CloakBrowser (downloads Chromium binary if missing)...
".venv\Scripts\python.exe" -m gpt_automation --check-cloakbrowser
if errorlevel 1 (
    echo [ERROR] CloakBrowser preflight failed.
    pause
    exit /b 1
)
echo.

:: ---------- 5. Prepare configuration files ----------
if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo [INFO] Created .env from .env.example.
    echo        Please ask your technician to fill in the network / IP rotation settings.
) else (
    echo [INFO] .env already exists.
)
if not exist "input" mkdir "input"
echo.

echo ==========================================
echo   Setup finished.
echo ==========================================
echo.
echo Next steps:
echo   1. Ask your technician to configure ".env" (iKuai / proxy / IP rotation).
echo   2. Put mailbox and card files into the "input" folder.
echo   3. Double-click start.bat to begin.
echo.
pause
