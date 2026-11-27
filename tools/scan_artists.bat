@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

rem ---------------------------------------------------------------------
rem  MuseNest artist scanner
rem  Put this file (and scan_artists.py) into your music folder and
rem  double-click it. It walks every subfolder, works out the artist of
rem  each track and writes the unique names to artists.txt right here.
rem
rem  Needs Python 3 - MuseNest itself already requires it.
rem ---------------------------------------------------------------------

if not exist "%~dp0scan_artists.py" (
    echo [ERROR] scan_artists.py is missing.
    echo         Copy BOTH files - scan_artists.bat and scan_artists.py -
    echo         into the folder you want to scan.
    goto :end
)

where python >nul 2>&1
if not errorlevel 1 (
    python "%~dp0scan_artists.py" "%~dp0"
    goto :end
)

where py >nul 2>&1
if not errorlevel 1 (
    py -3 "%~dp0scan_artists.py" "%~dp0"
    goto :end
)

echo [ERROR] Python 3 was not found in PATH.
echo         Install it from python.org, or run it from MuseNest's venv:
echo             python scan_artists.py "%~dp0"

:end
echo.
pause
