#!/usr/bin/env python3
"""Pull the selected update channel before launch."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
CHANNEL_ROOT = os.path.join(ROOT, "update_channel.txt")
CHANNEL_LOCAL = os.path.join(ROOT, "data", "update_channel.txt")
REPO_URL = "https://github.com/KvaDRxniKuS/MuseNest.git"
KEEP_PREFIXES = ("data/", "data\\", "downloads/", "downloads\\")


def _run(args):
    print("    $ " + " ".join(args))
    r = subprocess.run(
        args,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    out = (r.stdout or "").strip()
    if out:
        for line in out.splitlines():
            print("      " + line)
    return r


def _have_git():
    try:
        r = _run(["git", "--version"])
        return r.returncode == 0
    except FileNotFoundError:
        return False


def _parse_channel(text: str) -> str:
    val = ""
    for line in text.splitlines():
        s = line.strip().lstrip("\ufeff")
        if not s or s.startswith("#"):
            continue
        val = s.lower()
    if val in ("beta", "dev", "nightly"):
        return "beta"
    if val in ("stable", "main", "release"):
        return "stable"
    return "stable"


def read_channel() -> str:
    # Root switcher wins: that is the file the user edits.
    for path in (CHANNEL_ROOT, CHANNEL_LOCAL):
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8-sig") as f:
                    ch = _parse_channel(f.read())
                print(f"[*] Channel file: {path} -> {ch}")
                return ch
            except OSError as e:
                print(f"[!] cannot read {path}: {e}")
    print("[*] No channel file, default stable")
    return "stable"


def write_channel(channel: str) -> None:
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    body = (
        "# MuseNest update channel\n"
        "# stable  - only main\n"
        "# beta    - newest remote branch\n"
        f"{channel}\n"
    )
    for path in (CHANNEL_ROOT, CHANNEL_LOCAL):
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
        except OSError as e:
            print(f"[!] cannot write {path}: {e}")


def newest_remote_branch() -> str:
    r = _run(
        [
            "git",
            "for-each-ref",
            "--sort=-committerdate",
            "--format=%(committerdate:iso) %(refname:short)",
            "refs/remotes/origin",
        ]
    )
    if r.returncode != 0:
        return "main"
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        name = line.split()[-1]
        if name in ("origin", "origin/HEAD"):
            continue
        if name.startswith("origin/"):
            name = name[len("origin/") :]
        if name:
            print(f"[*] Newest remote branch by last commit: {name}")
            return name
    return "main"


def remote_exists(branch: str) -> bool:
    r = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}"],
        cwd=ROOT,
    )
    return r.returncode == 0


def _remote_url() -> str:
    r = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if r.returncode == 0:
        return (r.stdout or "").strip()
    return ""


def ensure_repo() -> bool:
    git_dir = os.path.join(ROOT, ".git")
    if not os.path.isdir(git_dir):
        print("[*] No .git — init and link GitHub")
        init = _run(["git", "init", "-b", "main"])
        if init.returncode != 0:
            init = _run(["git", "init"])
        if init.returncode != 0:
            print("[!] git init failed")
            return False

    url = _remote_url()
    if not url:
        add = _run(["git", "remote", "add", "origin", REPO_URL])
        if add.returncode != 0:
            _run(["git", "remote", "set-url", "origin", REPO_URL])
    elif "KvaDRxniKuS/MuseNest" not in url.replace("\\", "/"):
        print(f"[*] origin was {url} — set to official repo")
        _run(["git", "remote", "set-url", "origin", REPO_URL])
    else:
        print(f"[*] origin: {url}")
    return True


def _clear_tracked_conflicts(branch: str) -> None:
    """Remove local files that would block checkout, keep data/ and downloads/."""
    r = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", f"origin/{branch}"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if r.returncode != 0:
        return
    removed = 0
    for rel in (r.stdout or "").splitlines():
        rel = rel.strip().replace("/", os.sep)
        if not rel:
            continue
        norm = rel.replace("\\", "/")
        if norm.startswith("data/") or norm == "data":
            continue
        if norm.startswith("downloads/") or norm == "downloads":
            continue
        path = os.path.join(ROOT, rel)
        if os.path.isfile(path):
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
    if removed:
        print(f"[*] Cleared {removed} local files that blocked checkout")


def force_sync(branch: str) -> bool:
    _run(["git", "checkout", "-f", "-B", branch, f"origin/{branch}"])
    rst = _run(["git", "reset", "--hard", f"origin/{branch}"])
    if rst.returncode == 0:
        return True
    print("[*] Hard reset blocked — clearing conflicting files and retrying")
    _clear_tracked_conflicts(branch)
    rst = _run(["git", "reset", "--hard", f"origin/{branch}"])
    if rst.returncode == 0:
        return True
    co = _run(["git", "checkout", "-f", "-B", branch, f"origin/{branch}"])
    return co.returncode == 0


def update() -> int:
    print("[*] Auto-update...")
    print(f"[*] Folder: {ROOT}")
    if not _have_git():
        print("[!] Git is not installed: https://git-scm.com/download/win")
        return 0
    if not ensure_repo():
        return 0

    channel = read_channel()
    write_channel(channel)

    fetch = _run(["git", "fetch", "origin", "--prune", "--tags"])
    if fetch.returncode != 0:
        print("[!] git fetch failed — stay on local files")
        return 0

    branch = "main"
    if channel == "beta":
        branch = newest_remote_branch()
    if not remote_exists(branch):
        print(f"[!] origin/{branch} missing — fallback to main")
        branch = "main"
        if not remote_exists(branch):
            print("[!] origin/main missing")
            return 0

    print(f"[*] Sync channel={channel} branch={branch}")
    if not force_sync(branch):
        print("[!] could not switch branch — stay on local files")
        return 0

    write_channel(channel)
    head = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        text=True,
    )
    name = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        text=True,
    )
    print(
        f"[*] Now on {(name.stdout or '').strip()} "
        f"@ {(head.stdout or '').strip()}"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(update())
    except Exception as e:
        print(f"[!] auto-update error: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(0)
