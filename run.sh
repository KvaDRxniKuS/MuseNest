#!/usr/bin/env bash
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
    exec python3 start.py
fi
if command -v python >/dev/null 2>&1; then
    exec python start.py
fi
echo "[!] Python not found."
exit 1
