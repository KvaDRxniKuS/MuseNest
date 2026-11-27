import json
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

# Current application version, shown in the UI header and returned by the API.
APP_VERSION = "0.5.8"
# Valid monitoring platforms (used for music_source and per-artist source).
VALID_SOURCES = ("spotify", "deezer", "yandex", "zvuk", "musicbrainz")
# Valid downloaders (used for downloader and per-artist downloader).
VALID_DOWNLOADERS = ("youtube", "zvuk")

DEFAULT_CONFIG = {
    "spotify_client_id": "",
    "spotify_client_secret": "",
    "save_folder": "downloads",
    "artists": [],
    "folders": [],
    "zvuk_token": "",
    "yandex_token": "",
    "blacklist": [
        "remix", "edit", "live", "instrumental", "karaoke",
        "cover", "mashup", "bootleg", "acapella", "sped up",
        "slowed", "extended", "reverb", "nightcore", "phonk",
        "vocals only", "vocals",
    ],
    "monitor_enabled": True,
    "monitor_interval_minutes": 60,
    "duration_tolerance_sec": 15,
    "fallback_to_closest": False,
    "max_albums_per_artist": 99999,
    "audio_quality": "320",
    "download_threads": 4,
    "download_max_parallel": 1,
    "download_delay_sec": 3.5,
    "search_delay_sec": 1.0,
    "music_source": "deezer",
    "downloader": "youtube",
}


def sort_key(s):
    res = []
    for ch in str(s).lower():
        o = ord(ch)
        if 0x0430 <= o <= 0x044F:  # Cyrillic а-я
            o += 1000
        res.append(o)
    return res


def artist_source_for_entry(entry, cfg):
    """Effective metadata source for one artist entry.

    An explicit per-artist ``source`` that names a real platform wins;
    anything else (``"auto"``, a missing or invalid value, a plain-string
    artist) follows the global ``music_source`` setting — and only if *that*
    is invalid do we fall back to Deezer. This is what makes a Zvuk
    selection actually reach artists stamped ``"auto"`` by the UI.
    """
    cfg = cfg or {}
    if isinstance(entry, dict):
        src = str(entry.get("source") or "").strip().lower()
        if src in VALID_SOURCES:
            return src
    elif entry is not None:
        # A bare platform name (e.g. build_source("zvuk", cfg)) — trust it when
        # it names a real platform, otherwise fall through to music_source.
        src = str(entry or "").strip().lower()
        if src in VALID_SOURCES:
            return src
    src = str(cfg.get("music_source") or "").strip().lower()
    return src if src in VALID_SOURCES else "deezer"


def sanitize_config(c):
    seen = set()
    uniq = []
    for a in c.get("artists", []):
        if isinstance(a, dict):
            norm = {
                "id": (a.get("id") or None),
                "name": str(a.get("name") or "").strip(),
                "source": (a.get("source") or "auto").lower(),
                "downloader": (a.get("downloader") or "").lower(),
                "spotify_name": (a.get("spotify_name") or None),
                "spotify_id": (a.get("spotify_id") or None),
                "deezer_id": (a.get("deezer_id") or None),
                "yandex_id": (a.get("yandex_id") or None),
                "zvuk_id": (a.get("zvuk_id") or None),
                "genre_path": str(a.get("genre_path") or "").strip().strip("/\\").replace("\\", "/"),
            }
        else:
            norm = {
                "id": None, 
                "name": str(a).strip(),
                "source": "auto", 
                "downloader": "",
                "spotify_name": None,
                "spotify_id": None,
                "deezer_id": None,
                "genre_path": "",
            }
        if not norm["name"]:
            continue
        key = norm["id"] if norm["id"] else norm["name"].casefold()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(norm)
    uniq.sort(key=lambda x: sort_key(x["name"]))
    c["artists"] = uniq

    c["music_source"] = (c.get("music_source") or "deezer").lower()
    if c["music_source"] not in VALID_SOURCES:
        c["music_source"] = "deezer"

    c["downloader"] = (c.get("downloader") or "youtube").lower()
    if c["downloader"] not in VALID_DOWNLOADERS:
        c["downloader"] = "youtube"

    bseen = set()
    buniq = []
    for b in c.get("blacklist", []):
        if not isinstance(b, str):
            continue
        k = b.casefold().strip()
        if not k or k in bseen:
            continue
        bseen.add(k)
        buniq.append(k)
    c["blacklist"] = buniq

    for key, default in [
        ("monitor_interval_minutes", 60),
        ("duration_tolerance_sec", 15),
        ("max_albums_per_artist", 99999),
        ("download_threads", 4),
    ]:
        try:
            val = int(c.get(key, default))
            c[key] = val if val > 0 else default
        except (TypeError, ValueError):
            c[key] = default

    c["download_threads"] = max(1, min(8, c["download_threads"]))

    # YouTube request router (anti rate-limit): how many downloads may run at
    # once (1 = strictly one by one) and the minimum spacing between starts.
    for key, default, lo, hi in [
        ("download_max_parallel", 1, 1, 4),
        ("download_delay_sec", 3.5, 0.0, 120.0),
        ("search_delay_sec", 1.0, 0.0, 120.0),
    ]:
        try:
            val = float(str(c.get(key, default)).strip())
            if not (lo <= val <= hi):
                val = default
            c[key] = int(val) if key == "download_max_parallel" else val
        except (TypeError, ValueError):
            c[key] = default

    c["monitor_enabled"] = bool(c.get("monitor_enabled", True))
    c["fallback_to_closest"] = bool(c.get("fallback_to_closest", False))
    c["audio_quality"] = str(c.get("audio_quality", "320")) or "320"
    c["zvuk_token"] = str(c.get("zvuk_token", "") or "").strip()
    c["yandex_token"] = str(c.get("yandex_token", "") or "").strip()
    c["folders"] = [str(f).strip().replace("\\", "/").strip("/") for f in c.get("folders", []) if f and str(f).strip()]

    sf = c.get("save_folder", "downloads") or "downloads"
    if not os.path.isabs(sf):
        sf = os.path.join(BASE_DIR, sf)
    c["save_folder"] = sf
    return c


