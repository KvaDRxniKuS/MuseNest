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


_UNSET = object()  # sentinel: "let _effective_token() decide"


class ZvukStreamError(RuntimeError):
    """Zvuk refused to return a stream. Carries what was actually attempted so
    the log can state the real reason instead of guessing."""

    def __init__(self, message, detail=None):
        super().__init__(message)
        self.detail = detail or {}


def _http_detail(exc):
    """Extract ``{"status", "error"}`` from a requests exception.

    The previous code swallowed these (``except Exception: return None``), which
    made an expired token, a 403 and a dead proxy look identical.
    """
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None)
    if status is not None:
        try:
            body = " ".join(str(resp.text or "").split())[:180]
        except Exception:
            body = ""
        return {"status": status, "error": body or ("HTTP %s" % status)}
    return {"status": None, "error": " ".join(str(exc).split())[:180]}


# Raw requests exceptions are unreadable in a log line ("HTTPConnectionPool(...
# Max retries exceeded ... NewConnectionError(...)"). Collapse them to a phrase;
# the full text stays in ZvukStreamError.detail for debugging.
_NET_ERR_HINTS = (
    ("max retries exceeded", "нет соединения"),
    ("newconnectionerror", "нет соединения"),
    ("connection refused", "нет соединения (порт закрыт)"),
    ("no route to host", "нет маршрута"),
    ("network is unreachable", "сеть недоступна"),
    ("timed out", "таймаут"),
    ("timeout", "таймаут"),
    ("name or service not known", "DNS не резолвится"),
    ("getaddrinfo failed", "DNS не резолвится"),
    ("temporary failure in name resolution", "DNS не резолвится"),
    ("failed to resolve", "DNS не резолвится"),
    ("proxyerror", "прокси не отвечает"),
    ("tunnel connection failed", "прокси не отвечает"),
    ("unable to connect to proxy", "прокси не отвечает"),
    ("socks", "SOCKS-прокси не отвечает"),
    ("certificate verify failed", "ошибка TLS (сертификат)"),
    ("ssl", "ошибка TLS"),
)


def _short_err(err):
    """Condense a raw requests exception into a short, log-friendly phrase."""
    text = " ".join(str(err or "").split())
    if not text:
        return "ошибка"
    low = text.lower()
    for needle, label in _NET_ERR_HINTS:
        if needle in low:
            return label
    return text if len(text) <= 70 else text[:67] + "…"


def _describe_attempts(attempts):
    """Human-readable trace of everything that was tried, e.g.
    ``high (токен) → HTTP 401; mid (аноним) → HTTP 403``."""
    parts = []
    for a in attempts:
        tok = {"user": "токен", "anon": "аноним", "none": "без токена"}.get(a.get("token"), a.get("token") or "?")
        q = a.get("quality")
        st = a.get("status")
        err = a.get("error")
        if a.get("url"):
            parts.append("%s (%s) → ok" % (q, tok))
        elif st is None:
            parts.append("%s (%s) → %s" % (q, tok, _short_err(err)))
        elif st < 400 and err:
            # 200 without a stream field: the payload detail is the whole point,
            # so it must not be flattened into a bare "HTTP 200".
            parts.append("%s (%s) → HTTP %s, %s" % (q, tok, st, err))
        else:
            parts.append("%s (%s) → HTTP %s" % (q, tok, st))
    return "; ".join(parts)


