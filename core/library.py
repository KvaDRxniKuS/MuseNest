import os
import json
import logging
from . import config as cfg_mod
from . import source as src_mod
from . import status

_log = logging.getLogger("tracker")
_LIBRARY_PATH = os.path.join(cfg_mod.BASE_DIR, "data", "library.json")


def sanitize_name(name):
    bad = '<>:\"/\\|?*'
    out = []
    for ch in str(name):
        if ch in bad:
            continue
        out.append(ch)
    name = "".join(out).strip().rstrip(".")
    name = " ".join(name.split())
    return name[:180] or "untitled"


def _norm_artist_name(s):
    return " ".join("".join(ch for ch in str(s).casefold() if ch.isalnum() or ch.isspace()).split())


def _match_spotify_id(spotify_src, name):
    queries = []
    raw = (name or "").strip()
    if raw:
        queries.append(raw)
    compact = _norm_artist_name(raw)
    if compact and compact not in queries:
        queries.append(compact)
    best = None
    for q in queries:
        try:
            results = spotify_src.search_artists(q, limit=10)
        except Exception:
            continue
        if not results:
            continue
        nq = _norm_artist_name(q)
        for r in results:
            rn = _norm_artist_name(r.get("name") or "")
            if rn == nq:
                return r.get("id")
        if best is None:
            best = results[0].get("id")
    return best


def load_library():
    if not os.path.exists(_LIBRARY_PATH):
        return {"artists": []}
    try:
        with open(_LIBRARY_PATH, "r", encoding="utf-8") as f:
            lib = json.load(f)
            
        # Filter to only show artists that exist in config.json
        try:
            from . import config as cfg_mod
            cfg = cfg_mod.load_config()
            cfg_artist_names = {str(a.get("name") or "").lower().strip() for a in cfg.get("artists", []) if isinstance(a, dict)}
            
            filtered_artists = []
            changed = False
            for art in lib.get("artists", []):
                name = str(art.get("name") or "").lower().strip()
                if name in cfg_artist_names:
                    filtered_artists.append(art)
                else:
                    changed = True
            
            if changed:
                lib["artists"] = filtered_artists
                with open(_LIBRARY_PATH, "w", encoding="utf-8") as f:
                    json.dump(lib, f, indent=2, ensure_ascii=False)
        except Exception as filter_err:
            _log.warning("Failed to filter library by config artists: %s", filter_err)
            
        return lib
    except Exception as e:
        _log.error("Failed to load library.json: %s", e)
        return {"artists": []}


import threading
_LIB_LOCK = threading.Lock()


def save_library(lib):
    try:
        with open(_LIBRARY_PATH, "w", encoding="utf-8") as f:
            json.dump(lib, f, indent=2, ensure_ascii=False)
    except Exception as e:
        _log.error("Failed to save library.json: %s", e)


def update_track_status(artist_name, album_name, track_id, downloaded=None, no_match=None, error_code=None):
    with _LIB_LOCK:
        lib = load_library()
        updated = False
        for art in lib.get("artists", []):
            if str(art.get("name", "")).lower().strip() == str(artist_name).lower().strip():
                for alb in art.get("albums", []):
                    if str(alb.get("name", "")).lower().strip() == str(album_name).lower().strip():
                        for trk in alb.get("tracks", []):
                            if str(trk.get("id")).strip() == str(track_id).strip():
                                if downloaded is not None:
                                    trk["downloaded"] = downloaded
                                if no_match is not None:
                                    trk["no_match"] = no_match
                                if error_code is not None:
                                    trk["error_code"] = error_code
                                updated = True
                                break
        if updated:
            save_library(lib)


