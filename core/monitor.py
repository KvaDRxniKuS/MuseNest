import os
import time
import socket
import datetime
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import config as cfg_mod
from . import source as src_mod
from . import youtube as yt_mod
from . import matcher as mat_mod
from . import db as db_mod
from . import status

# Set global socket timeout so yt-dlp requests don't hang forever
socket.setdefaulttimeout(35)


def _sanitize(name):
    bad = '<>:\"/\\|?*'
    out = []
    for ch in str(name):
        if ch in bad:
            continue
        out.append(ch)
    name = "".join(out).strip().rstrip(".")
    name = " ".join(name.split())
    return name[:180] or "untitled"


def _stopped():
    return status.status.get("stop_requested", False)


def set_thread_state(worker_id, state, task):
    status.set_thread_state(worker_id, state, task)


def process_track(track, spot_artist, cfg, worker_id=1):
    if _stopped():
        set_thread_state(worker_id, "idle", "Остановлено")
        return

    track_name = track["name"]
    album_name = track.get("album_name", "Unknown Album")
    dur = track.get("duration_ms") or 0
    query = f"{spot_artist} {track_name}"

    # Quick local filter check: skip blacklisted words in track/query immediately
    blacklist_l = [b.lower().strip() for b in (cfg.get("blacklist") or []) if b and b.strip()]
    query_l = query.lower()
    if any(b in query_l for b in blacklist_l):
        try:
            from . import library as lib_mod
            lib_mod.update_track_status(spot_artist, album_name, track["id"], downloaded=False, no_match=True, error_code="ERR-3")
        except Exception:
            pass
        status.log.info("Filter error (ERR-3): %s - %s", spot_artist, track_name)
        status.inc("failed")
        set_thread_state(worker_id, "idle", "⚠️ Фильтр: " + track_name)
        return

    # Update global status stage with Artist and Album name
    status.status["current_stage"] = f"Скачивание: {spot_artist} — {album_name}"

    set_thread_state(worker_id, "searching", f"🔍 Поиск: {track_name}")

    try:
        results = yt_mod.search_youtube(query, limit=15, cfg=cfg)
    except Exception as e:
        if _stopped():
            set_thread_state(worker_id, "idle", "Остановлено")
            return
        status.log.error("YouTube search failed for %s - %s: %s",
                         spot_artist, track_name, e)
        status.inc("failed")
        
        # Mark as no_match with ERR-5 (Search Error)
        try:
            from . import library as lib_mod
            lib_mod.update_track_status(spot_artist, album_name, track["id"], downloaded=False, no_match=True, error_code="ERR-5")
        except Exception:
            pass
            
        set_thread_state(worker_id, "error", "❌ Ошибка поиска")
        return

    if _stopped():
        set_thread_state(worker_id, "idle", "Остановлено")
        return

    best, err_code = mat_mod.find_best_match(
        results, dur, track_name, spot_artist,
        cfg["blacklist"], cfg["duration_tolerance_sec"],
        cfg["fallback_to_closest"],
    )
    if not best:
        status.log.warning("No suitable YouTube match for %s - %s (Code: %s)",
                           spot_artist, track_name, err_code)
        status.inc("failed")
        
        # Mark as no_match with the actual err_code from find_best_match
        try:
            from . import library as lib_mod
            lib_mod.update_track_status(spot_artist, album_name, track["id"], downloaded=False, no_match=True, error_code=err_code or "ERR-1")
        except Exception:
            pass
            
        set_thread_state(worker_id, "idle", "⚠️ Нет совпадений")
        return

    if _stopped():
        set_thread_state(worker_id, "idle", "Остановлено")
        return

    album_name = track.get("album_name", "Unknown Album")
    
    # Retrieve genre_path of spot_artist from config
    genre_path = ""
    for a in cfg.get("artists", []):
        if isinstance(a, dict) and a.get("name") == spot_artist:
            genre_path = a.get("genre_path", "")
            break
            
    if genre_path:
        path_parts = [cfg["save_folder"]] + [p.strip() for p in genre_path.split("/") if p.strip()] + [_sanitize(spot_artist), _sanitize(album_name)]
        folder = os.path.join(*path_parts)
    else:
        folder = os.path.join(cfg["save_folder"], _sanitize(spot_artist), _sanitize(album_name))
        
    os.makedirs(folder, exist_ok=True)
    base = _sanitize(track_name)
    out_no_ext = os.path.join(folder, base)

    set_thread_state(worker_id, "downloading", f"⬇️ Скачивание: {track_name}")

    def _progress_hook(d):
        if _stopped():
            raise RuntimeError("Stop requested")
        try:
            st = d.get("status")
            if st == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes") or 0
                pct = round(done * 100.0 / total, 1) if total else None
                status.set_thread_progress(worker_id, {
                    "percent": pct,
                    "downloaded_bytes": done,
                    "total_bytes": total,
                    "speed": d.get("speed"),
                    "eta": d.get("eta"),
                })
            elif st == "finished":
                set_thread_state(worker_id, "processing",
                                 f"⚙️ Конвертация: {track_name}")
        except Exception:
            pass

    try:
        yt_mod.download_audio(
            best.get("webpage_url") or best.get("url"),
            out_no_ext, cfg["audio_quality"],
            progress_hook=_progress_hook,
            cfg=cfg,
        )
        
        if _stopped():
            set_thread_state(worker_id, "idle", "Остановлено")
            return
        
        final = out_no_ext + ".mp3"
        db_mod.add_track(spot_artist, track_name, track["id"],
                         best["id"], final, dur)

        # Update status in library.json as well (success, clear errors)
        try:
            from . import library as lib_mod
            lib_mod.update_track_status(spot_artist, album_name, track["id"], downloaded=True, no_match=False, error_code="")
        except Exception as le:
            status.log.debug("Failed to update track downloaded status in library.json: %s", le)

        status.inc("downloaded")
        status.log.info("Downloaded: %s - %s", spot_artist, track_name)
        set_thread_state(worker_id, "idle", f"✅ Готово: {track_name}")
    except Exception as e:
        if _stopped():
            set_thread_state(worker_id, "idle", "Остановлено")
            return
        status.log.error("Download failed for %s - %s: %s",
                         spot_artist, track_name, e)
        status.inc("failed")
        
        # Mark as no_match with ERR-4 (Download Error)
        try:
            from . import library as lib_mod
            lib_mod.update_track_status(spot_artist, album_name, track["id"], downloaded=False, no_match=True, error_code="ERR-4")
        except Exception:
            pass
            
        set_thread_state(worker_id, "error", f"❌ Ошибка загрузки")


