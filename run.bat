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

python -c "import flask, requests, yt_dlp" 2>nul
if errorlevel 1 (
    echo [*] Installing packages...
    python -m pip install -q --user -r requirements.txt
)

echo [*] Starting...
python app.py
pause
