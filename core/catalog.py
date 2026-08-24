"""Public Spotify artist lookup when the official Web API search returns 403."""
import re
from urllib.parse import quote

import requests

_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
}


def _item(sid, name, followers=0):
    return {
        "id": sid,
        "name": name,
        "followers": followers or 0,
        "link": f"https://open.spotify.com/artist/{sid}",
    }


def search_wikidata(query, limit=8, proxy=None):
    proxies = {"http": proxy, "https": proxy} if proxy else None
    out = []
    seen = set()
    try:
        r = requests.get(
            "https://www.wikidata.org/w/api.php",
            params={
                "action": "wbsearchentities",
                "search": query,
                "language": "en",
                "uselang": "en",
                "type": "item",
                "format": "json",
                "limit": max(1, min(int(limit or 8), 10)),
            },
            headers=_UA,
            timeout=15,
            proxies=proxies,
        )
        r.raise_for_status()
        ids = [x.get("id") for x in (r.json() or {}).get("search") or [] if x.get("id")]
        if not ids:
            return []
        r2 = requests.get(
            "https://www.wikidata.org/w/api.php",
            params={
                "action": "wbgetentities",
                "ids": "|".join(ids),
                "props": "labels|claims",
                "languages": "en|ru",
                "format": "json",
            },
            headers=_UA,
            timeout=15,
            proxies=proxies,
        )
        r2.raise_for_status()
        for ent in ((r2.json() or {}).get("entities") or {}).values():
            claims = (ent.get("claims") or {}).get("P1902") or []
            if not claims:
                continue
            sid = (((claims[0].get("mainsnak") or {}).get("datavalue") or {}).get("value"))
            if not sid or len(str(sid)) != 22 or str(sid) in seen:
                continue
            labels = ent.get("labels") or {}
            name = ((labels.get("en") or labels.get("ru") or {}).get("value")) or query
            seen.add(str(sid))
            out.append(_item(str(sid), name))
            if len(out) >= limit:
                break
    except Exception:
        return []
    return out


def search_open_spotify_html(query, limit=8, proxy=None):
    proxies = {"http": proxy, "https": proxy} if proxy else None
    urls = [
        f"https://open.spotify.com/search/{quote(query)}/artists",
        f"https://open.spotify.com/search/{quote(query)}",
    ]
    ids = []
    html = ""
    for url in urls:
        try:
            r = requests.get(url, headers=_UA, timeout=15, proxies=proxies)
            if r.status_code != 200:
                continue
            html = r.text or ""
            ids = re.findall(r"/artist/([0-9A-Za-z]{22})", html)
            if ids:
                break
        except Exception:
            continue
    out = []
    seen = set()
    for sid in ids:
        if sid in seen:
            continue
        seen.add(sid)
        name = query
        m = re.search(
            rf'"uri"\s*:\s*"spotify:artist:{re.escape(sid)}".{{0,400}}?"name"\s*:\s*"([^"]+)"',
            html,
            re.S,
        )
        if not m:
            m = re.search(
                rf'"name"\s*:\s*"([^"]+)".{{0,400}}?"uri"\s*:\s*"spotify:artist:{re.escape(sid)}"',
                html,
                re.S,
            )
        if m:
            name = m.group(1)
        out.append(_item(sid, name))
        if len(out) >= limit:
            break
    return out


_BAD_TITLES = {
    "listening is everything",
    "spotify",
    "spotify – web player",
    "spotify - web player",
    "spotify web player",
    "music for everyone",
}


def is_real_artist_name(name, artist_id=None):
    n = " ".join(str(name or "").split())
    if not n:
        return False
    if artist_id and n == str(artist_id):
        return False
    low = n.casefold()
    if low in _BAD_TITLES:
        return False
    if "listening is everything" in low:
        return False
    if low.startswith("spotify"):
        return False
    return True


def name_from_wikidata_spotify(artist_id, proxy=None):
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        r = requests.get(
            "https://www.wikidata.org/w/api.php",
            params={
                "action": "query",
                "list": "search",
                "srsearch": f"haswbstatement:P1902={artist_id}",
                "format": "json",
            },
            headers=_UA,
            timeout=15,
            proxies=proxies,
        )
        r.raise_for_status()
        hits = ((r.json() or {}).get("query") or {}).get("search") or []
        if not hits:
            return None
        qid = hits[0].get("title")
        r2 = requests.get(
            "https://www.wikidata.org/w/api.php",
            params={
                "action": "wbgetentities",
                "ids": qid,
                "props": "labels",
                "languages": "en|ru",
                "format": "json",
            },
            headers=_UA,
            timeout=15,
            proxies=proxies,
        )
        r2.raise_for_status()
        labels = (((r2.json() or {}).get("entities") or {}).get(qid) or {}).get("labels") or {}
        name = ((labels.get("en") or labels.get("ru") or {}).get("value"))
        return name if is_real_artist_name(name, artist_id) else None
    except Exception:
        return None


