#!/usr/bin/env bash
cd "$(dirname "$0")"

echo "=========================================="
echo "   Spotify - YouTube Tracker"
echo "=========================================="

if ! command -v python3 &>/dev/null; then
    echo "[!] Python3 not found."
    exit 1
fi

python3 update_from_git.py || true

if [ -z "${MUSE_REEXEC:-}" ]; then
    export MUSE_REEXEC=1
    exec bash "$0" "$@"
fi

python3 -c "import flask, requests, yt_dlp" 2>/dev/null || {
    echo "[*] Installing packages..."
    python3 -m pip install -q --user -r requirements.txt
}

echo "[*] Starting..."
exec python3 app.py