def check_library_files(cfg, lib=None):
    if lib is None:
        lib = load_library()
    
    save_folder = cfg.get("save_folder", "downloads")
    import shutil
    
    for art in lib.get("artists", []):
        art_name = art.get("name", "")
        sanitized_art = sanitize_name(art_name)
        artist_dir = os.path.join(save_folder, sanitized_art)
        
        # Resolve genre_path from the artist
        genre_path = art.get("genre_path", "")
        if not genre_path:
            for a in cfg.get("artists", []):
                if isinstance(a, dict) and a.get("name") == art_name:
                    genre_path = a.get("genre_path", "")
                    break
                    
        # Construct active target path
        if genre_path:
            path_parts = [save_folder] + [p.strip() for p in genre_path.split("/") if p.strip()] + [sanitized_art]
            target_artist_dir = os.path.join(*path_parts)
        else:
            target_artist_dir = os.path.join(save_folder, sanitized_art)
        
        # 1. Map each normalized track name to its preferred physical album
        track_preferred_album = {}
        
        full_albums = [alb for alb in art.get("albums", []) if alb.get("album_type") == "album"]
        single_albums = [alb for alb in art.get("albums", []) if alb.get("album_type") == "single"]
        
        for alb in full_albums:
            alb_name = alb.get("name", "")
            for trk in alb.get("tracks", []):
                norm_trk_key = sanitize_name(trk.get("name", "")).lower()
                if norm_trk_key not in track_preferred_album:
                    track_preferred_album[norm_trk_key] = sanitize_name(alb_name)
                    
        for alb in single_albums:
            alb_name = alb.get("name", "")
            for trk in alb.get("tracks", []):
                norm_trk_key = sanitize_name(trk.get("name", "")).lower()
                if norm_trk_key not in track_preferred_album:
                    track_preferred_album[norm_trk_key] = sanitize_name(alb_name)
                    
        # 2. Build a lookup map of existing files currently on disk for this artist
        disk_files = {} # maps normalized_track_name.lower() -> physical_path
        
        # Check general save folder
        if os.path.exists(save_folder):
            for f in os.listdir(save_folder):
                if f.lower().endswith(".mp3") and f.lower().startswith(sanitized_art.lower() + " - "):
                    prefix_len = len(sanitized_art) + 3
                    t_name = f[prefix_len:-4]
                    disk_files[sanitize_name(t_name).lower()] = os.path.join(save_folder, f)
                    
        # Check old artist subfolder
        if os.path.exists(artist_dir):
            for root, dirs, files in os.walk(artist_dir):
                for f in files:
                    if f.lower().endswith(".mp3"):
                        t_name = f[:-4]
                        if t_name.lower().startswith(sanitized_art.lower() + " - "):
                            t_name = t_name[len(sanitized_art) + 3:]
                        disk_files[sanitize_name(t_name).lower()] = os.path.join(root, f)
                        
        # Check new nested target artist subfolder
        if target_artist_dir != artist_dir and os.path.exists(target_artist_dir):
            for root, dirs, files in os.walk(target_artist_dir):
                for f in files:
                    if f.lower().endswith(".mp3"):
                        t_name = f[:-4]
                        if t_name.lower().startswith(sanitized_art.lower() + " - "):
                            t_name = t_name[len(sanitized_art) + 3:]
                        disk_files[sanitize_name(t_name).lower()] = os.path.join(root, f)
                        
        # 3. For each track in each album/single:
        # Check if it exists anywhere on disk.
        # If yes, downloaded = True. If it is not in its preferred folder, MOVE it!
        # If no, downloaded = False.
        for alb in art.get("albums", []):
            alb_name = alb.get("name", "")
            
            for trk in alb.get("tracks", []):
                trk_name = trk.get("name", "")
                trk_id = trk.get("id")
                sanitized_trk = sanitize_name(trk_name)
                norm_key = sanitized_trk.lower()
                
                if norm_key in disk_files:
                    trk["downloaded"] = True
                    current_path = disk_files[norm_key]
                    
                    pref_alb_name = track_preferred_album.get(norm_key, sanitize_name(alb_name))
                    target_path = os.path.join(target_artist_dir, pref_alb_name, sanitized_trk + ".mp3")
                    
                    if os.path.exists(current_path) and os.path.abspath(current_path) != os.path.abspath(target_path):
                        try:
                            # Make source path absolute for reliable comparison
                            src_abs = os.path.abspath(current_path)
                            dst_abs = os.path.abspath(target_path)
                            if os.path.exists(src_abs) and src_abs != dst_abs:
                                os.makedirs(os.path.dirname(dst_abs), exist_ok=True)
                                shutil.move(src_abs, dst_abs)
                                if not os.path.exists(dst_abs):
                                    _log.error("Migration failed: %s -> %s (file not at destination)", src_abs, dst_abs)
                                else:
                                    status.log.info("Авто-сортировка: перенесён трек '%s' из '%s' в приоритетный альбом '%s'",
                                                trk_name, os.path.basename(os.path.dirname(src_abs)), pref_alb_name)
                                    disk_files[norm_key] = dst_abs
                                    # Clean up old folder if empty
                                    old_dir = os.path.dirname(src_abs)
                                    if os.path.exists(old_dir) and os.path.abspath(old_dir) != os.path.abspath(artist_dir) and os.path.abspath(old_dir) != os.path.abspath(target_artist_dir):
                                        try:
                                            remaining = [x for x in os.listdir(old_dir) if not x.startswith('.')]
                                            if not remaining:
                                                os.rmdir(old_dir)
                                                status.log.info("Удалена опустевшая папка сингла/дубликата: '%s'", os.path.basename(old_dir))
                                        except Exception:
                                            pass
                        except Exception as me:
                            _log.warning("Failed to auto-migrate old file %s -> %s: %s", current_path, target_path, me)
                else:
                    trk["downloaded"] = False
                    
    save_library(lib)
    return lib


