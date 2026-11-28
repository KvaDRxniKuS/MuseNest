"""ID3 tagging: the monitored artist lives in the file's METADATA.

Historically the converter stripped all tags (`-map_metadata -1`) and the
filename carried only the track name — so players showed no artist at all.
Now every produced .mp3 gets artist / title / album written into ID3 tags,
and :func:`backfill_tags` walks the library (the tracks DB first, then the
save folder on disk) to fix everything already downloaded.

Failures never break a download: tagging problems are logged, not raised.
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


def _get_frames(path):
    """Existing ID3 frames of a file (empty dict when untagged)."""
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


def tag_mp3(path, artist, title=None, album=None):
    """Write artist/title/album into the file's ID3 tags.

    Returns True when the file was (re)tagged, False when it already carried
    exactly these tags (no rewrite — mtimes stay untouched on re-runs) or
    when the file is gone / tagging is unavailable.
    """
    if not _HAVE_MUTAGEN:
        status.log.warning("mutagen недоступен — теги не записаны в %s", path)
        return False
    if not path or not str(path).lower().endswith(".mp3") or not os.path.exists(path):
        return False
    cur = _get_frames(path)
    same = (cur.get("artist", "").casefold() == str(artist or "").casefold()
            and (title is None or cur.get("title", "") == str(title))
            and (album is None or cur.get("album", "") == str(album)))
    if same:
        return False
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
            status.log.warning("mutagen недоступен — прогон тегов пропущен")
            return stats
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
