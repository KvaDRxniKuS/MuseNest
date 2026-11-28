"""Album covers: resolve once per album, embed into ID3 tags.

The user chose the economical variant (~300×300 embedded, ~30 КБ per track):
Deezer's public album search needs no auth and its ``cover_medium`` is
exactly that size. URLs and image bytes are cached per album, so the backfill
and repeated scans never refetch. Everything here is best-effort: no network
or a failed lookup just means "no cover" — text tags are written anyway.
"""
import threading
import time

import requests

from . import status

_SEARCH_URL = "https://api.deezer.com/search/album"

_URL_TTL = 7 * 24 * 3600      # a resolved cover URL stays valid for a week
_NEG_URL_TTL = 24 * 3600      # "no cover found" is retried the next day
_BYTES_TTL = 30 * 24 * 3600   # image bytes: a month
_MAX_BYTES = 3_000_000        # a 3 MB "cover" is not a cover
_BYTES_CACHE_MAX = 400

_LOCK = threading.Lock()
_URL_CACHE = {}    # (artist_cf, album_cf) -> (url | None, fetched_at)
_BYTES_CACHE = {}  # url -> (bytes, fetched_at)


def _proxies(cfg):
    p = ((cfg or {}).get("proxy") or "").strip()
    return {"http": p, "https": p} if p else None


def get_cover_url(artist, album, cfg=None):
    """A ~300px cover URL for the album, or None. Cached per (artist, album)."""
    artist = str(artist or "").strip()
    album = str(album or "").strip()
    if not artist or not album:
        return None
    key = (artist.casefold(), album.casefold())
    now = time.monotonic()
    with _LOCK:
        hit = _URL_CACHE.get(key)
        if hit is not None:
            url, at = hit
            if now - at < (_URL_TTL if url else _NEG_URL_TTL):
                return url
    url = None
    try:
        j = requests.get(
            _SEARCH_URL, params={"q": "%s %s" % (artist, album), "limit": 1},
            timeout=8, proxies=_proxies(cfg),
        ).json()
        data = (j or {}).get("data") or []
        if data:
            url = data[0].get("cover_medium") or data[0].get("cover_big") or None
    except Exception as e:
        status.log.debug("Cover resolve failed for %s - %s: %s", artist, album, e)
    with _LOCK:
        _URL_CACHE[key] = (url, now)
    return url


def get_cover_bytes(url, cfg=None):
    """The image itself (jpeg/png bytes), cached per URL. None on failure."""
    if not url:
        return None
    now = time.monotonic()
    with _LOCK:
        hit = _BYTES_CACHE.get(url)
        if hit is not None and now - hit[1] < _BYTES_TTL:
            return hit[0]
    data = None
    try:
        r = requests.get(url, timeout=12, proxies=_proxies(cfg))
        r.raise_for_status()
        if len(r.content) > _MAX_BYTES:
            status.log.warning("Cover too large (%d bytes): %s",
                               len(r.content), url)
        else:
            data = r.content
    except Exception as e:
        status.log.debug("Cover download failed for %s: %s", url, e)
    if data is not None:
        with _LOCK:
            if len(_BYTES_CACHE) >= _BYTES_CACHE_MAX:
                _BYTES_CACHE.clear()   # crude bound; entries re-fetch lazily
            _BYTES_CACHE[url] = (data, now)
    return data


def clear_caches():
    """Drop cached URLs/bytes (used by tests and manual refresh)."""
    with _LOCK:
        _URL_CACHE.clear()
        _BYTES_CACHE.clear()
