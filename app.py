import os
import sys
import threading
import webbrowser
import datetime
import logging
import time
import json
import uuid
import shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import config as cfg_mod
from core import spotify as sp_mod
from core import source as src_mod
from core import status, db, monitor, library as lib_mod
from core import jobs as jobs_mod
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

BASE_DIR = cfg_mod.BASE_DIR

log = status.logger
_fh = logging.FileHandler(os.path.join(BASE_DIR, "tracker.log"), encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_fh)

# Silence Werkzeug HTTP request logs in console
logging.getLogger("werkzeug").setLevel(logging.ERROR)

db.init()
status.status["ffmpeg"] = shutil.which("ffmpeg") is not None
jobs_mod.start()


def _scheduler():
    while True:
        try:
            c = cfg_mod.load_config()
            interval = max(1, int(c.get("monitor_interval_minutes", 60))) * 60
        except Exception as e:
            interval = 600
        
        status.status["next_scan"] = (
            datetime.datetime.now() + datetime.timedelta(seconds=interval)
        ).isoformat()
        
        # Sleep first so the server startup doesn't trigger an immediate background scan.
        # Background scans will only run after the scheduled interval has passed.
        # Manual scans can still be triggered immediately via the UI button.
        time.sleep(interval)
        
        try:
            c = cfg_mod.load_config()
            if c["monitor_enabled"] and c.get("artists") and c.get("spotify_client_id"):
                monitor.run_scan(c)
        except Exception as e:
            log.error("Scheduler error: %s", e)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/settings", methods=["GET"])
def get_settings():
    c = cfg_mod.load_config()
    c["app_version"] = cfg_mod.APP_VERSION
    return jsonify(c)


@app.route("/api/settings", methods=["POST"])
def post_settings():
    data = request.get_json(force=True, silent=True) or {}
    c = cfg_mod.load_config()
    for k, v in data.items():
        if k == "app_version":
            continue
        c[k] = v
    c = cfg_mod.sanitize_config(c)
    cfg_mod.save_config(c)
    c["app_version"] = cfg_mod.APP_VERSION
    return jsonify(c)


@app.route("/api/pick_folder", methods=["POST"])
def pick_folder():
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        folder = filedialog.askdirectory(title="Выберите папку для сохранения треков")
        root.destroy()
        if folder:
            return jsonify({"ok": True, "path": os.path.abspath(folder)})
        return jsonify({"ok": False, "canceled": True})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/scan", methods=["POST"])
