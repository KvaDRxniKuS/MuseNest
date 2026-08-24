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


def search_spotify_public(query, limit=8, proxy=None):
    for fn in (search_wikidata, search_open_spotify_html):
        hits = fn(query, limit=limit, proxy=proxy)
        if hits:
            return hits
    return []
