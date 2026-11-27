#!/usr/bin/env python3
"""Collect the list of artists hidden in a music folder.

Point it at a library (or just double-click ``scan_artists.bat`` sitting next to
it) and it walks every subfolder, works out who performs each track and writes
the deduplicated names to ``artists.txt`` beside the script.

For every file the artist is resolved in this order:

1. the ``artist`` tag inside the file (ID3v2 for mp3, Vorbis comment for
   flac/ogg) - the only source that is right for a ``Artist/Album/01 - Title``
   library, where the file name carries no artist at all;
2. the ``"Artist - Title"`` convention in the file name, with a leading track
   number (``01.``/``01)``/``#01``) stripped;
3. the folder the file lives in, walking up past year and ``Disc N`` folders.

``--no-tags`` forces step 2/3 only, i.e. strictly "read the track names".

The output is a plain UTF-8 list, one name per line, with ``#`` comment lines -
exactly what MuseNest's own ``data/artists.txt`` accepts, so the file can be
copied straight in.

Stdlib only: no mutagen, no third-party deps.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import unicodedata

AUDIO_EXTS = {
    ".mp3", ".flac", ".m4a", ".ogg", ".oga", ".opus",
    ".wav", ".aac", ".wma", ".ape", ".alac", ".aiff", ".aif",
}

# Names that mean "we do not know", not a real performer.
JUNK_ARTISTS = {
    "unknown", "unknown artist", "unknown performer", "no artist", "n/a", "na",
    "va", "various", "various artists", "soundtrack", "ost", "original soundtrack",
    "artist", "unknownartist",
    "неизвестен", "неизвестно", "неизвестный", "неизвестный исполнитель",
    "разные исполнители", "без исполнителя",
}

# Folder names that describe the medium or the year, not the performer.
_JUNK_FOLDER_RE = re.compile(
    r"^(?:"
    r"disc\s*\d+|disk\s*\d+|cd\s*\d+|vol\.?\s*\d+|volume\s*\d+"       # Disc 1 / CD2
    r"|(?:19|20)\d{2}[- ]?(?:19|20)\d{2}?"                            # 1998-2004
    r"|(?:19|20)\d{2}"                                                # 2011
    r"|discography|singles|albums|compilations|misc|other"
    r"|музыка|музыка на телефон"
    r")$",
    re.IGNORECASE,
)

# A leading track number is only a track number when it is *marked* as one:
# "01. Artist", "01) Artist", "#01 Artist". A bare number followed by a space
# ("3 Doors Down", "30 Seconds To Mars") is part of the band name and must
# survive - an earlier version stripped it and silently renamed both bands.
_LEADING_NUMBER_RE = re.compile(r"^(?:\s*#\s*\d{1,4}\s*|\s*\d{1,4}\s*[.)\]:-]\s*)")


# --------------------------------------------------------------------------
# tag readers (stdlib only)
# --------------------------------------------------------------------------

def _synchsafe(raw: bytes) -> int:
    n = 0
    for byte in raw:
        n = (n << 7) | (byte & 0x7F)
    return n


def _decode_text_frame(payload: bytes) -> str:
    """Decode an ID3v2 text frame body (encoding byte + text)."""
    if not payload:
        return ""
    enc, body = payload[0], payload[1:]
    table = {0: "latin-1", 1: "utf-16", 2: "utf-16-be", 3: "utf-8"}
    try:
        text = body.decode(table.get(enc, "latin-1"), "replace")
    except Exception:
        return ""
    # UTF-16 separators are two bytes wide, everything else one.
    terminator = "\x00\x00" if enc in (1, 2) else "\x00"
    return text.split(terminator)[0].strip()


def read_id3_artist(path: str, max_bytes: int = 1 << 20) -> str:
    """Return the TPE1/TP1 artist from an ID3v2 tag, or None."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(10)
            if len(head) < 10 or head[:3] != b"ID3":
                return None
            major, flags = head[3], head[5]
            tag = fh.read(_synchsafe(head[6:10]) or 0)
    except OSError:
        return None
    if not tag or major not in (2, 3, 4):
        return None

    pos = 0
    if flags & 0x40:  # extended header - skip it, its layout differs per version
        if major == 4:
            pos = _synchsafe(tag[0:4]) or len(tag)
        else:
            pos = 4 + int.from_bytes(tag[0:4], "big")

    # v2.2 frames are 3+3 bytes with no flags; v2.3/v2.4 are 4+4 bytes plus 2
    # flag bytes. Getting the size width wrong reads garbage.
    id_len, size_len = (3, 3) if major == 2 else (4, 4)
    head_len = id_len + size_len + (0 if major == 2 else 2)
    wanted = {b"TP1", b"TPE1"}

    while pos + head_len <= len(tag):
        frame_id = tag[pos:pos + id_len]
        if frame_id == b"\x00" * id_len:
            break
        if not all(32 <= c < 127 for c in frame_id):
            break
        raw_size = tag[pos + id_len:pos + id_len + size_len]
        size = _synchsafe(raw_size) if major == 4 else int.from_bytes(raw_size, "big")
        if size <= 0:
            break
        if frame_id in wanted:
            artist = _decode_text_frame(tag[pos + head_len:pos + head_len + size])
            if artist:
                return artist
        pos += head_len + size
    return None