class ZvukSource:
    def __init__(self, token=None, proxy=None):
        self._token = (token or "").strip() or None
        self._anon_token = None  # lazily fetched anonymous token (mid quality)
        self._proxy = proxy
        self._client = None  # zvuk_music.Client or False
        self.last_quality = None  # quality actually obtained by resolve_stream()
        self._stream_token = None  # token that produced the last successful stream

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

    def _fetch_anon_token(self):
        """Fetch (and cache) the anonymous token from /api/tiny/profile."""
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

    def _effective_token(self):
        """Return the X-Auth-Token to use: the user token if set, otherwise a
        lazily cached anonymous token (fetched once from /api/tiny/profile).

        Zvuk requires X-Auth-Token even for anonymous mid-quality streams, so
        without this the direct stream request fails.
        """
        if self._token:
            return self._token
        return self._fetch_anon_token()

    def _tiny(self, path, params=None, token=_UNSET):
        """GET a Tiny endpoint.

        ``token`` overrides which credential is sent:
          * ``_UNSET`` (default) — whatever :meth:`_effective_token` picks,
          * ``None`` — send no ``X-Auth-Token`` at all (anonymous probe),
          * a string — send exactly that token.

        Diagnostics need the explicit forms: probing "is the API up?" with the
        user token makes an expired token look like an outage.
        """
        headers = dict(_DEFAULT_HEADERS)
        tok = self._effective_token() if token is _UNSET else token
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

    def _try_stream(self, tid, quality, token=None, use_token="effective"):
        """One attempt at /track/stream. Returns ``(url_or_None, attempt_dict)``.

        Never raises — the caller decides what to try next and what to report.
        """
        headers = dict(_DEFAULT_HEADERS)
        if use_token == "effective":
            tok = self._effective_token()
            tok_kind = "user" if self._token else ("anon" if tok else "none")
        elif use_token == "anon":
            tok = self._fetch_anon_token()
            tok_kind = "anon" if tok else "none"
        else:  # explicit token (possibly None)
            tok = token
            tok_kind = "user" if token else "none"
        if tok:
            headers["X-Auth-Token"] = tok

        attempt = {"quality": quality, "token": tok_kind, "url": None,
                   "status": None, "error": None}
        try:
            r = requests.get(
                TINY_URL + "/track/stream", params={"id": tid, "quality": quality},
                headers=headers, timeout=8, proxies=self._proxies(),
            )
            r.raise_for_status()
            j = r.json() or {}
            url = j.get("stream")
            if url:
                attempt["url"] = url
                # The raw token is returned separately on purpose: it must never
                # end up in `attempt`, which is logged on failure.
                return url, attempt, tok
            # 200 but no stream field — report the payload so it is diagnosable.
            attempt["status"] = r.status_code
            attempt["error"] = "в ответе нет поля 'stream': %s" % (" ".join(str(j).split())[:160])
            return None, attempt, tok
        except Exception as e:
            d = _http_detail(e)
            attempt["status"] = d["status"]
            attempt["error"] = d["error"]
            return None, attempt, tok

    def resolve_stream(self, track_id, quality="high"):
        """Return ``(stream_url, used_quality)`` for a track.

        Tries, in order:
          1. the requested quality with the configured credentials,
          2. the same quality anonymously, if the user token was rejected (401),
          3. ``mid`` (the only quality an anonymous token gets) — so a track is
             still downloaded at mid instead of falling through to YouTube.

        Raises :class:`ZvukStreamError` with the real HTTP statuses when every
        attempt fails.
        """
        tid = str(track_id or "").replace("zvuk-", "")
        if not tid.isdigit():
            raise ZvukStreamError(
                "Некорректный id трека Zvuk: %r (нужен числовой id, например 3862635281)" % (track_id,),
                {"reason": "bad_id"})

        requested = _QUALITY_MAP.get(str(quality).lower(), "high")
        ladder = [requested] + (["mid"] if requested != "mid" else [])
        attempts = []
        expired_token = False

        for q in ladder:
            # Once the user token was rejected (401) there is no point sending
            # it again for the next quality — go straight to the anonymous one.
            url, attempt, tok = self._try_stream(
                tid, q, use_token="anon" if expired_token else "effective")
            attempts.append(attempt)
            if url:
                self.last_quality = q
                self._stream_token = tok
                return url, q

            # An expired/invalid user token answers 401 — retry anonymously once.
            if attempt.get("token") == "user" and attempt.get("status") == 401:
                expired_token = True
                url, anon_attempt, anon_tok = self._try_stream(tid, q, use_token="anon")
                attempts.append(anon_attempt)
                if url:
                    self.last_quality = q
                    self._stream_token = anon_tok
                    return url, q

        self._stream_token = None

        raise ZvukStreamError(
            self._stream_error_message(track_id, requested, ladder, attempts, expired_token),
            {"attempts": attempts, "token_set": bool(self._token),
             "expired_token": expired_token, "track_id": str(track_id)})

    def _stream_error_message(self, track_id, requested, ladder, attempts, expired_token):
        """Build an honest message: what was tried, what Zvuk answered."""
        statuses = [a.get("status") for a in attempts]
        token_part = ("Токен задан." if self._token
                      else "Токен не задан — без него Zvuk отдаёт только mid.")

        if expired_token or 401 in statuses:
            head = ("Токен Zvuk истёк или неверен (HTTP 401). Обновите его: войдите на "
                    "zvuk.com, откройте https://zvuk.com/api/tiny/profile, скопируйте "
                    "значение после \"token\": и вставьте в поле «Zvuk токен».")
        elif 403 in statuses:
            head = ("Zvuk отказал в потоке (HTTP 403) — это качество недоступно для вашей "
                    "подписки/региона, и анонимный mid тоже не отдан.")
        elif all(s is None for s in statuses):
            # Every attempt failed before reaching Zvuk: name the reason once and
            # leave the per-attempt detail out of the log line — it is identical
            # for each quality and the raw requests text is unreadable.
            reason = _short_err(attempts[0].get("error")) if attempts else "ошибка"
            head = ("Zvuk недоступен (%s) — до /api/tiny/track/stream не дошёл "
                    "ни один запрос." % reason)
            return "%s Пробовал качества: %s. %s" % (head, ", ".join(ladder), token_part)
        else:
            head = "Zvuk не отдал поток для трека %s (запрошено %s)." % (track_id, requested)

        return "%s Пробовал: %s. %s" % (head, _describe_attempts(attempts), token_part)

    def get_stream_url(self, track_id, quality="high"):
        """Return a direct, non-DRM audio URL for a track (or None).

        Thin compatibility wrapper around :meth:`resolve_stream`; prefer
        ``resolve_stream`` when the failure reason matters.
        """
        try:
            url, _q = self.resolve_stream(track_id, quality=quality)
            return url
        except ZvukStreamError:
            return None

    def download_audio(self, track_id, out_path_no_ext, quality="320"):
        """Download a track directly from Zvuk and convert to mp3.

        Returns the final .mp3 path. Raises on any failure so the caller can
        fall back to YouTube.
        """
        stream_url, used_quality = self.resolve_stream(track_id, quality=quality)
        self.last_quality = used_quality

        tmp = tempfile.NamedTemporaryFile(prefix="zvuk-", suffix=".bin", delete=False)
        tmp_path = tmp.name
        tmp.close()
        try:
            headers = dict(_DEFAULT_HEADERS)
            # Use the exact token that produced this stream URL: if the user
            # token was rejected (401) the stream came from the anonymous one,
            # and downloading with the expired token would fail again.
            tok = self._stream_token or self._effective_token()
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
