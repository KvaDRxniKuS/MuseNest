"""Zvuk (https://zvuk.com) catalog + direct-download source.

Uses the optional ``zvuk-music`` package when installed for full metadata
(search / artists / releases / tracks). Falls back to the public Tiny REST
endpoint for the direct (non-DRM) stream URL so high-quality downloading works
when the user has a paid subscription + a token.

Auth: the user's token (``X-Auth-Token``) is taken from ``config.json``
(field ``zvuk_token``). An anonymous token unlocks only mid quality.
"""

import os
import subprocess
import tempfile
import shutil
import re

import requests

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
_DEFAULT_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://zvuk.com/",
    "Origin": "https://zvuk.com",
}

TINY_URL = "https://zvuk.com/api/tiny"
GRAPHQL_URL = "https://zvuk.com/api/v1/graphql"

# Map our audio_quality setting to a Zvuk stream quality.
_QUALITY_MAP = {
    "128": "mid",
    "192": "mid",
    "256": "high",
    "320": "high",
    "high": "high",
    "mid": "mid",
    "flac": "flac",
    "lossless": "flac",
}


class ZvukSource:
    def __init__(self, token=None, proxy=None):
        self._token = (token or "").strip() or None
        self._anon_token = None  # lazily fetched anonymous token (mid quality)
        self._proxy = proxy
        self._client = None  # zvuk_music.Client or False

    # ---------- optional zvuk-music package ----------

    def _cli(self):
        if self._client is not None:
            return self._client if self._client else None
        try:
            from zvuk_music import Client
        except ImportError:
            self._client = False
            return None
        try:
            tok = self._token or Client.get_anonymous_token()
            self._client = Client(token=tok, proxy_url=self._proxy, timeout=8)
            return self._client
        except Exception:
            self._client = False
            return None

    def _proxies(self):
        if self._proxy:
            return {"http": self._proxy, "https": self._proxy}
        return None

    # ---------- low-level HTTP ----------

    def _effective_token(self):
        """Return the X-Auth-Token to use: the user token if set, otherwise a
        lazily cached anonymous token (fetched once from /api/tiny/profile).

        Zvuk requires X-Auth-Token even for anonymous mid-quality streams, so
        without this the direct stream request fails with
        "No Zvuk stream URL available (need subscription + token)".
        """
        if self._token:
            return self._token
        if self._anon_token is None:
            try:
                # Anonymous profile fetch MUST NOT require a token (no recursion).
                r = requests.get(
                    TINY_URL + "/profile", headers=dict(_DEFAULT_HEADERS),
                    timeout=8, proxies=self._proxies(),
                )
                r.raise_for_status()
                j = r.json()
                self._anon_token = ((j or {}).get("result") or {}).get("token") or ""
            except Exception:
                self._anon_token = ""
        return self._anon_token or None

    def _tiny(self, path, params=None):
        headers = dict(_DEFAULT_HEADERS)
        tok = self._effective_token()
        if tok:
            headers["X-Auth-Token"] = tok
        r = requests.get(
            TINY_URL + path, params=params, headers=headers,
            timeout=8, proxies=self._proxies(),
        )
        r.raise_for_status()
        return r.json()

    def anonymous_token(self):
        """Return the anonymous token (fetch + cache on first call)."""
        return self._effective_token()

    # ---------- search / metadata ----------

    def search_artists(self, query, limit=8):
        q = " ".join(str(query or "").split())
        if not q or (len(q) == 22 and not q.isdigit()):
            return []
        cli = self._cli()
        if not cli:
            return []
        try:
            res = cli.search(
                q, limit=max(1, min(int(limit or 8), 20)),
                tracks=False, releases=False, playlists=False,
                podcasts=False, episodes=False, profiles=False, books=False,
            )
            artists = getattr(res, "artists", None) or {}
            items = getattr(artists, "items", None) or []
            out = []
            for a in items:
                aid = getattr(a, "id", None)
                name = getattr(a, "title", None) or getattr(a, "name", None)
                if aid is None or not name:
                    continue
                out.append({"id": str(aid), "name": name, "followers": 0})
            # Exact-match first, like Yandex does.
            want = q.casefold()
            out.sort(key=lambda x: 0 if (x["name"] or "").casefold() == want else 1)
            return out[:limit]
        except Exception:
            return []

    def get_artist(self, artist_id):
        cli = self._cli()
        if not cli:
            return {"id": str(artist_id), "name": str(artist_id)}
        try:
            a = cli.get_artist(artist_id)
            return {
                "id": str(getattr(a, "id", artist_id) or artist_id),
                "name": getattr(a, "title", None) or getattr(a, "name", None) or str(artist_id),
            }
        except Exception:
            return {"id": str(artist_id), "name": str(artist_id)}

    def get_albums(self, artist_id, limit=99999, artist_name=""):
        cli = self._cli()
        if not cli:
            return []
        try:
            a = cli.get_artist(
                str(artist_id).replace("zvuk-", ""),
                with_releases=True,
                releases_limit=max(1, min(int(limit or 99999), 100)),
            )
            releases = getattr(a, "releases", None) or []
            out = []
            for r in releases:
                rid = getattr(r, "id", None)
                nm = getattr(r, "title", None)
                if rid is None or not nm:
                    continue
                atype = "album"
                rt = getattr(r, "type", None)
                st = str(getattr(rt, "value", rt) or "").lower()
                if st in ("single", "ep"):
                    atype = "single"
                date = getattr(r, "date", None) or ""
                out.append({
                    "id": f"zvuk-{rid}",
                    "name": nm,
                    "album_type": atype,
                    "release_date": str(date)[:10],
                })
            return out[:limit]
        except Exception:
            return []

    def get_album_tracks(self, album_id, album_name="", artist_name=""):
        cli = self._cli()
        if not cli:
            return []
        try:
            rel = cli.get_release(str(album_id).replace("zvuk-", ""))
            tracks = getattr(rel, "tracks", None) or []
            out = []
            for t in tracks:
                tid = getattr(t, "id", None)
                nm = getattr(t, "title", None)
                if tid is None or not nm:
                    continue
                dur = int(getattr(t, "duration", 0) or 0) * 1000
                arts = [
                    getattr(x, "title", None) or getattr(x, "name", None)
                    for x in (getattr(t, "artists", None) or [])
                ]
                out.append({
                    "id": f"zvuk-{tid}",
                    "name": nm,
                    "duration_ms": dur,
                    "artists": [x for x in arts if x],
                })
            return out
        except Exception:
            return []

    # ---------- direct stream / download ----------

    def get_stream_url(self, track_id, quality="high"):
        """Return a direct, non-DRM audio URL for a track (or None)."""
        q = _QUALITY_MAP.get(str(quality).lower(), "high")
        tid = str(track_id or "").replace("zvuk-", "")
        if not tid.isdigit():
            return None
        try:
            j = self._tiny("/track/stream", {"id": tid, "quality": q})
            return (j or {}).get("stream")
        except Exception:
            return None

    def download_audio(self, track_id, out_path_no_ext, quality="320"):
        """Download a track directly from Zvuk and convert to mp3.

        Returns the final .mp3 path. Raises on any failure so the caller can
        fall back to YouTube.
        """
        stream_url = self.get_stream_url(track_id, quality=quality)
        if not stream_url:
            raise RuntimeError("No Zvuk stream URL available (need subscription + token)")

        tmp = tempfile.NamedTemporaryFile(prefix="zvuk-", suffix=".bin", delete=False)
        tmp_path = tmp.name
        tmp.close()
        try:
            headers = dict(_DEFAULT_HEADERS)
            tok = self._effective_token()
            if tok:
                headers["X-Auth-Token"] = tok
            with requests.get(
                stream_url, headers=headers, stream=True,
                timeout=40, proxies=self._proxies(),
            ) as r:
                r.raise_for_status()
                with open(tmp_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        if chunk:
                            f.write(chunk)
            _convert_to_mp3(tmp_path, out_path_no_ext, quality)
            final = out_path_no_ext + ".mp3"
            if not os.path.exists(final):
                raise RuntimeError("mp3 conversion did not produce a file")
            return final
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _convert_to_mp3(src, out_no_ext, quality):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found — cannot convert Zvuk stream to mp3")
    q = str(quality or "320")
    b = {"320": "320k", "256": "256k", "192": "192k", "128": "128k"}.get(q, "192k")
    cmd = [
        ffmpeg, "-y", "-i", src,
        "-vn",
        "-acodec", "libmp3lame",
        "-b:a", b,
        "-map_metadata", "-1",
        out_no_ext + ".mp3",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg conversion failed: %s" % proc.stderr.decode("utf-8", "replace"))