def name_from_musicbrainz_spotify(artist_id, proxy=None):
    proxies = {"http": proxy, "https": proxy} if proxy else None
    url = f"https://open.spotify.com/artist/{artist_id}"
    try:
        r = requests.get(
            "https://musicbrainz.org/ws/2/url",
            params={"resource": url, "inc": "artist-rels", "fmt": "json"},
            headers={"User-Agent": "MuseNest/1.0 (https://github.com/KvaDRxniKuS/MuseNest)"},
            timeout=15,
            proxies=proxies,
        )
        if r.status_code != 200:
            return None
        for rel in (r.json() or {}).get("relations") or []:
            art = rel.get("artist") or {}
            name = art.get("name")
            if is_real_artist_name(name, artist_id):
                return name
    except Exception:
        return None
    return None


def resolve_public_artist_name(artist_id, fallback=None, proxy=None):
    for fn in (name_from_wikidata_spotify, name_from_musicbrainz_spotify):
        name = fn(artist_id, proxy=proxy)
        if is_real_artist_name(name, artist_id):
            return name
    if is_real_artist_name(fallback, artist_id):
        return fallback
    return None


def search_spotify_public(query, limit=8, proxy=None):
    for fn in (search_wikidata, search_open_spotify_html):
        hits = fn(query, limit=limit, proxy=proxy)
        if hits:
            return hits
    return []


def _fetch(url, proxy=None):
    proxies = {"http": proxy, "https": proxy} if proxy else None
    r = requests.get(url, headers=_UA, timeout=20, proxies=proxies)
    if r.status_code != 200:
        return ""
    return r.text or ""


def scrape_artist(artist_id, proxy=None):
    artist_id = str(artist_id or "").strip()
    html = _fetch(f"https://open.spotify.com/artist/{artist_id}", proxy=proxy)
    if not html:
        html = _fetch(f"https://open.spotify.com/embed/artist/{artist_id}", proxy=proxy)
    name = None
    m = re.search(r'"og:title"\s+content="([^"]+)"', html)
    if m:
        name = m.group(1).split("|")[0].strip()
    if not name:
        m = re.search(r'"name"\s*:\s*"([^"]+)"\s*,\s*"uri"\s*:\s*"spotify:artist:' + re.escape(artist_id) + r'"', html)
        if m:
            name = m.group(1)
    followers = 0
    m = re.search(r'"followers"\s*:\s*\{\s*"total"\s*:\s*(\d+)', html)
    if m:
        followers = int(m.group(1))
    if not followers:
        m = re.search(r'([\d\s\u00a0,\.]+)\s*(?:monthly listeners|слушател)', html, re.I)
        if m:
            digits = re.sub(r"\D", "", m.group(1))
            if digits:
                followers = int(digits)
    albums = []
    seen = set()
    extra = _fetch(f"https://open.spotify.com/artist/{artist_id}/discography/all", proxy=proxy)
    blob = html + "\n" + extra
    for mid in re.findall(r"spotify:album:([0-9A-Za-z]{22})", blob) + re.findall(r"/album/([0-9A-Za-z]{22})", blob):
        if mid in seen:
            continue
        seen.add(mid)
        nm = mid
        m = re.search(
            rf'spotify:album:{re.escape(mid)}.{{0,500}}?"name"\s*:\s*"([^"]+)"',
            blob,
            re.S,
        )
        if not m:
            m = re.search(
                rf'"name"\s*:\s*"([^"]+)".{{0,500}}?spotify:album:{re.escape(mid)}',
                blob,
                re.S,
            )
        if m:
            nm = m.group(1)
        atype = "album"
        if re.search(rf'spotify:album:{re.escape(mid)}.{{0,400}}"album_type"\s*:\s*"single"', blob, re.S):
            atype = "single"
        albums.append({"id": mid, "name": nm, "album_type": atype, "release_date": ""})
    return {"id": artist_id, "name": name or artist_id, "followers": followers, "albums": albums}


def scrape_album_tracks(album_id, proxy=None):
    album_id = str(album_id or "").strip()
    html = _fetch(f"https://open.spotify.com/album/{album_id}", proxy=proxy)
    if not html:
        html = _fetch(f"https://open.spotify.com/embed/album/{album_id}", proxy=proxy)
    tracks = []
    seen = set()
    for tid in re.findall(r"spotify:track:([0-9A-Za-z]{22})", html) + re.findall(r"/track/([0-9A-Za-z]{22})", html):
        if tid in seen:
            continue
        seen.add(tid)
        nm = tid
        m = re.search(
            rf'spotify:track:{re.escape(tid)}.{{0,400}}?"name"\s*:\s*"([^"]+)"',
            html,
            re.S,
        )
        if not m:
            m = re.search(
                rf'"name"\s*:\s*"([^"]+)".{{0,400}}?spotify:track:{re.escape(tid)}',
                html,
                re.S,
            )
        if m:
            nm = m.group(1)
        dur = 0
        m = re.search(
            rf'spotify:track:{re.escape(tid)}.{{0,500}}?"duration_ms"\s*:\s*(\d+)',
            html,
            re.S,
        )
        if not m:
            m = re.search(
                rf'spotify:track:{re.escape(tid)}.{{0,500}}?"totalMilliseconds"\s*:\s*(\d+)',
                html,
                re.S,
            )
        if m:
            dur = int(m.group(1))
        tracks.append({"id": tid, "name": nm, "duration_ms": dur, "artists": []})
    return tracks