def update_library_metadata(cfg, only_name=None):
    lib = load_library()
    
    # Store existing ignored, no_match, and error_code status to preserve them
    ignored_artists = {}
    ignored_albums = {}
    ignored_tracks = {}
    no_match_tracks = {}
    error_code_tracks = {}
    
    for art in lib.get("artists", []):
        art_id = str(art.get("id") or art.get("name")).strip()
        ignored_artists[art_id] = art.get("ignored", False)
        for alb in art.get("albums", []):
            alb_id = str(alb.get("id")).strip()
            ignored_albums[alb_id] = alb.get("ignored", False)
            for trk in alb.get("tracks", []):
                trk_id = str(trk.get("id")).strip()
                ignored_tracks[trk_id] = trk.get("ignored", False)
                no_match_tracks[trk_id] = trk.get("no_match", False)
                error_code_tracks[trk_id] = trk.get("error_code")
                
    # Pre-build clients for efficiency
    proxy = (cfg.get("proxy") or "").strip() or None
    deezer_src = src_mod.DeezerSource(proxy=proxy)
    spotify_src = None
    cid = (cfg.get("spotify_client_id") or "").strip()
    csec = (cfg.get("spotify_client_secret") or "").strip()
    if cid and csec:
        try:
            import core.spotify as sp_mod
            spotify_src = sp_mod.SpotifyClient(cid, csec, proxy=proxy)
            spotify_src._auth()
        except Exception as e:
            _log.warning("Failed to initialize Spotify client for metadata update: %s", e)
            
    fallback_src = src_mod.MultiFallbackSource(cfg)
    
    new_artists = []
    resolved_artists_for_config = []
    only = (only_name or "").strip().lower()
    
    for artist_entry in cfg.get("artists", []):
        if isinstance(artist_entry, dict):
            entry_id = artist_entry.get("id")
            entry_name = artist_entry.get("name", "")
            spotify_name = artist_entry.get("spotify_name")
            entry_source = artist_entry.get("source", "deezer")
            saved_spotify_id = artist_entry.get("spotify_id")
            saved_deezer_id = artist_entry.get("deezer_id")
        else:
            entry_id = None
            entry_name = str(artist_entry)
            spotify_name = None
            entry_source = "deezer"
            saved_spotify_id = None
            saved_deezer_id = None

        if only:
            names = {str(entry_name or "").strip().lower()}
            if spotify_name:
                names.add(str(spotify_name).strip().lower())
            if only not in names:
                continue
            
        # Parse entry_name to see if it is a link or raw ID
        parsed_kind, parsed_val = src_mod.parse_input(entry_name)
        if parsed_kind == "spotify_id":
            saved_spotify_id = parsed_val
            resolved_name = None
            if spotify_src:
                try:
                    sa = spotify_src.get_artist(parsed_val)
                    resolved_name = sa.get("name")
                except Exception:
                    pass
            if not resolved_name:
                try:
                    resolved_name = src_mod.resolve_spotify_name(parsed_val)
                except Exception:
                    pass
            if resolved_name:
                entry_name = resolved_name
        elif parsed_kind == "deezer_id":
            saved_deezer_id = parsed_val
            try:
                da = deezer_src.get_artist(parsed_val)
                entry_name = da.get("name", entry_name)
            except Exception:
                pass

        # Resolve missing Spotify ID (exact name first, then closest)
        if not saved_spotify_id and spotify_src:
            try:
                saved_spotify_id = _match_spotify_id(spotify_src, entry_name)
            except Exception as e:
                _log.warning("Spotify search failed for %s: %s", entry_name, e)
                
        # Resolve missing Deezer ID
        if not saved_deezer_id:
            try:
                da_list = deezer_src.search_artists(entry_name, limit=1)
                if da_list:
                    saved_deezer_id = da_list[0]["id"]
            except Exception:
                pass

        # Select which active source client to use
        active_id = None
        active_src = fallback_src
        fallback_deezer = False
        fallback_reason = ""
        
        if entry_source == "spotify":
            if spotify_src and saved_spotify_id:
                active_id = saved_spotify_id
                active_src = spotify_src
            else:
                active_id = saved_deezer_id or entry_name
                active_src = fallback_src
                fallback_deezer = True
        else: # deezer
            active_id = saved_deezer_id or entry_name
            active_src = fallback_src

        # Get followers
        followers = 0
        if spotify_src and saved_spotify_id:
            try:
                sa = spotify_src.get_artist(saved_spotify_id)
                followers = sa.get("followers", 0)
            except Exception:
                pass
        if not followers and saved_deezer_id:
            try:
                da = deezer_src.get_artist(saved_deezer_id)
                followers = da.get("followers", 0)
            except Exception:
                pass

        spot_artist = spotify_name or entry_name
        status.status["current_stage"] = f"Сеть: разрешение артиста {spot_artist}..."

        albums = []
        max_albums = cfg.get("max_albums_per_artist", 99999)
        
        try:
            albums = active_src.get_albums(active_id, limit=max_albums)
        except Exception as e:
            _log.warning("Failed to fetch albums for %s: %s", spot_artist, e)
            
        artist_node = {
            "id": (saved_spotify_id or saved_deezer_id or spot_artist),
            "name": spot_artist,
            "source": entry_source,
            "spotify_id": saved_spotify_id,
            "deezer_id": saved_deezer_id,
            "followers": followers,
            "fallback_deezer": fallback_deezer,
            "fallback_reason": fallback_reason,
            "genre_path": artist_entry.get("genre_path", "") if isinstance(artist_entry, dict) else "",
            "ignored": ignored_artists.get(str(saved_spotify_id or saved_deezer_id or spot_artist).strip(), False),
            "albums": []
        }
        
        if albums:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            album_nodes_map = {}
            
            def fetch_album_tracks_task(alb):
                alb_id = alb["id"]
                alb_name = alb["name"]
                try:
                    tracks_data = active_src.get_album_tracks(alb_id)
                except Exception as e:
                    _log.debug("Failed to get tracks for album %s: %s", alb_name, e)
                    tracks_data = []
                return alb_id, alb_name, tracks_data

            status.status["current_stage"] = f"Сеть: {spot_artist} — подготовка сбора..."
            
            with ThreadPoolExecutor(max_workers=6) as executor:
                futures = {executor.submit(fetch_album_tracks_task, alb): alb for alb in albums}
                completed = 0
                for fut in as_completed(futures):
                    completed += 1
                    try:
                        alb_id, alb_name, tracks_data = fut.result()
                        status.status["current_stage"] = f"Сеть: {spot_artist} — сбор ({completed}/{len(albums)} альбомов)..."
                        
                        album_node = {
                            "id": alb_id,
                            "name": alb_name,
                            "album_type": next((a.get("album_type", "album") for a in albums if a["id"] == alb_id), "album"),
                            "ignored": ignored_albums.get(str(alb_id).strip(), False),
                            "tracks": []
                        }
                        
                        seen_tracks = set()
                        for trk in tracks_data:
                            trk_id = trk["id"]
                            if trk_id in seen_tracks:
                                continue
                            seen_tracks.add(trk_id)
                            
                            track_node = {
                                "id": trk_id,
                                "name": trk["name"],
                                "duration_ms": trk.get("duration_ms", 0),
                                "ignored": ignored_tracks.get(str(trk_id).strip(), False),
                                "no_match": no_match_tracks.get(str(trk_id).strip(), False),
                                "error_code": error_code_tracks.get(str(trk_id).strip()),
                                "downloaded": False
                            }
                            album_node["tracks"].append(track_node)
                            
                        album_nodes_map[alb_id] = album_node
                    except Exception as e:
                        _log.debug("Failed to fetch tracks in parallel: %s", e)
                        
            # Append them in the original sorted album order
            for alb in albums:
                if alb["id"] in album_nodes_map:
                    artist_node["albums"].append(album_nodes_map[alb["id"]])
                    
        new_artists.append(artist_node)
        
        resolved_artists_for_config.append({
            "id": saved_spotify_id or saved_deezer_id or spot_artist,
            "name": spot_artist,
            "source": entry_source,
            "spotify_name": spot_artist if saved_spotify_id else None,
            "spotify_id": saved_spotify_id,
            "deezer_id": saved_deezer_id,
            "genre_path": artist_entry.get("genre_path", "") if isinstance(artist_entry, dict) else "",
        })

    if only:
        if not new_artists:
            status.status["current_stage"] = "✅ Завершено"
            return lib
        node = new_artists[0]
        merged = []
        replaced = False
        for art in lib.get("artists", []):
            if str(art.get("name") or "").strip().lower() == str(node.get("name") or "").strip().lower():
                merged.append(node)
                replaced = True
            else:
                merged.append(art)
        if not replaced:
            merged.append(node)
        lib["artists"] = merged
        target = str(node.get("name") or only).strip().lower()
        for i, a in enumerate(cfg.get("artists", [])):
            if isinstance(a, dict) and str(a.get("name") or "").strip().lower() == target:
                cfg["artists"][i].update(resolved_artists_for_config[0])
                break
        cfg_mod.save_config(cfg)
    else:
        cfg["artists"] = resolved_artists_for_config
        cfg_mod.save_config(cfg)
        lib["artists"] = new_artists

    status.status["current_stage"] = "Сеть: локальная сверка файлов..."
    check_library_files(cfg, lib)
    
    # Restore idle/completed state
    status.status["current_stage"] = "✅ Завершено"
    return lib


