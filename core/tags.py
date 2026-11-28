"""ID3 tagging: the monitored artist lives in the file's METADATA.

Historically the converter stripped all tags (`-map_metadata -1`) and the
filename carried only the track name — so players showed no artist at all.
Now every produced .mp3 gets artist / title / album written into ID3 tags,
and :func:`backfill_tags` walks the library (the tracks DB first, then the
save folder on disk) to fix everything already downloaded.

Failures never break a download: tagging problems are logged, not raised.
mutagen is the recommended writer, but a built-in pure-python ID3v2.3
writer/reader takes over when it is missing — tagging must work everywhere,
including machines where `pip install mutagen` has not happened yet.
"""
import os

from . import status
from . import db as db_mod

try:
    from mutagen.id3 import ID3, ID3NoHeaderError, TPE1, TIT2, TALB
    _HAVE_MUTAGEN = True
except ImportError:   # pragma: no cover - mutagen ships in requirements
    _HAVE_MUTAGEN = False

_BACKFILL_LOCK = None   # created lazily: tests reload this module
_FALLBACK_NOTED = False


# ---------------------------------------------------------------------------
# Built-in ID3v2.3 (no dependencies): frames are id + syncsafe size + payload;
# text payloads are UTF-16LE with BOM; the tag is PREPENDED to the mp3 data
# (replacing any tag already there). Enough for TPE1/TIT2/TALB and readable
# by every player.
# ---------------------------------------------------------------------------

def _syncsafe(n):
    return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])


def _unsyncsafe(b4):
    return (b4[0] << 21) | (b4[1] << 14) | (b4[2] << 7) | b4[3]


def _text_frame(fid, text):
    payload = (b"\x01" + b"\xff\xfe" + str(text).encode("utf-16-le")
               + b"\x00\x00")
    return fid.encode("ascii") + _syncsafe(len(payload)) + b"\x00\x00" + payload


def _read_fallback_frames(path):
    """TPE1/TIT2/TALB texts of an existing ID3v2 tag (pure python)."""
    try:
        with open(path, "rb") as f:
            head = f.read(10)
            if head[:3] != b"ID3" or len(head) < 10:
                return {}
            data = f.read(_unsyncsafe(head[6:10]))
    except Exception:
        return {}
    frames = {}
    pos = 0
    while pos + 10 <= len(data):
        fid = data[pos:pos + 4].decode("ascii", "replace")
        if not fid.isalnum():
            break   # padding
        fsz = _unsyncsafe(data[pos + 4:pos + 8])
        if fsz < 1 or pos + 10 + fsz > len(data):
            break
        frames[fid] = data[pos + 10:pos + 10 + fsz]
        pos += 10 + fsz
    out = {}
    for key, fid in (("artist", "TPE1"), ("title", "TIT2"), ("album", "TALB")):
        payload = frames.get(fid)
        if not payload:
            continue
        try:
            enc = payload[0]
            raw = payload[1:]
            # Strip exactly the NUL terminator — never a blind rstrip: the
            # high byte of every latin UTF-16-LE char is 0x00, and eating it
            # corrupts the last character (odd byte count -> decode error).
            term = b"\x00\x00" if enc in (1, 2) else b"\x00"
            if raw.endswith(term):
                raw = raw[:-len(term)]
            if enc == 1:
                if raw[:2] == b"\xff\xfe":
                    text = raw[2:].decode("utf-16-le")
                elif raw[:2] == b"\xfe\xff":
                    text = raw[2:].decode("utf-16-be")
                else:
                    text = raw.decode("utf-16-le", "replace")
            elif enc == 2:
                text = raw.decode("utf-16-be", "replace")
            elif enc == 3:
                text = raw.decode("utf-8", "replace")
            else:
                text = raw.decode("latin-1", "replace")
        except Exception:
            continue
        if text:
            out[key] = text
    return out


def _write_id3_fallback(path, artist, title, album):
    """Prepend a fresh ID3v2.3 tag (TPE1/TIT2/TALB) to the mp3 data."""
    try:
        with open(path, "rb") as f:
            data = f.read()
        body = data
        if body[:3] == b"ID3" and len(body) >= 10:
            old = _unsyncsafe(body[6:10])
            if 10 + old <= len(body):
                body = body[10 + old:]
        frames = b""
        for fid, val in (("TPE1", artist), ("TIT2", title), ("TALB", album)):
            if val:
                frames += _text_frame(fid, val)
        if not frames:
            return False
        tag = b"ID3\x03\x00\x00" + _syncsafe(len(frames)) + frames
        tmp = path + ".tagtmp"
        with open(tmp, "wb") as f:
            f.write(tag + body)
        os.replace(tmp, path)
        return True
    except Exception as e:
        status.log.warning("Встроенная запись ID3 не удалась для %s: %s", path, e)
        return False


