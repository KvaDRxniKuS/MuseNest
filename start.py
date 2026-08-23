#!/usr/bin/env python3
"""
MuseNest launcher (Windows / macOS / Linux).

Usage:
    python start.py
    run.bat / run.sh   (only start this file)

1. Reads update_channel.txt  (stable = main, beta = newest branch)
2. Downloads the branch zip from GitHub (no git required)
3. Overwrites app files; keeps user data/ settings
4. If start.py itself changed, restarts
5. Installs Python deps if missing
6. Starts app.py
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPO_OWNER = "KvaDRxniKuS"
REPO_NAME = "MuseNest"
DEFAULT_BRANCH = "main"
UA = "MuseNest-launcher"

HERE = os.path.dirname(os.path.abspath(__file__))
CHANNEL_FILE = os.path.join(HERE, "update_channel.txt")
SKIP_PREFIXES = (
    "data/config.json",
    "data/library.json",
    "data/tracks.db",
    "data/artists.txt",
    "data/blacklist.txt",
    "data/save_folder.txt",
    "data/cookies.txt",
    "data/update_channel.txt",
    "downloads/",
)
SKIP_NAMES = {".git", "__pycache__", ".gitignore"}


def log(msg: str) -> None:
    print(msg, flush=True)


def http_get(url: str, timeout: int = 60) -> bytes:
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    with urlopen(req, timeout=timeout) as r:
        return r.read()


def parse_channel(text: str) -> str:
    val = "stable"
    for line in text.splitlines():
        s = line.strip().lstrip("\ufeff")
        if not s or s.startswith("#"):
            continue
        val = s.lower()
    if val in ("beta", "dev", "nightly"):
        return "beta"
    return "stable"


def read_channel() -> str:
    if not os.path.isfile(CHANNEL_FILE):
        with open(CHANNEL_FILE, "w", encoding="utf-8") as f:
            f.write(
                "# MuseNest update channel\n"
                "# stable  - only main\n"
                "# beta    - newest GitHub branch\n"
                "stable\n"
            )
        return "stable"
    with open(CHANNEL_FILE, "r", encoding="utf-8-sig") as f:
        return parse_channel(f.read())


def newest_branch() -> str:
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/branches?per_page=100"
    items = json.loads(http_get(url).decode("utf-8"))
    best_name = DEFAULT_BRANCH
    best_dt = datetime.min
    for item in items:
        name = item.get("name") or ""
        date_s = (
            ((item.get("commit") or {}).get("commit") or {}).get("committer") or {}
        ).get("date") or ""
        try:
            dt = datetime.fromisoformat(date_s.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            dt = datetime.min
        log(f"    branch {name}  last commit {date_s or '?'}")
        if dt >= best_dt and name:
            best_dt = dt
            best_name = name
    return best_name or DEFAULT_BRANCH


def should_skip(rel: str) -> bool:
    norm = rel.replace("\\", "/").lstrip("./")
    base = os.path.basename(norm)
    if base in SKIP_NAMES or base.endswith(".pyc"):
        return True
    for p in SKIP_PREFIXES:
        if norm == p or norm.startswith(p):
            return True
    return False


def file_digest(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return b""


def apply_zip(blob: bytes) -> bool:
    """Extract zip over HERE. Returns True if start.py changed."""
    zf = zipfile.ZipFile(io.BytesIO(blob))
    names = zf.namelist()
    if not names:
        raise RuntimeError("empty zip")
    root_prefix = names[0].split("/")[0] + "/"
    start_before = file_digest(os.path.join(HERE, "start.py"))
    updated = 0
    created = 0
    for info in zf.infolist():
        name = info.filename
        if not name.startswith(root_prefix):
            continue
        rel = name[len(root_prefix) :]
        if not rel or rel.endswith("/"):
            continue
        if should_skip(rel):
            continue
        dest = os.path.join(HERE, *rel.split("/"))
        data = zf.read(info)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.isfile(dest) and file_digest(dest) == data:
            continue
        existed = os.path.isfile(dest)
        with open(dest, "wb") as f:
            f.write(data)
        if existed:
            updated += 1
            log(f"    updated {rel}")
        else:
            created += 1
            log(f"    downloaded {rel}")
    log(f"[OK] files updated: {updated}, new: {created}")
    start_after = file_digest(os.path.join(HERE, "start.py"))
    return start_before != start_after


def ensure_deps() -> None:
    need = ("flask", "requests", "yt_dlp")
    missing = []
    for mod in need:
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    req = os.path.join(HERE, "requirements.txt")
    if missing:
        log(f"[*] Installing packages: {', '.join(missing)}")
        cmd = [sys.executable, "-m", "pip", "install", "--user"]
        if os.path.isfile(req):
            cmd += ["-r", req]
        else:
            cmd += ["flask>=3.0", "requests>=2.31", "yt-dlp>=2024.1.0"]
        r = subprocess.run(cmd)
        if r.returncode != 0:
            log("[!] pip install failed — try: python -m pip install -r requirements.txt")
    else:
        log("[OK] Python packages present")

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        log(f"[OK] ffmpeg: {ffmpeg}")
    else:
        log("[!] ffmpeg not found — MP3 conversion will fail until it is on PATH")


def auto_update() -> None:
    channel = read_channel()
    log(f"[*] Channel: {channel}  ({CHANNEL_FILE})")
    try:
        if channel == "beta":
            log("[*] Looking up newest branch...")
            branch = newest_branch()
        else:
            branch = DEFAULT_BRANCH
    except (HTTPError, URLError, json.JSONDecodeError) as e:
        log(f"[!] Cannot list branches ({e}) — skip update")
        return

    zip_url = f"https://codeload.github.com/{REPO_OWNER}/{REPO_NAME}/zip/refs/heads/{branch}"
    log(f"[*] Downloading {REPO_OWNER}/{REPO_NAME}@{branch}")
    try:
        blob = http_get(zip_url, timeout=120)
    except (HTTPError, URLError) as e:
        log(f"[!] Download failed ({e}) — using local files")
        return

    try:
        self_changed = apply_zip(blob)
    except Exception as e:
        log(f"[!] Extract failed ({e}) — using local files")
        return

    if self_changed:
        log("[*] start.py updated — restarting...")
        os.execv(sys.executable, [sys.executable, os.path.join(HERE, "start.py")])


def start_app() -> None:
    app = os.path.join(HERE, "app.py")
    if not os.path.isfile(app):
        log("[!] app.py missing after update")
        sys.exit(1)
    log("[*] Starting MuseNest...")
    os.execv(sys.executable, [sys.executable, app])


def main() -> None:
    os.chdir(HERE)
    log("MuseNest launcher")
    log("-----------------")
    log(f"Folder: {HERE}")
    log(f"Python: {sys.version.split()[0]}  ({sys.executable})")
    log("")
    auto_update()
    log("")
    ensure_deps()
    log("")
    start_app()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)