def sync_artist_to_library(spot_artist, artist_id, albums, src, cfg):
    with _LIB_LOCK:
        lib = load_library()
        
        ignored_artists = {}
        ignored_albums = {}
        ignored_tracks = {}
        no_match_tracks = {}
        error_code_tracks = {}
        
        for art in lib.get("artists", []):
            art_id = str(art.get("id") or art.get("name")).strip()
            ignored_artists[art_id] = art.get("ignored", False)
            for alb in art.get("albums", []):
                alb_id = str(alb.get("id")).strip()
                ignored_albums[alb_id] = alb.get("ignored", False)
                for trk in alb.get("tracks", []):
                    trk_id = str(trk.get("id")).strip()
                    ignored_tracks[trk_id] = trk.get("ignored", False)
                    no_match_tracks[trk_id] = trk.get("no_match", False)
                    error_code_tracks[trk_id] = trk.get("error_code")
                    
        # Safely determine spotify_id and deezer_id
        spotify_id = None
        deezer_id = None
        if isinstance(artist_id, str):
            if artist_id.isdigit():
                deezer_id = artist_id
            elif len(artist_id) == 22:
                spotify_id = artist_id
                
        # Find if this artist already has saved IDs in config
        entry_source = "deezer"
        genre_path = ""
        for a in cfg.get("artists", []):
            if isinstance(a, dict) and a.get("name") == spot_artist:
                if not spotify_id:
                    spotify_id = a.get("spotify_id")
                if not deezer_id:
                    deezer_id = a.get("deezer_id")
                entry_source = a.get("source", "deezer")
                genre_path = a.get("genre_path", "")
                
        artist_node = {
            "id": (spotify_id or deezer_id or artist_id or spot_artist),
            "name": spot_artist,
            "source": entry_source,
            "spotify_id": spotify_id,
            "deezer_id": deezer_id,
            "genre_path": genre_path,
            "ignored": ignored_artists.get(str(artist_id or spot_artist).strip(), False),
            "albums": []
        }
        
        from concurrent.futures import ThreadPoolExecutor, as_completed
        album_nodes_map = {}
        
        def fetch_task(alb):
            alb_id = alb["id"]
            alb_name = alb["name"]
            try:
                tracks_data = src.get_album_tracks(alb_id)
            except Exception:
                tracks_data = []
            return alb_id, alb_name, tracks_data
            
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(fetch_task, alb): alb for alb in albums}
            for fut in as_completed(futures):
                try:
                    alb_id, alb_name, tracks_data = fut.result()
                    album_node = {
                        "id": alb_id,
                        "name": alb_name,
                        "album_type": next((a.get("album_type", "album") for a in albums if a["id"] == alb_id), "album"),
                        "ignored": ignored_albums.get(str(alb_id).strip(), False),
                        "tracks": []
                    }
                    seen_tracks = set()
                    for trk in tracks_data:
                        trk_id = trk["id"]
                        if trk_id in seen_tracks:
                            continue
                        seen_tracks.add(trk_id)
                        
                        track_node = {
                            "id": trk_id,
                            "name": trk["name"],
                            "duration_ms": trk.get("duration_ms", 0),
                            "ignored": ignored_tracks.get(str(trk_id).strip(), False),
                            "no_match": no_match_tracks.get(str(trk_id).strip(), False),
                            "error_code": error_code_tracks.get(str(trk_id).strip()),
                            "downloaded": False
                        }
                        album_node["tracks"].append(track_node)
                    album_nodes_map[alb_id] = album_node
                except Exception:
                    pass
                    
        for alb in albums:
            if alb["id"] in album_nodes_map:
                artist_node["albums"].append(album_nodes_map[alb["id"]])
                
        new_artists = []
        replaced = False
        for art in lib.get("artists", []):
            if str(art.get("name", "")).lower().strip() == str(spot_artist).lower().strip():
                new_artists.append(artist_node)
                replaced = True
            else:
                new_artists.append(art)
        if not replaced:
            new_artists.append(artist_node)
            
        lib["artists"] = new_artists
        check_library_files(cfg, lib)


