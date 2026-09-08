@echo off
chcp 65001 >nul 2>&1
title MuseNest
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [!] Python not found. Install from python.org
    pause
    exit /b 1
)

python start.py
echo.
pause
exit /b %ERRORLEVEL%
