@echo off
chcp 65001 >nul 2>&1
title MuseNest
cd /d "%~dp0"

echo ==========================================
echo    MuseNest
echo ==========================================

where python >nul 2>nul
if errorlevel 1 (
    echo [!] Python not found. Install from python.org
    pause
    exit /b 1
)

echo [*] Updating from GitHub...
python update_from_git.py
echo [*] Update step finished.

if "%MUSE_REEXEC%"=="" (
    set "MUSE_REEXEC=1"
    call "%~f0"
    exit /b %ERRORLEVEL%
)

python -c "import flask, requests, yt_dlp" 2>nul
if errorlevel 1 (
    echo [*] Installing packages...
    python -m pip install -q --user -r requirements.txt
)

echo [*] Starting...
python app.py
pause
exit /b 0