def is_track_ignored(artist_name, album_name, track_id, lib=None):
    if lib is None:
        lib = load_library()
    for art in lib.get("artists", []):
        if art.get("name") == artist_name:
            if art.get("ignored", False):
                return True
            for alb in art.get("albums", []):
                if alb.get("name") == album_name:
                    if alb.get("ignored", False):
                        return True
                    for trk in alb.get("tracks", []):
                        if trk.get("id") == track_id:
                            if trk.get("error_code") == "ERR-3":
                                return True
                            return trk.get("ignored", False)
    return False


def reset_filter_errors():
    with _LIB_LOCK:
        lib = load_library()
        updated = False
        for art in lib.get("artists", []):
            for alb in art.get("albums", []):
                for trk in alb.get("tracks", []):
                    if trk.get("error_code") == "ERR-3":
                        trk["error_code"] = ""
                        trk["no_match"] = False
                        updated = True
        if updated:
            save_library(lib)
        return lib


def toggle_ignore(artist_id, album_id=None, track_id=None):
    lib = load_library()
    for art in lib.get("artists", []):
        if str(art.get("id")).strip() == str(artist_id).strip() or str(art.get("name")).strip() == str(artist_id).strip():
            if album_id is None:
                new_val = not art.get("ignored", False)
                art["ignored"] = new_val
                for alb in art.get("albums", []):
                    alb["ignored"] = new_val
                    for trk in alb.get("tracks", []):
                        trk["ignored"] = new_val
                save_library(lib)
                return lib
            
            for alb in art.get("albums", []):
                if str(alb.get("id")).strip() == str(album_id).strip():
                    if track_id is None:
                        new_val = not alb.get("ignored", False)
                        alb["ignored"] = new_val
                        for trk in alb.get("tracks", []):
                            trk["ignored"] = new_val
                        
                        if not new_val:
                            art["ignored"] = False
                        else:
                            if all(al.get("ignored", False) for al in art.get("albums", [])):
                                art["ignored"] = True
                        save_library(lib)
                        return lib
                    
                    for trk in alb.get("tracks", []):
                        if str(trk.get("id")).strip() == str(track_id).strip():
                            new_val = not trk.get("ignored", False)
                            trk["ignored"] = new_val
                            
                            if not new_val:
                                alb["ignored"] = False
                                art["ignored"] = False
                            else:
                                if all(t.get("ignored", False) for t in alb.get("tracks", [])):
                                    alb["ignored"] = True
                                if all(al.get("ignored", False) for al in art.get("albums", [])):
                                    art["ignored"] = True
                            save_library(lib)
                            return lib
    return lib