def scan():
    c = cfg_mod.load_config()
    if status.status["running"]:
        return jsonify({"ok": False, "message": "Scan already running"}), 409
    threading.Thread(target=monitor.run_scan, args=(c,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def stop_scan():
    status.status["stop_requested"] = True
    status.log.info("⏹ Запрошена принудительная остановка сканирования...")
    return jsonify({"ok": True})


@app.route("/api/status", methods=["GET"])
def get_status():
    payload = {
        "status": status.status,
        "logs": status.get_logs(),
        "jobs": jobs_mod.list_jobs(include_result=False),
        "app_version": cfg_mod.APP_VERSION,
    }
    return jsonify(payload)


@app.route("/api/jobs/<job_id>", methods=["GET"])
def get_job(job_id):
    job = jobs_mod.get_job(job_id)
    if not job:
        return jsonify({"ok": False, "message": "Job not found"}), 404
    return jsonify({"ok": True, "job": job})


@app.route("/api/tracks", methods=["GET"])
def get_tracks():
    return jsonify(db.get_all_tracks())


@app.route("/api/test_spotify", methods=["POST"])
def test_spotify():
    c = cfg_mod.load_config()
    try:
        proxy = (c.get("proxy") or "").strip() or None
        sp = sp_mod.SpotifyClient(c["spotify_client_id"], c["spotify_client_secret"], proxy=proxy)
        sp._auth()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@app.route("/api/artist_search", methods=["POST"])
def artist_search():
    """Search artists in the configured source, or resolve a pasted ID/URL.
    Safely searches both platforms without throwing a 403, and fetches both Spotify ID
    and Deezer ID for each result, so the user can select whether to monitor by Spotify or Deezer."""
    data = request.get_json(force=True, silent=True) or {}
    q = (data.get("query") or "").strip()
    if not q:
        return jsonify([])
    try:
        c = cfg_mod.load_config()
        kind, val = src_mod.parse_input(q)
        
        proxy = (c.get("proxy") or "").strip() or None
        ds = src_mod.DeezerSource(proxy=proxy)
        mb = src_mod.MusicBrainzSource(proxy=proxy)
        sp = None
        cid = (c.get("spotify_client_id") or "").strip()
        csec = (c.get("spotify_client_secret") or "").strip()
        if cid and csec:
            try:
                sp = sp_mod.SpotifyClient(cid, csec, proxy=proxy)
            except Exception as e:
                log.warning("Spotify client init failed during search: %s", e)
                sp = None

        results = []
        
        if kind == "spotify_id":
            spotify_id = val
            name = None
            followers = 0
            link = f"https://open.spotify.com/artist/{val}"
            
            if sp:
                try:
                    sa = sp.get_artist(val)
                    name = sa.get("name")
                    followers = sa.get("followers", 0)
                    link = sa.get("link", link)
                except Exception as e:
                    log.warning("Spotify get_artist failed for ID %s: %s", val, e)
            
            from core import catalog as cat_mod
            if not cat_mod.is_real_artist_name(name, val):
                try:
                    name = src_mod.resolve_spotify_name(val)
                except Exception as e:
                    log.warning("Spotify name resolve failed for ID %s: %s", val, e)
                    name = name if cat_mod.is_real_artist_name(name, val) else None
            if not cat_mod.is_real_artist_name(name, val):
                try:
                    from core import yandex as ya_mod
                    # last resort: cannot search YM by spotify id
                    name = cat_mod.resolve_public_artist_name(val) or name
                except Exception:
                    pass
            if not cat_mod.is_real_artist_name(name, val):
                name = val
            
            deezer_id = None
            if name:
                try:
                    da_list = ds.search_artists(name, limit=1)
                    if da_list:
                        deezer_id = da_list[0]["id"]
                except Exception as e:
                    log.warning("Deezer search failed during Spotify ID resolve: %s", e)
                    
            results.append({
                "id": spotify_id,
                "name": name,
                "spotify_id": spotify_id,
                "deezer_id": deezer_id,
                "followers": followers,
                "link": link,
                "spotify_name": name
            })
            
        elif kind == "deezer_id":
            deezer_id = val
            name = None
            followers = 0
            try:
                da = ds.get_artist(val)
                name = da.get("name")
                followers = da.get("followers", 0)
            except Exception as e:
                log.warning("Deezer get_artist failed for ID %s: %s", val, e)
                name = val
                
            spotify_id = None
            link = f"https://www.deezer.com/artist/{val}"
            
            if sp and name:
                try:
                    sa_list = sp.search_artists(name, limit=1)
                    if sa_list:
                        spotify_id = sa_list[0]["id"]
                        followers = sa_list[0].get("followers", 0)
                        link = sa_list[0].get("link", link)
                except Exception as e:
                    log.warning("Spotify search failed during Deezer ID resolve: %s", e)
                    
            results.append({
                "id": deezer_id,
                "name": name,
                "spotify_id": spotify_id,
                "deezer_id": deezer_id,
                "followers": followers,
                "link": link,
                "spotify_name": name if spotify_id else None
            })
            
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            from core import yandex as ya_mod

            def _dz():
                return [("deezer", x) for x in (ds.search_artists(q, limit=8) or [])]

            def _ya():
                ya = ya_mod.YandexSource(token=(c.get("yandex_token") or "").strip() or None)
                return [("yandex", x) for x in (ya.search_artists(q, limit=8) or [])]

            def _sp():
                if not sp:
                    return []
                return [("spotify", x) for x in (sp.search_artists(q, limit=8) or [])]

            def _zv():
                from core import zvuk as zv_mod
                zv = zv_mod.ZvukSource(token=(c.get("zvuk_token") or "").strip() or None, proxy=proxy)
                return [("zvuk", x) for x in (zv.search_artists(q, limit=8) or [])]

            buckets = {"spotify": [], "yandex": [], "deezer": [], "zvuk": []}
            with ThreadPoolExecutor(max_workers=4) as ex:
                futs = [ex.submit(_dz), ex.submit(_ya), ex.submit(_sp), ex.submit(_zv)]
                try:
                    for fut in as_completed(futs, timeout=6):
                        try:
                            for kind_hit, item in fut.result() or []:
                                buckets[kind_hit].append(item)
                        except Exception as e:
                            log.warning("Search worker failed for %s: %s", q, e)
                except Exception:
                    pass

            seen = {}
            def _add(item, via, sid=None, did=None, yid=None, zvid=None):
                nm = (item.get("name") or "").strip()
                if not nm:
                    return
                key = "".join(ch for ch in nm.casefold() if ch.isalnum())
                if not key:
                    return
                if key in seen:
                    row = seen[key]
                    if sid and not row.get("spotify_id"):
                        row["spotify_id"] = sid
                        row["spotify_name"] = nm
                        row["link"] = item.get("link") or f"https://open.spotify.com/artist/{sid}"
                    if did and not row.get("deezer_id"):
                        row["deezer_id"] = did
                    if yid and not row.get("yandex_id"):
                        row["yandex_id"] = yid
                    if zvid and not row.get("zvuk_id"):
                        row["zvuk_id"] = zvid
                    if item.get("followers") and (item.get("followers") or 0) > (row.get("followers") or 0):
                        row["followers"] = item.get("followers") or 0
                    if via in ("yandex", "zvuk") and not row.get("via"):
                        row["via"] = via
                    return
                row = {
                    "id": sid or yid or did or zvid or item.get("id"),
                    "name": nm,
                    "spotify_id": sid,
                    "deezer_id": did,
                    "yandex_id": yid,
                    "zvuk_id": zvid,
                    "followers": item.get("followers", 0) or 0,
                    "link": item.get("link") or (
                        f"https://open.spotify.com/artist/{sid}" if sid
                        else (f"https://music.yandex.ru/artist/{str(yid).replace('ya-', '')}" if yid
                              else (f"https://zvuk.com/artist/{zvid}" if zvid
                                    else (f"https://www.deezer.com/artist/{did}" if did else None)))
                    ),
                    "spotify_name": nm if sid else None,
                    "via": via,
                }
                seen[key] = row
                results.append(row)

            for sa in buckets["spotify"]:
                _add(sa, "spotify", sid=sa.get("id"))
            for ya_a in buckets["yandex"]:
                _add(ya_a, "yandex", yid=ya_a.get("id"))
            for zv_a in buckets["zvuk"]:
                _add(zv_a, "zvuk", zvid=zv_a.get("id"))
            for da in buckets["deezer"]:
                _add(da, "deezer", did=da.get("id"))
                    
        return jsonify(results)
    except Exception as e:
        log.exception("Error in artist_search")
        return jsonify({"error": str(e)}), 500


@app.route("/api/library", methods=["GET"])
def get_library():
    resp = jsonify(lib_mod.load_library())
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


def _background_metadata_update(only_name=None):
    """Return a zero-arg callable that resolves one artist (or the whole
    library) and persists results. Runs inside the background job worker."""
    def _run():
        c = cfg_mod.load_config()
        lib_mod.update_library_metadata(c, only_name=only_name)
    return _run


def _background_files_check():
    """Zero-arg callable that re-checks files on disk and auto-sorts folders."""
    def _run():
        c = cfg_mod.load_config()
        lib_mod.check_library_files(c)
    return _run


@app.route("/api/library/update", methods=["POST"])
def update_library():
    data = request.get_json(force=True, silent=True) or {}
    only = (data.get("artist") or request.args.get("artist") or "").strip() or None
    if status.status["running"]:
        return jsonify({"ok": False, "message": "Scan already running"}), 409
    job_id = jobs_mod.submit(
        "resolve", only or "", _background_metadata_update(only_name=only)
    )
    return jsonify({"ok": True, "background": True, "job_id": job_id})


@app.route("/api/library/check", methods=["POST"])
def check_library():
    if status.status["running"]:
        return jsonify({"ok": False, "message": "Scan already running"}), 409
    job_id = jobs_mod.submit("files", "", _background_files_check())
    return jsonify({"ok": True, "background": True, "job_id": job_id})


# ---------------------------------------------------------------------------
# Downloader self-check with live progress (progress bar in the UI)
# ---------------------------------------------------------------------------
_dl_checks = {}
_dl_check_lock = threading.Lock()
_dl_check_running = {"run_id": None}


@app.route("/api/downloader/check/start", methods=["POST"])
def downloader_check_start():
    """Start the downloader self-test in the background and return immediately.

    The old ``/api/youtube/check`` blocked the HTTP request for up to 45 s with
    no feedback; this returns a run_id and the UI polls for stage/percent.
    """
    from core import youtube as yt_mod
    data = request.get_json(force=True, silent=True) or {}
    try:
        timeout = max(10, min(180, int(data.get("timeout") or 45)))
    except (TypeError, ValueError):
        timeout = 45

    with _dl_check_lock:
        existing = _dl_check_running.get("run_id")
        if existing and _dl_checks.get(existing, {}).get("status") == "running":
            return jsonify({"ok": True, "run_id": existing, "already_running": True})
        run_id = uuid.uuid4().hex[:12]
        _dl_checks[run_id] = {
            "run_id": run_id,
            "status": "running",
            "stage_key": "prepare",
            "stage": "Подготовка",
            "percent": 0,
            "message": "",
            "result": None,
            "error": None,
            "started_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": None,
            "elapsed_s": 0.0,
            "timeout": timeout,
        }
        _dl_check_running["run_id"] = run_id
        # keep memory bounded
        if len(_dl_checks) > 10:
            for old in sorted(_dl_checks, key=lambda k: _dl_checks[k]["started_at"])[:len(_dl_checks) - 10]:
                _dl_checks.pop(old, None)

    cfg = cfg_mod.load_config()
    started = time.time()

    def _on_progress(p):
        with _dl_check_lock:
            st = _dl_checks.get(run_id)
            if not st:
                return
            st["stage_key"] = p.get("stage_key") or st["stage_key"]
            st["stage"] = p.get("stage") or st["stage"]
            if p.get("percent") is not None:
                st["percent"] = round(float(p["percent"]), 1)
            st["message"] = p.get("message") or ""
            st["elapsed_s"] = round(time.time() - started, 1)

    def _worker():
        try:
            res = yt_mod.downloader_check(cfg, timeout=timeout, on_progress=_on_progress)
            err = None
        except Exception as e:
            log.exception("Downloader check failed")
            res = {"ok": False, "test": {"ok": False, "message": str(e), "detail": ""}}
            err = str(e)
        with _dl_check_lock:
            st = _dl_checks.get(run_id)
            if st:
                st["status"] = "done"
                st["result"] = res
                st["error"] = err
                st["percent"] = 100
                st["finished_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                st["elapsed_s"] = round(time.time() - started, 1)
                ok = bool(res and res.get("ok") and (res.get("test") or {}).get("ok"))
                st["stage_key"] = "done" if ok else "failed"
                st["stage"] = "Готово" if ok else "Проверка не пройдена"
                st["message"] = ((res.get("test") or {}).get("message") or "") if res else ""

    threading.Thread(target=_worker, daemon=True, name="downloader-check-%s" % run_id).start()
    return jsonify({"ok": True, "run_id": run_id, "timeout": timeout})


@app.route("/api/downloader/check/state/<run_id>", methods=["GET"])
def downloader_check_state(run_id):
    with _dl_check_lock:
        st = _dl_checks.get(run_id)
        if not st:
            return jsonify({"ok": False, "message": "Проверка не найдена"}), 404
        return jsonify({"ok": True, "state": dict(st)})


@app.route("/api/youtube/check", methods=["POST"])
def youtube_check():
    """Live self-test of the download pipeline (yt-dlp + ffmpeg)."""
    from core import youtube as yt_mod
    c = cfg_mod.load_config()
    try:
        res = yt_mod.downloader_check(c)
        return jsonify({"ok": res.get("ok"), "result": res})
    except Exception as e:
        log.exception("YouTube check failed")
        return jsonify({"ok": False, "result": {"ok": False, "message": str(e)}}), 500


@app.route("/api/force_download", methods=["POST"])
def force_download():
    """Force-download a single track / whole album / whole artist, ignoring
    the skipped/no-match flags. Runs in a background job and reports the real
    per-track result (so the yt-dlp exit-code-1 error is surfaced to the user)."""
    data = request.get_json(force=True, silent=True) or {}
    artist_id = data.get("artist_id") or data.get("artist") or None
    album_id = data.get("album_id") or None
    track_id = data.get("track_id") or None
    if not artist_id and not album_id and not track_id:
        return jsonify({"ok": False, "message": "No target provided"}), 400
    if status.status["running"]:
        return jsonify({"ok": False, "message": "Scan already running"}), 409

    c = cfg_mod.load_config()
    tasks = lib_mod.collect_download_tasks(artist_id=artist_id, album_id=album_id, track_id=track_id)
    if not tasks:
        return jsonify({"ok": True, "background": False, "job_id": None,
                        "results": [], "message": "No pending tracks to force-download"})

    def _runner():
        return monitor.force_download_tracks(tasks, c)

    job_id = jobs_mod.submit("download", (data.get("name") or ""), _runner)
    return jsonify({"ok": True, "background": True, "job_id": job_id, "count": len(tasks)})


@app.route("/api/library/import_local", methods=["POST"])
def import_local_folders():
    try:
        c = cfg_mod.load_config()
        save_folder = c.get("save_folder", "downloads")
        if not os.path.exists(save_folder):
            return jsonify({"ok": False, "message": f"Save folder not found: {save_folder}"}), 400
            
        existing_artist_names = {str(a.get("name") or "").lower().strip() for a in c.get("artists", [])}
        
        # Scan for all leaf directories containing mp3 files or loose "Artist - Track.mp3" files
        leaf_dirs = [] # list of parts
        loose_artist_candidates = [] # list of tuples: (artist_name, genre_path)
        
        for root, dirs, files in os.walk(save_folder):
            mp3s = [f for f in files if f.lower().endswith(".mp3")]
            if mp3s:
                rel = os.path.relpath(root, save_folder)
                parts = [p for p in rel.replace("\\", "/").split("/") if p and p != "."]
                
                # Check for loose files with format "Artist - Track.mp3"
                has_loose_files = False
                for f in mp3s:
                    if " - " in f:
                        artist_part = f.split(" - ", 1)[0].strip()
                        if artist_part and len(artist_part) > 1:
                            gpath = "/".join(parts) if parts else ""
                            loose_artist_candidates.append((artist_part, gpath))
                            has_loose_files = True
                            
                # If no loose "Artist - Track" files found, treat directories as artist/album hierarchy
                if not has_loose_files and parts:
                    leaf_dirs.append(parts)
                    
        # Merge all tasks
        tasks = []
        for p in leaf_dirs:
            if p not in tasks:
                tasks.append(p)
        for cand in loose_artist_candidates:
            if cand not in tasks:
                tasks.append(cand)
                
        if not tasks:
            return jsonify({"ok": True, "added": 0, "message": "No music files (.mp3) found on disk."})
            
        # We will resolve candidates in a thread pool
        from concurrent.futures import ThreadPoolExecutor, as_completed
        ds = src_mod.DeezerSource()
        new_artists_to_append = []
        seen_resolved = set()
        
        def resolve_artist_task(item):
            # Case 1: Loose artist name and genre path candidate
            if isinstance(item, tuple):
                name, gpath = item
                try:
                    da_list = ds.search_artists(name, limit=1)
                    if da_list:
                        official_name = da_list[0]["name"]
                        return {
                            "id": da_list[0]["id"],
                            "name": official_name,
                            "source": "deezer",
                            "spotify_name": None,
                            "spotify_id": None,
                            "deezer_id": da_list[0]["id"],
                            "genre_path": gpath
                        }
                except Exception:
                    pass
                return {
                    "id": None,
                    "name": name,
                    "source": "deezer",
                    "spotify_name": None,
                    "spotify_id": None,
                    "deezer_id": None,
                    "genre_path": gpath
                }
                
            # Case 2: Folder path parts list
            parts = item
            # Candidate A: standard "Artist/Album" folder hierarchy
            if len(parts) >= 2:
                cand_a = parts[-2]
                gpath_a = "/".join(parts[:-2])
                try:
                    da_list = ds.search_artists(cand_a, limit=1)
                    if da_list:
                        official_name = da_list[0]["name"]
                        if official_name.lower().strip() == cand_a.lower().strip() or cand_a.lower().strip() in official_name.lower():
                            return {
                                "id": da_list[0]["id"],
                                "name": official_name,
                                "source": "deezer",
                                "spotify_name": None,
                                "spotify_id": None,
                                "deezer_id": da_list[0]["id"],
                                "genre_path": gpath_a
                            }
                except Exception:
                    pass
            
            # Candidate B: tracks are saved directly in "Artist" folder (no album folder)
            cand_b = parts[-1]
            gpath_b = "/".join(parts[:-1])
            try:
                da_list = ds.search_artists(cand_b, limit=1)
                if da_list:
                    official_name = da_list[0]["name"]
                    if official_name.lower().strip() == cand_b.lower().strip() or cand_b.lower().strip() in official_name.lower():
                        return {
                            "id": da_list[0]["id"],
                            "name": official_name,
                            "source": "deezer",
                            "spotify_name": None,
                            "spotify_id": None,
                            "deezer_id": da_list[0]["id"],
                            "genre_path": gpath_b
                        }
            except Exception:
                pass
            
            # Fallback if search didn't find any clean match
            if len(parts) >= 2:
                return {
                    "id": None,
                    "name": parts[-2],
                    "source": "deezer",
                    "spotify_name": None,
                    "spotify_id": None,
                    "deezer_id": None,
                    "genre_path": "/".join(parts[:-2])
                }
            else:
                return {
                    "id": None,
                    "name": parts[-1],
                    "source": "deezer",
                    "spotify_name": None,
                    "spotify_id": None,
                    "deezer_id": None,
                    "genre_path": ""
                }

        # Resolve all candidates in parallel
        added_count = 0
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(resolve_artist_task, t): t for t in tasks}
            for fut in as_completed(futures):
                try:
                    res = fut.result()
                    if res:
                        key = res["name"].lower().strip()
                        if key and key not in existing_artist_names and key not in seen_resolved:
                            seen_resolved.add(key)
                            new_artists_to_append.append(res)
                            added_count += 1
                except Exception:
                    pass
                    
        if added_count > 0:
            c["artists"].extend(new_artists_to_append)
            c = cfg_mod.sanitize_config(c)
            cfg_mod.save_config(c)
            
            # Sync metadata of new artists into library
            lib = lib_mod.update_library_metadata(c)
        else:
            lib = lib_mod.load_library()
            
        return jsonify({"ok": True, "added": added_count, "library": lib})
        
    except Exception as e:
        log.exception("Failed to import local folders")
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/library/toggle_ignore", methods=["POST"])
def toggle_library_ignore():
    data = request.get_json(force=True, silent=True) or {}
    artist_id = data.get("artist_id")
    album_id = data.get("album_id")
    track_id = data.get("track_id")
    if not artist_id:
        return jsonify({"ok": False, "message": "Missing artist_id"}), 400
    try:
        lib = lib_mod.toggle_ignore(artist_id, album_id, track_id)
        return jsonify({"ok": True, "library": lib})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/library/reset_filter_errors", methods=["POST"])
def reset_filter_errors():
    try:
        lib = lib_mod.reset_filter_errors()
        return jsonify({"ok": True, "library": lib})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500

@app.route("/api/translations", methods=["GET"])
def list_translations():
    folder = os.path.join(BASE_DIR, "data", "translations")
    os.makedirs(folder, exist_ok=True)
    files = [f[:-4] for f in os.listdir(folder) if f.endswith(".txt")]
    return jsonify(sorted(files))


@app.route("/api/translations/<lang>", methods=["GET"])
def get_translation(lang):
    # Sanitize lang string to prevent traversal
    lang = "".join(c for c in lang if c.isalnum() or c in "-_")
    path = os.path.join(BASE_DIR, "data", "translations", f"{lang}.txt")
    if not os.path.exists(path):
        return jsonify({"error": "Translation file not found"}), 404
        
    translations = {}
    with open(path, "r", encoding="utf-8") as f:
        current_key = None
        current_val = []
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if ":" in line and not line.startswith(" "):
                # Save previous
                if current_key:
                    translations[current_key] = "\n".join(current_val).replace("\\n", "\n")
                parts = line.split(":", 1)
                current_key = parts[0].strip()
                current_val = [parts[1].strip()]
            else:
                if current_key:
                    current_val.append(line.rstrip("\r\n"))
        # Save last
        if current_key:
            translations[current_key] = "\n".join(current_val).replace("\\n", "\n")
            
    return jsonify(translations)


# ---------------------------------------------------------------------------
# yt-dlp VPN/proxy tester — compare several VPN configs against YouTube
# ---------------------------------------------------------------------------
_vpn_runs = {}
_vpn_lock = threading.Lock()
_VPN_MAX_RUNS = 20
_VPN_LOG_MAX = 400


@app.route("/vpn-test")
def vpn_test_page():
    return render_template("vpn_test.html")


@app.route("/api/vpn-test/env", methods=["GET"])
def vpn_test_env():
    from core import vpn_probe as vp_mod
    env = vp_mod.env_report()
    c = cfg_mod.load_config()
    return jsonify({
        "ok": True,
        "env": env,
        "hints": vp_mod.env_hints(env),
        "config_proxy": vp_mod.normalize_proxy(c.get("proxy")) or "",
        "app_version": cfg_mod.APP_VERSION,
    })


def _vpn_coerce_extra(raw):
    """Normalize the UI's extra profile objects (proxy strings or dicts)."""
    from core import vpn_probe as vp_mod
    out = []
    for item in raw or []:
        if isinstance(item, str):
            proxy = vp_mod.normalize_proxy(item)
            out.append({"name": vp_mod.proxy_label({"proxy": proxy}), "proxy": proxy, "notes": ""})
            continue
        if isinstance(item, dict) and (item.get("proxy") is not None or item.get("name")):
            p = vp_mod._coerce_profile(item)
            if p:
                out.append(p)
    return out


@app.route("/api/vpn-test/start", methods=["POST"])
def vpn_test_start():
    """Kick off a VPN test run in a background thread (never blocks the app)."""
    from core import vpn_probe as vp_mod
    data = request.get_json(force=True, silent=True) or {}

    if status.status.get("running") and not data.get("force"):
        return jsonify({"ok": False,
                        "message": "Идёт сканирование — остановите его или включите "
                                   "«Запустить всё равно» (иначе YouTube может выдать 429)."}), 409

    profiles, file_targets = vp_mod.parse_profile_spec(data.get("profiles_text") or "")
    profiles.extend(_vpn_coerce_extra(data.get("profiles")))
    if data.get("add_config_proxy"):
        cfg_proxy = vp_mod.normalize_proxy((cfg_mod.load_config() or {}).get("proxy"))
        profiles.append({"name": "прокси из настроек MuseNest" if cfg_proxy else "direct (настройки MuseNest)",
                         "proxy": cfg_proxy, "notes": ""})
    if data.get("add_direct"):
        profiles.append({"name": "direct (системный маршрут)", "proxy": None, "notes": ""})
    profiles = vp_mod.dedupe_profiles(profiles)
    if not profiles:
        return jsonify({"ok": False, "message": "Не задано ни одного профиля VPN"}), 400

    targets = [str(t).strip() for t in (data.get("targets") or []) if str(t).strip()]
    targets += file_targets
    targets = targets or [vp_mod.DEFAULT_TARGET]

    opts = data.get("options") or {}
    clients = [str(c).strip() for c in (opts.get("clients") or "").split(",") if str(c).strip()]
    options = {
        "timeout": float(opts.get("timeout") or 25),
        "cookies": (opts.get("cookies") or "auto") if opts.get("cookies") in ("auto", "none") else "auto",
        "ip": bool(opts.get("ip", True)),
        "reach": bool(opts.get("reach", True)),
        "extract": bool(opts.get("extract", True)),
        "search": bool(opts.get("search", True)),
        "download": bool(opts.get("download", True)),
        "clients": clients or None,
        "mp3": bool(opts.get("mp3", False)),
        "throttle_kbps": float(opts.get("throttle_kbps") or 60),
        "search_query": opts.get("search_query") or vp_mod.DEFAULT_SEARCH,
        "search_limit": int(opts.get("search_limit") or 5),
        "download_url": (opts.get("download_url") or "").strip() or None,
    }

    run_id = uuid.uuid4().hex[:12]
    state = {
        "run_id": run_id,
        "label": str(data.get("label") or "")[:120],
        "status": "running",
        "started_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": None,
        "elapsed_s": 0.0,
        "total": len(profiles),
        "done": 0,
        "current": profiles[0]["name"],
        "env": vp_mod.env_report(),
        "targets": targets,
        "options": options,
        "log": [],
        "results": [],
        "error": None,
        "saved_to": None,
    }
    with _vpn_lock:
        _vpn_runs[run_id] = state
        # keep memory bounded
        if len(_vpn_runs) > _VPN_MAX_RUNS:
            for old in sorted(_vpn_runs, key=lambda k: _vpn_runs[k]["started_at"])[:len(_vpn_runs) - _VPN_MAX_RUNS]:
                _vpn_runs.pop(old, None)

    cfg = cfg_mod.load_config()
    threading.Thread(target=_vpn_run_worker, args=(run_id, profiles, targets, options, cfg),
                     daemon=True, name="vpn-test-%s" % run_id).start()
    return jsonify({"ok": True, "run_id": run_id,
                    "profiles": [p["name"] for p in profiles], "targets": targets})


def _vpn_log(run_id, name, message):
    with _vpn_lock:
        st = _vpn_runs.get(run_id)
        if not st:
            return
        st["log"].append({"ts": datetime.datetime.now().strftime("%H:%M:%S"),
                          "profile": name, "message": str(message)[:300]})
        if len(st["log"]) > _VPN_LOG_MAX:
            del st["log"][:len(st["log"]) - _VPN_LOG_MAX]


def _vpn_save(run_id):
    """Persist the run to data/vpn_tests/ (gitignored) for later comparison."""
    from core import vpn_probe as vp_mod
    with _vpn_lock:
        st = _vpn_runs.get(run_id)
        if not st:
            return None
        payload = {k: v for k, v in st.items() if k != "log"}
        payload["schema"] = vp_mod.PROBE_SCHEMA
    try:
        d = os.path.join(BASE_DIR, "data", "vpn_tests")
        os.makedirs(d, exist_ok=True)
        safe = "".join(c for c in (payload.get("label") or "") if c.isalnum() or c in "-_")
        path = os.path.join(d, "vpn_test_%s%s.json" % (
            datetime.datetime.now().strftime("%Y%m%d_%H%M%S"), ("_" + safe) if safe else ""))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        return path
    except Exception as e:
        log.warning("Не удалось сохранить отчёт VPN-теста: %s", e)
        return None


def _vpn_run_worker(run_id, profiles, targets, options, cfg):
    from core import vpn_probe as vp_mod
    started = time.time()
    try:
        for i, profile in enumerate(profiles):
            with _vpn_lock:
                st = _vpn_runs.get(run_id)
                if not st:
                    return
                st["current"] = profile["name"]
            _vpn_log(run_id, profile["name"], "проверка…")
            try:
                res = vp_mod.run_profile(profile, cfg=cfg, targets=targets, options=options,
                                        on_log=lambda name, msg: _vpn_log(run_id, name, msg))
            except Exception as e:
                log.exception("VPN probe failed for %s", profile.get("name"))
                res = {"name": profile.get("name"), "proxy": profile.get("proxy"),
                       "proxy_label": vp_mod.proxy_label(profile), "verdict": "error",
                       "verdict_text": vp_mod.VERDICTS["error"], "codes": ["exception"],
                       "hints": [str(e)], "elapsed_s": 0.0}
            with _vpn_lock:
                st = _vpn_runs.get(run_id)
                if not st:
                    return
                st["results"].append(res)
                st["done"] = i + 1
                st["current"] = ""
                st["elapsed_s"] = round(time.time() - started, 1)
            if i < len(profiles) - 1:
                time.sleep(2)  # be polite to YouTube between profiles
    except Exception as e:
        log.exception("VPN test run %s failed", run_id)
        with _vpn_lock:
            st = _vpn_runs.get(run_id)
            if st:
                st["status"] = "error"
                st["error"] = str(e)
        return
    saved = _vpn_save(run_id)
    with _vpn_lock:
        st = _vpn_runs.get(run_id)
        if st:
            st["status"] = "done"
            st["finished_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            st["elapsed_s"] = round(time.time() - started, 1)
            st["saved_to"] = saved


@app.route("/api/vpn-test/state/<run_id>", methods=["GET"])
def vpn_test_state(run_id):
    with _vpn_lock:
        st = _vpn_runs.get(run_id)
        if not st:
            return jsonify({"ok": False, "message": "Запуск не найден"}), 404
        payload = dict(st)
    return jsonify({"ok": True, "state": payload})


@app.route("/api/vpn-test/report/<run_id>", methods=["GET"])
def vpn_test_report(run_id):
    """Download the report as markdown (default) or JSON."""
    from core import vpn_probe as vp_mod
    fmt = (request.args.get("format") or "md").lower()
    with _vpn_lock:
        st = _vpn_runs.get(run_id)
        if not st:
            return jsonify({"ok": False, "message": "Запуск не найден"}), 404
        payload = dict(st)
    if fmt == "json":
        body = json.dumps({k: v for k, v in payload.items() if k != "log"},
                          ensure_ascii=False, indent=2)
        mime, ext = "application/json", "json"
    else:
        body = vp_mod.format_markdown_report(payload["results"], env=payload.get("env"),
                                             targets=payload.get("targets"),
                                             label=payload.get("label"))
        mime, ext = "text/markdown; charset=utf-8", "md"
    fname = "vpn_test_%s.%s" % (run_id, ext)
    return app.response_class(body, mimetype=mime, headers={
        "Content-Disposition": 'attachment; filename="%s"' % fname})


if __name__ == "__main__":
    threading.Thread(target=_scheduler, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))

    def _open_browser():
        time.sleep(1.5)
        try:
            webbrowser.open(f"http://127.0.0.1:{port}/")
        except Exception:
            pass

    threading.Thread(target=_open_browser, daemon=True).start()
    log.info("Starting server on http://127.0.0.1:%s/", port)
    try:
        # Bind to 0.0.0.0 so the host preview proxy can reach the app.
        app.run(host="0.0.0.0", port=port, threaded=True)
    except (KeyboardInterrupt, SystemExit):
        print("\n[✔] Сервер успешно остановлен.")
        sys.exit(0)

