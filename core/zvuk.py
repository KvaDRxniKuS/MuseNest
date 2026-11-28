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
import time
import random
import threading

import requests

from . import status

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


# ---------------------------------------------------------------------------
# Pacing / anti-bot gate.
#
# Zvuk's anti-bot answers HTTP 418 (occasionally 429) when too many requests
# arrive from one address in a burst — exactly what the parallel download
# workers did to anonymous /track/stream calls: some tracks got their stream,
# others got a teapot. Every direct call to zvuk.com now takes a slot on a
# global pacing timeline (minimum spacing + jitter); waiters sleep *outside*
# the gate lock, so parallel workers wait simultaneously, not in a queue of
# held locks.
# ---------------------------------------------------------------------------

STREAM_DELAY_SEC = 1.5     # minimum spacing between zvuk.com API requests
_RETRYABLE_STATUSES = (418, 429)
_STREAM_TRIES = 3          # 1 initial + 2 retries on 418/429


class _PacingGate:
    """Hands out unique, strictly increasing start moments on one timeline."""

    def __init__(self):
        self._lock = threading.Lock()
        self._next_free = 0.0

    def reserve(self, interval, jitter=(0.1, 0.6)):
        with self._lock:
            start = max(time.time(), self._next_free)
            start += random.uniform(*jitter)
            self._next_free = start + max(0.0, interval)
        return start


_STREAM_GATE = _PacingGate()


# Global anti-bot cool-down: 418/429 from Zvuk means the *address* is
# temporarily blocked — retrying immediately (and from parallel workers!)
# only extends the block. When any request sees 418/429, every zvuk.com
# caller waits out a shared cool-down (escalating 15s -> 30s -> 60s) before
# the next attempt. A successful request resets the escalation.
_ANTIBOT_LOCK = threading.Lock()
_ANTIBOT = {"until": 0.0, "hits": 0, "ok": 0}
_ANTIBOT_STEPS = (15.0, 30.0, 60.0)

# Adaptive spacing: every antibot hit steps the interval up by 3 s
# (3, 6, 9, ...), ten clean requests in a row step it back down by 3 s,
# floor at the base. Ceiling per user report: at a 60 s interval Zvuk keeps
# serving downloads fine — no reason to go higher.
_STREAM_DELAY_MAX = 60.0
_ADAPTIVE_DELAY_SEC = None   # None -> use the base STREAM_DELAY_SEC


def _register_antibot_hit():
    """Start/extend the global cool-down and step the request spacing up.

    Returns the cool-down duration. The escalation decays after 5 quiet
    minutes — a fresh episode tomorrow starts at the shortest step, not
    where the last one left off. The spacing grows LINEARLY, +3 s per hit
    (3, 6, 9, ... capped at 60 s: at that interval Zvuk keeps serving
    downloads) — background downloading must not stall on an exponential.
    Ten clean requests in a row step it back down by 3 s, to the base.
    """
    global _ADAPTIVE_DELAY_SEC
    with _ANTIBOT_LOCK:
        now = time.time()
        if now - _ANTIBOT.get("last", 0.0) > 300.0:
            _ANTIBOT["hits"] = 0
        step = _ANTIBOT_STEPS[min(_ANTIBOT["hits"], len(_ANTIBOT_STEPS) - 1)]
        _ANTIBOT["until"] = now + step
        _ANTIBOT["hits"] += 1
        _ANTIBOT["last"] = now
        _ANTIBOT["ok"] = 0
        _ADAPTIVE_DELAY_SEC = max(
            STREAM_DELAY_SEC,
            min(_STREAM_DELAY_MAX, 3.0 * _ANTIBOT["hits"]))
        return step


def _mark_pacing_ok():
    """A served request: count it; ten in a row step the spacing down 3 s."""
    global _ADAPTIVE_DELAY_SEC
    with _ANTIBOT_LOCK:
        _ANTIBOT["ok"] = _ANTIBOT.get("ok", 0) + 1
        if _ADAPTIVE_DELAY_SEC is not None and _ANTIBOT["ok"] >= 10:
            _ADAPTIVE_DELAY_SEC = max(STREAM_DELAY_SEC,
                                      _ADAPTIVE_DELAY_SEC - 3.0)
            _ANTIBOT["ok"] = 0


def _current_pacing_delay():
    """The spacing zvuk.com requests currently keep (base, adaptive or cap)."""
    with _ANTIBOT_LOCK:
        return (_ADAPTIVE_DELAY_SEC if _ADAPTIVE_DELAY_SEC is not None
                else STREAM_DELAY_SEC)


def _antibot_reset():
    with _ANTIBOT_LOCK:
        _ANTIBOT["hits"] = 0
        _ANTIBOT["until"] = 0.0


def _antibot_wait():
    """Wait out the shared cool-down (no-op when nobody was blocked)."""
    while True:
        with _ANTIBOT_LOCK:
            remaining = _ANTIBOT["until"] - time.time()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 2.0))


def set_stream_pacing(delay):
    """Tune the BASE spacing between zvuk.com API requests (seconds).

    Resets the adaptive interval: 418/429 episodes grow it from this base
    up to :data:`_STREAM_DELAY_MAX`.
    """
    global STREAM_DELAY_SEC, _ADAPTIVE_DELAY_SEC
    STREAM_DELAY_SEC = max(0.0, float(delay))
    _ADAPTIVE_DELAY_SEC = None
    with _ANTIBOT_LOCK:
        _ANTIBOT["ok"] = 0