def read_flac_artist(path: str, max_bytes: int = 1 << 20) -> str:
    """Return ARTIST from a FLAC Vorbis comment block, or None."""
    try:
        with open(path, "rb") as fh:
            if fh.read(4) != b"fLaC":
                return None
            found = ""
            while True:
                header = fh.read(4)
                if len(header) < 4:
                    return found or None
                last, block_type = bool(header[0] & 0x80), header[0] & 0x7F
                length = int.from_bytes(header[1:4], "big")
                if block_type != 4:  # not a VORBIS_COMMENT block
                    fh.seek(length, os.SEEK_CUR)
                else:
                    body = fh.read(length)
                    found = _parse_vorbis_comments(body) or found
                    if found:
                        return found
                if last:
                    return found or None
    except OSError:
        return None


def _parse_vorbis_comments(body: bytes) -> str:
    try:
        pos = 0
        vendor_len = int.from_bytes(body[pos:pos + 4], "little")
        pos += 4 + vendor_len
        count = int.from_bytes(body[pos:pos + 4], "little")
        pos += 4
        fallback = ""
        for _ in range(count):
            if pos + 4 > len(body):
                break
            size = int.from_bytes(body[pos:pos + 4], "little")
            pos += 4
            entry = body[pos:pos + size].decode("utf-8", "replace")
            pos += size
            key, _, value = entry.partition("=")
            key = key.strip().upper()
            value = value.strip()
            if key == "ARTIST" and value:
                return value
            if key == "ALBUMARTIST" and value and not fallback:
                fallback = value
        return fallback
    except Exception:
        return ""


