import os
import sys
import threading
import webbrowser
import datetime
import logging
import time
import shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import config as cfg_mod
from core import spotify as sp_mod
from core import source as src_mod
from core import status, db, monitor, library as lib_mod
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
    return jsonify(cfg_mod.load_config())


@app.route("/api/settings", methods=["POST"])
def post_settings():
    data = request.get_json(force=True, silent=True) or {}
    c = cfg_mod.load_config()
    for k, v in data.items():
        c[k] = v
    c = cfg_mod.sanitize_config(c)
    cfg_mod.save_config(c)
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
    return jsonify({"status": status.status, "logs": status.get_logs()})


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
        src_name = c.get("music_source", "deezer")
        kind, val = src_mod.parse_input(q)
        
        proxy = (c.get("proxy") or "").strip() or None
        ds = src_mod.DeezerSource(proxy=proxy)
        sp = None
        cid = (c.get("spotify_client_id") or "").strip()
        csec = (c.get("spotify_client_secret") or "").strip()
        if cid and csec:
            try:
                sp = sp_mod.SpotifyClient(cid, csec, proxy=proxy)
                sp._auth()
            except Exception as e:
                log.warning("Spotify Client auth failed during search init: %s", e)
                sp = None

        # Working keys → Spotify catalog first, even if UI source is still Deezer.
        prefer_spotify = bool(sp)
                
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
            
            if not name:
                try:
                    name = src_mod.resolve_spotify_name(val)
                except Exception as e:
                    log.warning("Spotify oEmbed resolve failed for ID %s: %s", val, e)
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
            # With working Spotify keys always search Spotify first.
            # Global "deezer" source used to skip this and never store spotify_id.
            if sp and prefer_spotify:
                try:
                    sp_results = sp.search_artists(q, limit=8)
                    for sa in sp_results:
                        s_id = sa["id"]
                        s_name = sa["name"]
                        d_id = None
                        try:
                            da_list = ds.search_artists(s_name, limit=1)
                            if da_list:
                                d_id = da_list[0]["id"]
                        except Exception:
                            pass
                        results.append({
                            "id": s_id,
                            "name": s_name,
                            "spotify_id": s_id,
                            "deezer_id": d_id,
                            "followers": sa.get("followers", 0),
                            "link": sa.get("link"),
                            "spotify_name": s_name,
                        })
                except Exception as e:
                    log.warning("Spotify search failed for query %s: %s. Falling back to Deezer.", q, e)

            if not results:
                try:
                    dz_results = ds.search_artists(q, limit=8)
                    for da in dz_results:
                        d_id = da["id"]
                        d_name = da["name"]
                        s_id = None
                        followers = da.get("followers", 0)
                        link = f"https://www.deezer.com/artist/{d_id}"
                        if sp:
                            try:
                                s_id = lib_mod._match_spotify_id(sp, d_name)
                                if s_id:
                                    sa = sp.get_artist(s_id)
                                    followers = sa.get("followers", followers)
                                    link = sa.get("link", link)
                            except Exception:
                                pass
                        results.append({
                            "id": d_id,
                            "name": d_name,
                            "spotify_id": s_id,
                            "deezer_id": d_id,
                            "followers": followers,
                            "link": link,
                            "spotify_name": d_name if s_id else None,
                        })
                except Exception as e:
                    log.error("Deezer search failed for query %s: %s", q, e)
                    return jsonify({"error": str(e)}), 500
                    
        return jsonify(results)
    except Exception as e:
        log.exception("Error in artist_search")
        return jsonify({"error": str(e)}), 500


@app.route("/api/library", methods=["GET"])
def get_library():
    resp = jsonify(lib_mod.load_library())
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.route("/api/library/update", methods=["POST"])
def update_library():
    c = cfg_mod.load_config()
    data = request.get_json(force=True, silent=True) or {}
    only = (data.get("artist") or request.args.get("artist") or "").strip() or None
    try:
        lib = lib_mod.update_library_metadata(c, only_name=only)
        return jsonify({"ok": True, "library": lib})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/library/check", methods=["POST"])
def check_library():
    c = cfg_mod.load_config()
    try:
        lib = lib_mod.check_library_files(c)
        return jsonify({"ok": True, "library": lib})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


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
        app.run(host="127.0.0.1", port=port, threaded=True)
    except (KeyboardInterrupt, SystemExit):
        print("\n[✔] Сервер успешно остановлен.")
        sys.exit(0)