def _wait_pacing_slot():
    """Sleep until this caller's slot, then wait out any anti-bot cool-down."""
    start = _STREAM_GATE.reserve(_current_pacing_delay())
    pause = start - time.time()
    if pause > 0:
        time.sleep(pause)
    _antibot_wait()


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
                _wait_pacing_slot()
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
        _wait_pacing_slot()
        r = requests.get(
            TINY_URL + path, params=params, headers=headers,
            timeout=8, proxies=self._proxies(),
        )
        if r.status_code in _RETRYABLE_STATUSES:
            _register_antibot_hit()
        r.raise_for_status()
        _mark_pacing_ok()
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
            _wait_pacing_slot()
            res = cli.search(
                q, limit=max(1, min(int(limit or 8), 20)),
                tracks=False, releases=False, playlists=False,
                podcasts=False, episodes=False, profiles=False, books=False,
            )
            _mark_pacing_ok()
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
            _wait_pacing_slot()
            a = cli.get_artist(artist_id)
            _mark_pacing_ok()
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
            _wait_pacing_slot()
            a = cli.get_artist(
                str(artist_id).replace("zvuk-", ""),
                with_releases=True,
                releases_limit=max(1, min(int(limit or 99999), 100)),
            )
            _mark_pacing_ok()
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
            _wait_pacing_slot()
            rel = cli.get_release(str(album_id).replace("zvuk-", ""))
            _mark_pacing_ok()
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
        """One paced attempt at /track/stream. Returns ``(url_or_None, attempt)``.

        Never raises — the caller decides what to try next and what to report.
        The request waits for its slot on the global pacing timeline, and a
        418/429 (anti-bot rate limit) is retried after a growing pause — those
        refusals are usually transient, and a retried anonymous request is
        served normally.
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
                   "status": None, "error": None, "retries": 0}
        for try_no in range(_STREAM_TRIES):
            _wait_pacing_slot()
            try:
                r = requests.get(
                    TINY_URL + "/track/stream", params={"id": tid, "quality": quality},
                    headers=headers, timeout=8, proxies=self._proxies(),
                )
                if (r.status_code in _RETRYABLE_STATUSES
                        and try_no < _STREAM_TRIES - 1):
                    attempt["retries"] += 1
                    attempt["status"] = r.status_code
                    attempt["error"] = "антибот-ограничение (HTTP %d), глобальная пауза и повтор" % r.status_code
                    step = _register_antibot_hit()
                    status.log.info("⏳ Антибот Zvuk: пауза %d с перед повтором "
                                    "(блок действует на все запросы)", int(step))
                    continue
                r.raise_for_status()
                j = r.json() or {}
                url = j.get("stream")
                if url:
                    attempt["url"] = url
                    _antibot_reset()
                    _mark_pacing_ok()   # a served request ends the cool-down escalation
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
        if self._token:
            token_part = "Токен задан."
        elif 403 in statuses or 401 in statuses:
            # Promising "without a token Zvuk serves only mid" is false in this
            # very line: the attempt trace shows the anonymous request refused.
            token_part = "Токен не задан."
        else:
            token_part = "Токен не задан — без него Zvuk отдаёт только mid."

        if (expired_token or 401 in statuses) and self._token:
            head = ("Токен Zvuk истёк или неверен (HTTP 401). Обновите его: войдите на "
                    "zvuk.com, откройте https://zvuk.com/api/tiny/profile, скопируйте "
                    "значение после \"token\": и вставьте в поле «Zvuk токен».")
        elif 401 in statuses:
            # No user token exists, so the 401 came from the anonymous token.
            # Telling the user to "refresh" a token they never set is the same
            # class of lie as the original bug.
            head = ("Zvuk отклонил анонимный доступ (HTTP 401) — без токена этот "
                    "трек не отдаётся. Добавьте токен: войдите на zvuk.com, "
                    "откройте https://zvuk.com/api/tiny/profile, скопируйте "
                    "значение после \"token\": и вставьте в поле «Zvuk токен».")
        elif 418 in statuses or 429 in statuses:
            # Zvuk's anti-bot answers 418 (sometimes 429) to anonymous
            # requests arriving in bursts. The code already retries with
            # pauses; reaching this message means even the retries were
            # refused, so the honest advice is "wait or add a token".
            head = ("Zvuk ограничил поток (HTTP %s) — сработала антибот-защита "
                    "после нескольких запросов подряд. Автоповторы с паузами "
                    "не помогли: подождите пару минут, снизьте число потоков "
                    "или добавьте токен Zvuk в настройках."
                    % ("429" if 429 in statuses else "418"))
            return "%s Пробовал: %s. %s" % (head, _describe_attempts(attempts), token_part)
        elif 403 in statuses and not self._token:
            # No subscription to blame: there is no token at all, so the only
            # actionable fix is to add one.
            head = ("Zvuk отказал в потоке (HTTP 403) — без токена этот трек не "
                    "отдаётся. Добавьте токен: войдите на zvuk.com, откройте "
                    "https://zvuk.com/api/tiny/profile, скопируйте значение после "
                    "\"token\": и вставьте в поле «Zvuk токен».")
        elif 403 in statuses:
            head = ("Zvuk отказал в потоке (HTTP 403) — это качество недоступно для "
                    "вашей подписки или региона.")
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