def read_tag_artist(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".mp3":
        return read_id3_artist(path) or ""
    if ext in (".flac", ".ogg", ".oga", ".opus"):
        return read_flac_artist(path) or ""
    return ""


# --------------------------------------------------------------------------
# name / folder heuristics
# --------------------------------------------------------------------------

def clean_name(value: str) -> str:
    """Normalise a candidate name: trim, collapse spaces, drop stray brackets."""
    value = unicodedata.normalize("NFC", value or "")
    value = value.replace("\uff0d", "-").replace("\u2013", "-").replace("\u2014", "-")
    value = re.sub(r"\s+", " ", value).strip(" -_.\t")
    return value


def artist_from_filename(stem: str) -> str:
    """'Artist - Title' / 'Artist - Album - Title' -> 'Artist', else ''.

    A bare leading track number is not an artist, so '01 - Title' yields ''
    and the caller falls back to the folder.
    """
    stem = clean_name(stem)
    if " - " not in stem:
        return ""
    parts = stem.split(" - ")
    head = clean_name(_LEADING_NUMBER_RE.sub("", parts[0], count=1))
    if head and not head.isdigit():
        return head
    # The first part was a bare track number. "NN - Artist - Title" still names
    # the artist in the second part, while "NN - Title" does not name anyone -
    # there the folder is the only source, so return nothing here.
    if len(parts) >= 3:
        second = clean_name(parts[1])
        if second and not second.isdigit():
            return second
    return ""


def is_junk_folder(name: str) -> bool:
    name = clean_name(name)
    return (not name) or bool(_JUNK_FOLDER_RE.match(name))


def artist_from_folders(path: str, root: str) -> str:
    """Walk up from the file, skipping year/'Disc N'/the root folder itself."""
    root_abs = os.path.abspath(root)
    parent = os.path.dirname(os.path.abspath(path))
    while True:
        if os.path.normcase(parent) == os.path.normcase(root_abs):
            return ""
        name = os.path.basename(parent)
        if not is_junk_folder(name):
            return clean_name(name)
        upper = os.path.dirname(parent)
        if upper == parent:
            return ""
        parent = upper


def is_junk_artist(name: str) -> bool:
    name = clean_name(name)
    if not name or len(name) < 2 or name.isdigit():
        return True
    compact = re.sub(r"[\s._-]+", "", name).casefold()
    return compact in JUNK_ARTISTS or name.casefold() in JUNK_ARTISTS


def resolve_artist(path: str, root: str, use_tags: bool = True) -> tuple[str, str]:
    """Return ``(artist, how)`` where how is tag / filename / folder / ''."""
    if use_tags:
        tagged = clean_name(read_tag_artist(path))
        if tagged and not is_junk_artist(tagged):
            return tagged, "tag"
    from_name = artist_from_filename(os.path.splitext(os.path.basename(path))[0])
    if from_name and not is_junk_artist(from_name):
        return from_name, "filename"
    from_folder = artist_from_folders(path, root)
    if from_folder and not is_junk_artist(from_folder):
        return from_folder, "folder"
    return "", "none"


# --------------------------------------------------------------------------
# scan
# --------------------------------------------------------------------------

def iter_audio_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("._"):
                continue
            if os.path.splitext(name)[1].lower() in AUDIO_EXTS:
                yield os.path.join(dirpath, name)


def scan(root: str, use_tags: bool = True, progress=None) -> dict:
    """Scan *root* and return artists plus how each file was resolved.

    *progress*, if given, is called as ``progress(dirpath, files, artists)`` for
    every directory walked - including ones with no audio in them, since a long
    empty branch is exactly when the caller needs to see that work continues -
    and additionally every 200 files, so a single huge flat folder is not silent
    either.
    """
    seen: dict[str, str] = {}
    stats = {"files": 0, "tag": 0, "filename": 0, "folder": 0, "none": 0, "dirs": 0}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        stats["dirs"] += 1
        if progress:
            progress(dirpath, stats["files"], len(seen))
        for name in filenames:
            if name.startswith("._"):
                continue
            if os.path.splitext(name)[1].lower() not in AUDIO_EXTS:
                continue
            stats["files"] += 1
            artist, how = resolve_artist(os.path.join(dirpath, name), root, use_tags=use_tags)
            stats[how] = stats.get(how, 0) + 1
            if artist:
                key = re.sub(r"[\s._-]+", "", artist).casefold()
                # Keep the first spelling seen; later case variants must not duplicate.
                seen.setdefault(key, artist)
            # One flat folder with thousands of tracks would otherwise report
            # nothing at all until it finished.
            if progress and stats["files"] % 200 == 0:
                progress(dirpath, stats["files"], len(seen))
    return {
        "artists": sorted(seen.values(), key=lambda s: (s.casefold(), s)),
        "stats": stats,
    }


def make_progress_printer(root: str, interval: float = 0.15):
    """Throttled 'still working' line: one per *interval* seconds at most.

    Without this a scan of a big library prints nothing for minutes and the
    console looks dead.
    """
    state = {"last": 0.0}

    def progress(dirpath, files, artists):
        # The very first call happens before anything was read; printing
        # "0 files | 0 artists" there would look like nothing is happening.
        if not files and not artists:
            return
        now = time.monotonic()
        if state["last"] and now - state["last"] < interval:
            return
        state["last"] = now
        rel = os.path.relpath(dirpath, root)
        shown = "." if rel in (".", "") else rel
        print(f"  [{files:7d} files | {artists:4d} artists] {shown}", flush=True)

    return progress


def write_list(path: str, artists: list[str], root: str, stats: dict) -> None:
    header = (
        "# MuseNest artist scan\n"
        f"# source folder: {os.path.abspath(root)}\n"
        f"# audio files scanned: {stats['files']}\n"
        f"# artists found: {len(artists)}\n"
        "# (lines starting with # are ignored by MuseNest's artists.txt)\n"
    )
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(header)
        for name in artists:
            fh.write(name + "\n")


def clean_path_arg(value: str) -> str:
    """Undo the quoting Windows leaves on a path argument.

    A batch file calling ``script.py "%~dp0"`` hands over ``C:\\music\\"`` -
    the trailing backslash escapes the closing quote, so the quote becomes part
    of the argument and the folder is reported as missing. Strip a stray quote
    (and a matching pair), instead of failing on a folder that is right there.
    """
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value.rstrip("\"'").strip()


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:  # Windows consoles are often cp866 - never crash on a name.
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", nargs="?", default=os.path.dirname(os.path.abspath(__file__)),
                        help="folder to scan (default: the folder of this script)")
    parser.add_argument("-o", "--out", default=None,
                        help="output file (default: artists.txt next to this script)")
    parser.add_argument("--no-tags", action="store_true",
                        help="ignore embedded tags, use file names and folders only")
    parser.add_argument("--print", dest="show", action="store_true",
                        help="also print the whole list")
    parser.add_argument("--quiet", action="store_true",
                        help="no per-folder progress, only the final summary")
    args = parser.parse_args(argv)

    root = os.path.abspath(clean_path_arg(args.root))
    if not os.path.isdir(root):
        print(f"error: not a folder: {root}", file=sys.stderr)
        return 2

    out = clean_path_arg(args.out) if args.out else os.path.join(os.path.dirname(os.path.abspath(__file__)), "artists.txt")
    print("MuseNest artist scanner")
    print(f"Scanning: {root}", flush=True)

    result = scan(root, use_tags=not args.no_tags,
                  progress=None if args.quiet else make_progress_printer(root))
    artists, stats = result["artists"], result["stats"]
    write_list(out, artists, root, stats)

    print()
    print(f"Scanned : {root}")
    print(f"Folders : {stats['dirs']}")
    print(f"Files   : {stats['files']} audio "
          f"(tag: {stats['tag']}, name: {stats['filename']}, "
          f"folder: {stats['folder']}, unresolved: {stats['none']})")
    print(f"Artists : {len(artists)}")
    print(f"Written : {os.path.abspath(out)}")
    if args.show:
        print()
        for name in artists:
            print("  " + name)
    elif not artists:
        print("\nNo artists found. Check that the folder really holds audio files.")
    else:
        preview = artists[:10]
        print("\nFirst 10:")
        for name in preview:
            print("  " + name)
        if len(artists) > len(preview):
            print(f"  ... and {len(artists) - len(preview)} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