def scan_artist(src, artist_entry, cfg):
    if _stopped():
        return

    if isinstance(artist_entry, dict):
        entry_source = artist_entry.get("source", "deezer")
        if entry_source == "spotify":
            entry_id = artist_entry.get("spotify_id") or artist_entry.get("id")
        else:
            entry_id = artist_entry.get("deezer_id") or artist_entry.get("id")
        entry_name = artist_entry.get("name", "")
        spotify_name = artist_entry.get("spotify_name")
    else:
        entry_id = None
        entry_name = artist_entry
        spotify_name = None

    status.status["current_stage"] = f"Разрешение артиста: {spotify_name or entry_name}"

    if entry_id:
        try:
            a = src.get_artist(entry_id)
            resolved = a["name"]
            artist_id = a["id"]
            status.log.info("Resolved artist by ID: %s (%s)", resolved, artist_id)
        except Exception as e:
            status.log.warning("Artist ID not found on source: %s (%s)",
                             entry_id, e)
            status.inc("skipped")
            return
    else:
        a = src.search_artist(entry_name)
        if not a:
            status.log.warning("Artist not found on source: %s", entry_name)
            status.inc("skipped")
            return
        resolved = a["name"]
        artist_id = a["id"]

    if _stopped():
        return

    spot_artist = spotify_name or resolved
    status.status["current_artist"] = spot_artist
    status.log.info("=== Scanning artist: %s ===", spot_artist)

    status.status["current_stage"] = f"Получение альбомов: {spot_artist}"
    albums = src.get_albums(artist_id, limit=cfg["max_albums_per_artist"])
    status.log.info("Found %d albums/singles", len(albums))

    if _stopped():
        return

    # Automatically sync this artist's tree into library.json
    try:
        from . import library as lib_mod
        lib_mod.sync_artist_to_library(spot_artist, artist_id, albums, src, cfg)
    except Exception as se:
        status.log.debug("Auto metadata sync failed inside monitor: %s", se)

    # Sort albums: full albums ("album") first, then singles ("single")
    # This ensures we gather the track from the full album if it exists, rather than the single
    albums = sorted(albums, key=lambda x: 0 if x.get("album_type") == "album" else 1)

    status.status["current_stage"] = f"Сбор треков: {spot_artist} — подготовка..."
    seen = set()
    tracks = []
    from . import library as lib_mod
    lib_data = lib_mod.load_library()
    
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    def fetch_monitor_tracks_task(alb):
        alb_id = alb["id"]
        alb_name = alb.get("name", "Unknown Album")
        try:
            tracks_data = src.get_album_tracks(alb_id)
        except Exception as e:
            status.log.debug("Error getting album tracks in monitor: %s", e)
            tracks_data = []
        return alb_id, alb_name, tracks_data

    album_tracks_map = {}
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(fetch_monitor_tracks_task, alb): alb for alb in albums}
        completed = 0
        for fut in as_completed(futures):
            if _stopped():
                break
            completed += 1
            try:
                alb_id, alb_name, tracks_data = fut.result()
                status.status["current_stage"] = f"Сбор треков: {spot_artist} — ({completed}/{len(albums)} альбомов)..."
                album_tracks_map[alb_id] = (alb_name, tracks_data)
            except Exception:
                pass
                
    for al in albums:
        alb_id = al["id"]
        if alb_id in album_tracks_map:
            alb_name, tracks_data = album_tracks_map[alb_id]
            for t in tracks_data:
                if _stopped():
                    break
                tid = t.get("id")
                if tid in seen:
                    continue
                seen.add(tid)
                
                t["album_name"] = alb_name
                
                # Skip if this track/album/artist is ignored
                if lib_mod.is_track_ignored(spot_artist, alb_name, tid, lib_data):
                    status.log.info("Skipping ignored track: %s - %s - %s", spot_artist, alb_name, t["name"])
                    continue
                
                tracks.append(t)

    if _stopped():
        return

    status.log.info("Total unique tracks: %d", len(tracks))

    # Build a set of normalized track names currently on disk for this artist (any folder)
    disk_track_names = set()
    sanitized_art = _sanitize(spot_artist)
    artist_dir = os.path.join(cfg["save_folder"], sanitized_art)
    
    # Retrieve genre_path of spot_artist from config
    genre_path = ""
    for a in cfg.get("artists", []):
        if isinstance(a, dict) and a.get("name") == spot_artist:
            genre_path = a.get("genre_path", "")
            break
            
    if genre_path:
        path_parts = [cfg["save_folder"]] + [p.strip() for p in genre_path.split("/") if p.strip()] + [sanitized_art]
        target_artist_dir = os.path.join(*path_parts)
    else:
        target_artist_dir = artist_dir
        
    # Check general save folder
    if os.path.exists(cfg["save_folder"]):
        for f in os.listdir(cfg["save_folder"]):
            if f.lower().endswith(".mp3") and f.lower().startswith(sanitized_art.lower() + " - "):
                prefix_len = len(sanitized_art) + 3
                t_name = f[prefix_len:-4]
                disk_track_names.add(_sanitize(t_name).lower())
                
    # Check artist subfolders
    if os.path.exists(artist_dir):
        for root, dirs, files in os.walk(artist_dir):
            for f in files:
                if f.lower().endswith(".mp3"):
                    t_name = f[:-4]
                    if t_name.lower().startswith(sanitized_art.lower() + " - "):
                        t_name = t_name[len(sanitized_art) + 3:]
                    disk_track_names.add(_sanitize(t_name).lower())
                    
    # Check new nested target artist subfolders
    if target_artist_dir != artist_dir and os.path.exists(target_artist_dir):
        for root, dirs, files in os.walk(target_artist_dir):
            for f in files:
                if f.lower().endswith(".mp3"):
                    t_name = f[:-4]
                    if t_name.lower().startswith(sanitized_art.lower() + " - "):
                        t_name = t_name[len(sanitized_art) + 3:]
                    disk_track_names.add(_sanitize(t_name).lower())

    new_tracks = []
    for t in tracks:
        if _stopped():
            break
        status.inc("processed")
        
        # Check if downloaded in DB, or if the file exists ANYWHERE under this artist on disk
        norm_key = _sanitize(t["name"]).lower()
        if db_mod.is_downloaded(t["id"]) or (norm_key in disk_track_names):
            status.inc("skipped")
            continue
            
        status.inc("found")
        new_tracks.append(t)

    if not new_tracks or _stopped():
        status.log.info("Nothing new for %s", spot_artist)
        return

    status.status["current_stage"] = f"Скачивание: {spot_artist} ({len(new_tracks)} шт.)"
    status.log.info("Downloading %d new tracks using %d thread(s)...",
                    len(new_tracks), cfg["download_threads"])

    workers = max(1, int(cfg["download_threads"]))
    
    # Use ThreadPoolExecutor with ability to cancel pending futures on stop
    executor = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = []
        for i, t in enumerate(new_tracks):
            if _stopped():
                break
            worker_id = (i % workers) + 1
            futures.append(executor.submit(process_track, t, spot_artist, cfg, worker_id))

        # Wait for completion, but check stop flag frequently
        while futures:
            if _stopped():
                # Cancel all pending (not yet started) futures immediately
                for f in futures:
                    f.cancel()
                break
            
            # Wait a short time for any future to complete
            done = []
            for f in futures:
                if f.done():
                    done.append(f)
            
            for f in done:
                futures.remove(f)
                try:
                    f.result()
                except Exception as e:
                    if not _stopped():
                        status.log.error("Worker error: %s", e)
                        status.inc("failed")
            
            if not done:
                time.sleep(0.3)  # Short sleep to avoid busy-waiting
    finally:
        # Don't wait for running threads — shut down immediately
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            executor.shutdown(wait=False)  # Python < 3.9 fallback

    # Reset worker states
    for w in range(1, workers + 1):
        set_thread_state(w, "idle", "Ожидание")


