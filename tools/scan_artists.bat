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

set "ROOT=%~dp0"

echo ==============================================
echo   MuseNest artist scanner
echo ==============================================
echo.
echo Folder : %ROOT%
echo.

if not exist "%~dp0scan_artists.py" (
    echo [ERROR] scan_artists.py is missing.
    echo         Copy BOTH files - scan_artists.bat and scan_artists.py -
    echo         into the folder you want to scan.
    goto :end
)

echo Looking for Python...
set "PY="
python -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=python"
if not defined PY (
    py -3 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
    echo [ERROR] Python 3 is missing or does not run.
    echo         Install it from python.org and tick "Add python.exe to PATH",
    echo         or start MuseNest's venv and run:
    echo             python scan_artists.py "%ROOT%."
    goto :end
)
echo Found  : %PY%
echo.
echo Working. One line appears per scanned folder - the window is not stuck.
echo.

%PY% "%~dp0scan_artists.py" "%ROOT%."
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo [ERROR] the scanner stopped with code %RC%.
    goto :end
)
if exist "%~dp0artists.txt" (
    echo Done. The list is here:
    echo   %~dp0artists.txt
) else (
    echo [WARN] the scanner finished but artists.txt was not created.
)

:end
echo.
pause