def load_artists_from_txt(path):
    artists = []
    if not os.path.exists(path):
        return artists
    
    # Avoid circular import during init
    from . import source as src_mod
    
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            
            kind, val = src_mod.parse_input(line)
            if kind == "spotify_id":
                artists.append({
                    "id": val,
                    "name": line,
                    "source": "spotify",
                    "spotify_name": None,
                    "spotify_id": val,
                    "deezer_id": None,
                    "genre_path": "",
                })
            elif kind == "deezer_id":
                artists.append({
                    "id": val,
                    "name": line,
                    "source": "deezer",
                    "spotify_name": None,
                    "spotify_id": None,
                    "deezer_id": val,
                    "genre_path": "",
                })
            else:
                artists.append({
                    "id": None,
                    "name": line,
                    "source": "auto",
                    "spotify_name": None,
                    "spotify_id": None,
                    "deezer_id": None,
                    "genre_path": "",
                })
    return artists


def save_artists_to_txt(artists, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Список артистов для отслеживания. По одному на строку.\n")
        f.write("# Можно писать просто имя группы, либо вставлять ссылку на Spotify/Deezer.\n\n")
        for a in artists:
            if isinstance(a, dict):
                src_val = a.get("source", "auto")
                # Write Spotify URL ONLY if we have a valid non-numeric 22-char spotify_id
                if src_val == "spotify" and a.get("spotify_id") and not str(a.get("spotify_id")).isdigit() and len(str(a.get("spotify_id"))) == 22:
                    f.write(f"https://open.spotify.com/artist/{a['spotify_id']}\n")
                # Write Deezer URL ONLY if we have a valid numeric deezer_id
                elif src_val == "deezer" and a.get("deezer_id") and str(a.get("deezer_id")).isdigit():
                    f.write(f"https://www.deezer.com/artist/{a['deezer_id']}\n")
                elif a.get("spotify_id") and not str(a.get("spotify_id")).isdigit() and len(str(a.get("spotify_id"))) == 22:
                    f.write(f"https://open.spotify.com/artist/{a['spotify_id']}\n")
                elif a.get("deezer_id") and str(a.get("deezer_id")).isdigit():
                    f.write(f"https://www.deezer.com/artist/{a['deezer_id']}\n")
                elif a.get("id"):
                    id_val = str(a["id"])
                    if id_val.isdigit():
                        f.write(f"https://www.deezer.com/artist/{id_val}\n")
                    elif len(id_val) == 22:
                        f.write(f"https://open.spotify.com/artist/{id_val}\n")
                    else:
                        f.write(f"{a.get('name', id_val)}\n")
                else:
                    f.write(f"{a.get('name', '')}\n")
            else:
                f.write(f"{str(a)}\n")


def load_blacklist_from_txt(path):
    blacklist = []
    if not os.path.exists(path):
        return blacklist
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip().lower()
            if not line or line.startswith("#"):
                continue
            blacklist.append(line)
    return blacklist


def save_blacklist_to_txt(blacklist, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Слова-исключения для YouTube. Видео с этими словами в названии будут игнорироваться.\n\n")
        for b in blacklist:
            f.write(f"{b.strip().lower()}\n")


def load_config():
    if not os.path.exists(CONFIG_PATH):
        save_config(dict(DEFAULT_CONFIG))
        data = dict(DEFAULT_CONFIG)
    else:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            
    artists_txt_path = os.path.join(DATA_DIR, "artists.txt")
    blacklist_txt_path = os.path.join(DATA_DIR, "blacklist.txt")
    save_folder_txt_path = os.path.join(DATA_DIR, "save_folder.txt")
    
    # 1. Sync save_folder.txt
    if not os.path.exists(save_folder_txt_path):
        with open(save_folder_txt_path, "w", encoding="utf-8") as f:
            f.write("# Укажите путь к папке для сохранения треков (например, D:\\Music)\n")
            f.write(f"{data.get('save_folder', 'downloads')}\n")
    else:
        with open(save_folder_txt_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    data["save_folder"] = line
                    break
    
    # 2. Sync artists.txt
    if not os.path.exists(artists_txt_path):
        save_artists_to_txt(data.get("artists", []) or DEFAULT_CONFIG["artists"], artists_txt_path)
    
    txt_artists = load_artists_from_txt(artists_txt_path)
    json_artists = data.get("artists", [])
    
    if not txt_artists and json_artists:
        save_artists_to_txt(json_artists, artists_txt_path)
        txt_artists = load_artists_from_txt(artists_txt_path)
    
    # Merge txt_artists and json_artists to preserve rich metadata (source, spotify_id, deezer_id, spotify_name)
    merged_artists = []
    json_lookup = {}
    for ja in json_artists:
        if not isinstance(ja, dict):
            continue
        keys = []
        if ja.get("id"):
            keys.append(str(ja["id"]).lower().strip())
        if ja.get("spotify_id"):
            keys.append(str(ja["spotify_id"]).lower().strip())
        if ja.get("deezer_id"):
            keys.append(str(ja["deezer_id"]).lower().strip())
        if ja.get("name"):
            keys.append(str(ja["name"]).lower().strip())
            
        for k in keys:
            json_lookup[k] = ja

    for ta in txt_artists:
        matched = None
        ta_id = ta.get("id")
        ta_name = ta.get("name")
        
        if ta_id and str(ta_id).lower().strip() in json_lookup:
            matched = json_lookup[str(ta_id).lower().strip()]
        elif ta_name and str(ta_name).lower().strip() in json_lookup:
            matched = json_lookup[str(ta_name).lower().strip()]
            
        if matched:
            merged_artist = {
                "id": ta_id or matched.get("id"),
                "name": matched.get("name") or ta_name,
                "source": matched.get("source", ta.get("source", "auto")),
                "downloader": matched.get("downloader") or ta.get("downloader") or "",
                "spotify_name": matched.get("spotify_name") or ta.get("spotify_name"),
                "spotify_id": matched.get("spotify_id") or (ta_id if ta.get("source") == "spotify" else None),
                "deezer_id": matched.get("deezer_id") or (ta_id if ta.get("source") == "deezer" else None),
                "yandex_id": matched.get("yandex_id") or ta.get("yandex_id"),
                "zvuk_id": matched.get("zvuk_id") or ta.get("zvuk_id"),
                "genre_path": matched.get("genre_path") or ta.get("genre_path", ""),
            }
            merged_artists.append(merged_artist)
        else:
            merged_artists.append({
                "id": ta_id,
                "name": ta_name,
                "source": ta.get("source", "auto"),
                "downloader": ta.get("downloader") or "",
                "spotify_name": ta.get("spotify_name"),
                "spotify_id": ta_id if ta.get("source") == "spotify" else None,
                "deezer_id": ta_id if ta.get("source") == "deezer" else None,
            })
            
    data["artists"] = merged_artists
    
    # 3. Sync blacklist.txt
    if not os.path.exists(blacklist_txt_path):
        save_blacklist_to_txt(data.get("blacklist", []) or DEFAULT_CONFIG["blacklist"], blacklist_txt_path)
    data["blacklist"] = load_blacklist_from_txt(blacklist_txt_path)
    
    return sanitize_config(data)


def save_config(cfg):
    artists_txt_path = os.path.join(DATA_DIR, "artists.txt")
    blacklist_txt_path = os.path.join(DATA_DIR, "blacklist.txt")
    save_folder_txt_path = os.path.join(DATA_DIR, "save_folder.txt")
    
    save_artists_to_txt(cfg.get("artists", []), artists_txt_path)
    save_blacklist_to_txt(cfg.get("blacklist", []), blacklist_txt_path)
    
    with open(save_folder_txt_path, "w", encoding="utf-8") as f:
        f.write("# Укажите путь к папке для сохранения треков (например, D:\\Music)\n")
        f.write(f"{cfg.get('save_folder', 'downloads')}\n")
    
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