def run_scan(cfg):
    if status.status["running"]:
        return False
    status.reset_counters()
    status.status["running"] = True
    status.status["stop_requested"] = False
    status.status["last_scan"] = datetime.datetime.now().isoformat()
    status.status["current_stage"] = "Инициализация источника"

    workers = max(1, int(cfg.get("download_threads", 4)))
    for w in range(1, workers + 1):
        status.status["threads_info"][str(w)] = {
            "state": "idle", "task": "Ожидание", "progress": None,
        }

    try:
        src_name = cfg.get("music_source", "deezer")
        src = src_mod.build_source(src_name, cfg)
        src._auth()
    except Exception as e:
        status.log.error("Source authentication failed: %s", e)
        status.status["running"] = False
        status.status["current_stage"] = "Ошибка авторизации"
        return False

    for artist_entry in cfg["artists"]:
        if _stopped():
            break
        name = artist_entry.get("spotify_name") or \
              (artist_entry["name"] if isinstance(artist_entry, dict) else artist_entry)
        status.status["current_artist"] = name
        try:
            # Dynamically determine and build the source for this specific artist
            art_src_name = artist_entry.get("source", cfg.get("music_source", "deezer")) if isinstance(artist_entry, dict) else cfg.get("music_source", "deezer")
            art_src = src_mod.build_source(art_src_name, cfg)
            art_src._auth()
            scan_artist(art_src, artist_entry, cfg)
        except Exception as e:
            status.log.error("Error while scanning %s: %s", name, e)

    # Automatically run check_library_files at the end of the scan to sort everything
    try:
        from . import library as lib_mod
        lib_mod.check_library_files(cfg)
    except Exception as e:
        status.log.debug("Auto-check and sort failed at end of scan: %s", e)

    status.status["running"] = False
    status.status["current_artist"] = ""
    is_stopped = _stopped()
    status.status["current_stage"] = "⏹ Остановлено" if is_stopped else "✅ Завершено"
    for w in status.status["threads_info"]:
        status.status["threads_info"][w] = {
            "state": "idle",
            "task": "⏹ Остановлено" if is_stopped else "✅ Готово",
            "progress": None,
        }
    status.log.info("Scan finished. Downloaded: %d, Failed: %d",
                    status.status["downloaded"], status.status["failed"])
    return True