def _note_fallback():
    global _FALLBACK_NOTED
    if not _FALLBACK_NOTED:
        _FALLBACK_NOTED = True
        status.log.info("mutagen не найден — теги пишет встроенный ID3-"
                        "записыватель (pip install mutagen — рекомендовано)")


def _get_frames(path):
    """Existing ID3 frames of a file (empty dict when untagged)."""
    if _HAVE_MUTAGEN:
        try:
            id3 = ID3(path)
        except ID3NoHeaderError:
            return {}
        except Exception:
            return {}
        out = {}
        for key, fid in (("artist", "TPE1"), ("title", "TIT2"), ("album", "TALB")):
            frames = id3.getall(fid)
            if frames and frames[0].text:
                out[key] = str(frames[0].text[0] or "")
        return out
    return _read_fallback_frames(path)


def tag_mp3(path, artist, title=None, album=None):
    """Write artist/title/album into the file's ID3 tags.

    Returns True when the file was (re)tagged, False when it already carried
    exactly these tags (no rewrite — mtimes stay untouched on re-runs) or
    when the file is gone / tagging is unavailable.
    """
    if not path or not str(path).lower().endswith(".mp3") or not os.path.exists(path):
        return False
    cur = _get_frames(path)
    same = (cur.get("artist", "").casefold() == str(artist or "").casefold()
            and (title is None or cur.get("title", "") == str(title))
            and (album is None or cur.get("album", "") == str(album)))
    if same:
        return False
    if _HAVE_MUTAGEN:
        id3 = ID3()
        if artist:
            id3.add(TPE1(encoding=3, text=str(artist)))
        if title:
            id3.add(TIT2(encoding=3, text=str(title)))
        if album:
            id3.add(TALB(encoding=3, text=str(album)))
        try:
            id3.save(path, v2_version=3)
            return True
        except Exception as e:
            status.log.warning("Не удалось записать теги в %s: %s", path, e)
            return False
    _note_fallback()
    return _write_id3_fallback(path, artist, title, album)


def _disk_rows(save_folder, seen):
    """(artist, title, album, path) for every mp3 under save_folder.

    Layout is save_folder/[genre/…]/Artist/Album/Track.mp3 — the folder
    names know the artist even when the DB row is missing.
    """
    rows = []
    root = str(save_folder or "").strip()
    if not root or not os.path.isdir(root):
        return rows
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if not f.lower().endswith(".mp3"):
                continue
            path = os.path.abspath(os.path.join(dirpath, f))
            if path in seen:
                continue
            seen.add(path)
            parts = os.path.normpath(dirpath).split(os.sep)[-2:]
            album = parts[1] if len(parts) == 2 else ""
            artist = parts[0] if len(parts) == 2 else ""
            rows.append((artist, os.path.splitext(f)[0], album, path))
    return rows


def backfill_tags(cfg=None):
    """Tag every existing .mp3: DB rows first, then the save folder.

    Safe to run repeatedly (and from the startup thread + each scan):
    correctly tagged files are skipped without a rewrite. Returns a summary
    dict {"tagged", "skipped", "errors"}.
    """
    global _BACKFILL_LOCK
    if _BACKFILL_LOCK is None:
        import threading
        _BACKFILL_LOCK = threading.Lock()
    with _BACKFILL_LOCK:
        stats = {"tagged": 0, "skipped": 0, "errors": 0}
        if not _HAVE_MUTAGEN:
            _note_fallback()
        seen = set()
        try:
            rows = db_mod.get_all_tracks(limit=1000000)
        except Exception as e:
            status.log.warning("Прогон тегов: база недоступна (%s)", e)
            rows = []
        for r in rows:
            path = r.get("filepath") or ""
            if not path or not os.path.exists(path):
                continue
            seen.add(os.path.abspath(path))
            try:
                if tag_mp3(path, r.get("artist"), r.get("track")):
                    stats["tagged"] += 1
                else:
                    stats["skipped"] += 1
            except Exception:
                stats["errors"] += 1
        if cfg is None:
            from . import config as cfg_mod
            try:
                cfg = cfg_mod.load_config()
            except Exception:
                cfg = {}
        try:
            disk = _disk_rows(cfg.get("save_folder", "downloads"), seen)
        except Exception as e:
            status.log.warning("Прогон тегов: обход папки не удался (%s)", e)
            disk = []
        for artist, title, album, path in disk:
            try:
                if tag_mp3(path, artist, title, album or None):
                    stats["tagged"] += 1
                else:
                    stats["skipped"] += 1
            except Exception:
                stats["errors"] += 1
        status.log.info("🏷️ Прогон тегов: записано %d, уже с тегами %d, ошибок %d",
                        stats["tagged"], stats["skipped"], stats["errors"])
        return stats
