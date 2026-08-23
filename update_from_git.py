#!/usr/bin/env python3
"""Pull the selected update channel before launch.

Channel file: update_channel.txt (root) or data/update_channel.txt
  stable — origin/main
  beta   — newest remote branch by creatordate
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
CHANNEL_ROOT = os.path.join(ROOT, "update_channel.txt")
CHANNEL_LOCAL = os.path.join(ROOT, "data", "update_channel.txt")


def _run(args, check=False):
    return subprocess.run(
        args,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _have_git():
    try:
        r = _run(["git", "--version"])
        return r.returncode == 0
    except FileNotFoundError:
        return False


def _parse_channel(text: str) -> str:
    val = "stable"
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        val = s.lower()
    if val not in ("stable", "beta"):
        return "stable"
    return val


def read_channel() -> str:
    for path in (CHANNEL_LOCAL, CHANNEL_ROOT):
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return _parse_channel(f.read())
            except OSError:
                continue
    return "stable"


def write_channel(channel: str) -> None:
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    body = (
        "# MuseNest update channel\n"
        "# stable  - only main\n"
        "# beta    - newest remote branch\n"
        f"{channel}\n"
    )
    for path in (CHANNEL_LOCAL, CHANNEL_ROOT):
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
            "--sort=-creatordate",
            "--format=%(refname:short)",
            "refs/remotes/origin",
        ]
    )
    if r.returncode != 0:
        return "main"
    for line in r.stdout.splitlines():
        name = line.strip()
        if not name or name == "origin/HEAD":
            continue
        if name.startswith("origin/"):
            name = name[len("origin/") :]
        return name or "main"
    return "main"


def remote_exists(branch: str) -> bool:
    r = _run(["git", "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}"])
    return r.returncode == 0


def update() -> int:
    print("[*] Auto-update...")
    if not _have_git():
        print("[!] git not found — skip auto-update")
        return 0
    if not os.path.isdir(os.path.join(ROOT, ".git")):
        print("[!] not a git clone — skip auto-update")
        return 0

    channel = read_channel()
    write_channel(channel)

    fetch = _run(["git", "fetch", "origin", "--prune"])
    if fetch.returncode != 0:
        print("[!] git fetch failed — continue with local files")
        if fetch.stdout:
            print(fetch.stdout.strip())
        return 0

    branch = "main"
    if channel == "beta":
        branch = newest_remote_branch()
    if not remote_exists(branch):
        print(f"[!] origin/{branch} missing — fallback to main")
        branch = "main"
        if not remote_exists(branch):
            print("[!] origin/main missing — skip auto-update")
            return 0

    print(f"[*] Channel: {channel}  ->  branch: {branch}")
    co = _run(["git", "checkout", "-B", branch, f"origin/{branch}"])
    if co.returncode != 0:
        print("[!] checkout failed — continue with local files")
        if co.stdout:
            print(co.stdout.strip())
        return 0
    rst = _run(["git", "reset", "--hard", f"origin/{branch}"])
    if rst.returncode != 0:
        print("[!] reset failed — continue with local files")
        if rst.stdout:
            print(rst.stdout.strip())
        return 0

    write_channel(channel)
    head = _run(["git", "rev-parse", "--short", "HEAD"])
    sha = (head.stdout or "").strip()
    print(f"[*] Updated to {sha} on {branch}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(update())
    except Exception as e:
        print(f"[!] auto-update error: {e}")
        sys.exit(0)
