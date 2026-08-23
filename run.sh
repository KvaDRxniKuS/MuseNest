#!/usr/bin/env bash
cd "$(dirname "$0")"

echo "=========================================="
echo "   Spotify - YouTube Tracker"
echo "=========================================="

CHANNEL_FILE="update_channel.txt"
CHANNEL_LOCAL="data/update_channel.txt"

_read_channel() {
    local src=""
    if [ -f "$CHANNEL_LOCAL" ]; then
        src="$CHANNEL_LOCAL"
    elif [ -f "$CHANNEL_FILE" ]; then
        src="$CHANNEL_FILE"
    else
        echo "stable"
        return
    fi
    local val
    val=$(grep -vE '^\s*#' "$src" | grep -vE '^\s*$' | tail -n 1 | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')
    if [ "$val" != "beta" ] && [ "$val" != "stable" ]; then
        val="stable"
    fi
    echo "$val"
}

_ensure_channel_files() {
    mkdir -p data
    local ch
    ch="$(_read_channel)"
    if [ ! -f "$CHANNEL_LOCAL" ]; then
        printf '# local copy — not overwritten by git pull\n# stable | beta\n%s\n' "$ch" > "$CHANNEL_LOCAL"
    fi
    if [ ! -f "$CHANNEL_FILE" ]; then
        printf '# MuseNest update channel\n# stable | beta\n%s\n' "$ch" > "$CHANNEL_FILE"
    fi
}

_newest_remote_branch() {
    git for-each-ref --sort=-creatordate --format='%(refname:short)' refs/remotes/origin \
        | grep -vE '^origin/HEAD$' \
        | head -n 1 \
        | sed 's#^origin/##'
}

_auto_update() {
    if ! command -v git &>/dev/null; then
        echo "[!] git not found — skip auto-update"
        return 0
    fi
    if [ ! -d .git ]; then
        echo "[!] not a git clone — skip auto-update"
        return 0
    fi

    _ensure_channel_files
    local channel
    channel="$(_read_channel)"
    # Keep user choice across hard reset / checkout
    local saved_channel="$channel"

    echo "[*] Fetching remotes..."
    if ! git fetch origin --prune; then
        echo "[!] git fetch failed — continue with local files"
        return 0
    fi

    local branch="main"
    if [ "$channel" = "beta" ]; then
        branch="$(_newest_remote_branch)"
        if [ -z "$branch" ]; then
            echo "[!] no remote branches — fallback to main"
            branch="main"
        fi
    fi

    if ! git show-ref --verify --quiet "refs/remotes/origin/${branch}"; then
        echo "[!] origin/${branch} missing — fallback to main"
        branch="main"
    fi

    echo "[*] Channel: ${channel}  →  branch: ${branch}"
    if ! git checkout -B "$branch" "origin/${branch}"; then
        echo "[!] checkout failed — continue with current files"
        return 0
    fi
    if ! git reset --hard "origin/${branch}"; then
        echo "[!] reset failed — continue with current files"
        return 0
    fi

    mkdir -p data
    printf '# local copy — not overwritten by git pull\n# stable | beta\n%s\n' "$saved_channel" > "$CHANNEL_LOCAL"
    # Restore root switcher if the pulled branch has no file or overwrote it
    if [ ! -f "$CHANNEL_FILE" ] || ! grep -qE "^${saved_channel}$" "$CHANNEL_FILE" 2>/dev/null; then
        if [ -f "$CHANNEL_FILE" ]; then
            # rewrite only the last non-comment value, keep comments if possible
            :
        fi
        printf '# MuseNest update channel\n# stable | beta\n%s\n' "$saved_channel" > "$CHANNEL_FILE"
    fi

    echo "[*] Updated to $(git rev-parse --short HEAD) on ${branch}"
}

_auto_update

if [ -z "${MUSE_REEXEC:-}" ]; then
    export MUSE_REEXEC=1
    exec bash "$0" "$@"
fi

if ! command -v python3 &>/dev/null; then
    echo "[!] Python3 not found."
    exit 1
fi

python3 -c "import flask, requests, yt_dlp" 2>/dev/null || {
    echo "[*] Installing packages..."
    python3 -m pip install -q --user -r requirements.txt
}

echo "[*] Starting..."
exec python3 app.py
