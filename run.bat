@echo off
chcp 65001 >nul 2>&1
title Spotify YouTube Tracker
cd /d "%~dp0"

echo ==========================================
echo    Spotify - YouTube Tracker
echo ==========================================

where python >nul 2>nul
if errorlevel 1 (
    echo [!] Python not found. Install from python.org
    pause
    exit /b 1
)

python update_from_git.py

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
