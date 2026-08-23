@echo off
chcp 65001 >nul 2>&1
title Spotify YouTube Tracker
cd /d "%~dp0"

echo ==========================================
echo    Spotify - YouTube Tracker
echo ==========================================

if not exist "data" mkdir data

set "CHANNEL=stable"
call :read_channel

if not exist "data\update_channel.txt" (
    >"data\update_channel.txt" echo # local copy — not overwritten by git pull
    >>"data\update_channel.txt" echo # stable ^| beta
    >>"data\update_channel.txt" echo %CHANNEL%
)
if not exist "update_channel.txt" (
    >"update_channel.txt" echo # MuseNest update channel
    >>"update_channel.txt" echo # stable ^| beta
    >>"update_channel.txt" echo %CHANNEL%
)

where git >nul 2>nul
if errorlevel 1 (
    echo [!] git not found — skip auto-update
    goto :deps
)
if not exist ".git" (
    echo [!] not a git clone — skip auto-update
    goto :deps
)

echo [*] Fetching remotes...
git fetch origin --prune
if errorlevel 1 (
    echo [!] git fetch failed — continue with local files
    goto :deps
)

set "BRANCH=main"
if /i "%CHANNEL%"=="beta" (
    for /f "usebackq delims=" %%B in (`git for-each-ref --sort=-creatordate --format^="%%(refname:short)" refs/remotes/origin`) do (
        if /i not "%%B"=="origin/HEAD" (
            set "BRANCH=%%B"
            goto :got_branch
        )
    )
)
:got_branch
if /i "%BRANCH:~0,7%"=="origin/" set "BRANCH=%BRANCH:~7%"
if "%BRANCH%"=="" set "BRANCH=main"

git show-ref --verify --quiet "refs/remotes/origin/%BRANCH%"
if errorlevel 1 (
    echo [!] origin/%BRANCH% missing — fallback to main
    set "BRANCH=main"
)

echo [*] Channel: %CHANNEL%  -^>  branch: %BRANCH%
git checkout -B "%BRANCH%" "origin/%BRANCH%"
if errorlevel 1 (
    echo [!] checkout failed — continue with current files
    goto :deps
)
git reset --hard "origin/%BRANCH%"
if errorlevel 1 (
    echo [!] reset failed — continue with current files
    goto :deps
)

if not exist "data" mkdir data
>"data\update_channel.txt" echo # local copy — not overwritten by git pull
>>"data\update_channel.txt" echo # stable ^| beta
>>"data\update_channel.txt" echo %CHANNEL%
>"update_channel.txt" echo # MuseNest update channel
>>"update_channel.txt" echo # stable ^| beta
>>"update_channel.txt" echo %CHANNEL%

for /f "delims=" %%H in ('git rev-parse --short HEAD') do echo [*] Updated to %%H on %BRANCH%

if "%MUSE_REEXEC%"=="" (
    set "MUSE_REEXEC=1"
    "%~f0"
    exit /b %ERRORLEVEL%
)

:deps
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
exit /b 0

:read_channel
set "SRC="
if exist "data\update_channel.txt" set "SRC=data\update_channel.txt"
if not defined SRC if exist "update_channel.txt" set "SRC=update_channel.txt"
if not defined SRC goto :eof
set "CHANNEL=stable"
for /f "usebackq tokens=* eol=#" %%L in ("%SRC%") do (
    if not "%%L"=="" set "LINE=%%L"
)
if /i "%LINE%"=="beta" set "CHANNEL=beta"
if /i "%LINE%"=="stable" set "CHANNEL=stable"
goto :eof
