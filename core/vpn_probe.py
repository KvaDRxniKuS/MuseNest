"""yt-dlp / VPN diagnostics engine for MuseNest.

Runs the *same* yt-dlp request pipeline the app uses (core/youtube.py::_base_opts)
through several VPN/proxy configurations and reports, for each of them:

  * which IP / country / ISP YouTube actually sees (egress check),
  * whether YouTube is reachable at all,
  * whether metadata extraction works (and which player clients survive),
  * whether search works (ytsearch — the path MuseNest uses to find tracks),
  * whether audio really downloads and at what speed (throttle detection),
  * whether the production mp3 path (ffmpeg) works.

Everything is offline-tolerant: no probe may raise — failures are captured,
classified into a machine-readable ``code`` and turned into a human verdict.

This module has no Flask dependency so it can be used from the CLI
(``tools/yt_vpn_test.py``) as well as from the web UI.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback

try:
    import yt_dlp
    _HAS_YTDLP = True
except Exception:  # pragma: no cover - only when yt-dlp is absent
    yt_dlp = None
    _HAS_YTDLP = False

PROBE_SCHEMA = 1

# Short, stable, public video ("Me at the zoo", 19 s) — cheap real download test.
DEFAULT_TARGET = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
DEFAULT_SEARCH = "never gonna give you up"

# Player clients worth probing: a blocked/datacenter exit IP often still works
# on one specific client while `web` demands a login.
DEFAULT_CLIENTS = ["web", "android", "tv"]

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# IP/geo endpoints (first one that answers wins). All are public, key-less.
IP_ENDPOINTS = [
    ("ipapi.co", "https://ipapi.co/json"),
    ("ipwho.is", "https://ipwho.is/"),
    ("ipinfo.io", "https://ipinfo.io/json"),
    ("ipify", "https://api.ipify.org?format=json"),
]

# --------------------------------------------------------------------------
# Error classification
# --------------------------------------------------------------------------
# (code, Russian explanation, severity, [substrings])
# severity: fatal (nothing works) / blocked (YouTube refuses) / degraded
ERROR_PATTERNS = [
    ("proxy_dead", "Прокси/VPN не отвечает или не пускает трафик", "fatal", [
        "proxyerror", "tunnel connection failed", "socks proxy", "socksproxy",
        "proxy connection failed", "unable to connect to proxy", "bad gateway",
        "proxy replied", "connection refused", "connection aborted",
        "no route to host", "network is unreachable", "proxy authentication failed",
    ]),
    ("dns_failed", "DNS не резолвится через этот VPN", "fatal", [
        "temporary failure in name resolution", "name or service not known",
        "getaddrinfo failed", "failed to resolve", "nodename nor servname",
    ]),
    ("timeout", "Таймаут — VPN слишком медленный или рвёт соединение", "fatal", [
        "timed out", "timeout", "the read operation timed out",
    ]),
    ("bot_check", "YouTube требует вход («Sign in to confirm you're not a bot»)", "blocked", [
        "sign in to confirm you", "confirm you're not a bot", "confirm your age",
        "please sign in", "sign in to verify", "login required",
    ]),
    ("ip_blocked", "IP этого VPN заблокирован YouTube", "blocked", [
        "ip address is blocked", "ip has been blocked", "requested ip address",
        "your account has been terminated", "blocked your account",
    ]),
    ("rate_limited", "Лимит запросов (HTTP 429) — VPN-IP перегружен", "blocked", [
        "http error 429", "too many requests", "rate limit",
    ]),
    ("geo_blocked", "Контент недоступен в стране этого VPN", "blocked", [
        "not available in your country", "uploader has not made this video available",
        "this video is not available", "content is not available",
        "video unavailable", "isn't available any more", "not available on this website",
    ]),
    ("private_or_paid", "Видео приватное / только для спонсоров (нужен аккаунт)", "blocked", [
        "private video", "members-only", "join this channel", "this video is private",
        "premieres", "channel members",
    ]),
    ("nsig_throttled", "Скорость режется YouTube (nsig/подпись) — типично для VPN", "degraded", [
        "nsig extraction failed", "nsig", "signature extraction failed",
        "throttled", "throttling", "you have requested",
    ]),
    ("format_unavailable", "Формат недоступен — YouTube отдал урезанный список", "degraded", [
        "requested format is not available", "no video formats", "no formats",
    ]),
    ("outdated_ytdlp", "yt-dlp устарел — YouTube поменял API", "degraded", [
        "yt-dlp is out of date", "update yt-dlp", "please update to", "outdated version",
    ]),
    ("ssl_error", "Ошибка TLS через VPN (MITM/сертификат VPN-клиента)", "fatal", [
        "ssl", "certificate verify failed", "wrong version number", "handshake",
    ]),
]

VERDICTS = {
    "ok": "✅ Работает полностью",
    "throttled": "⚠️ Работает, но скорость режется",
    "extract_only": "⚠️ Метаданные есть, скачать не удалось",
    "bot_check": "⛔ YouTube требует вход с этого IP",
    "geo_blocked": "⛔ Контент закрыт для страны этого VPN",
    "rate_limited": "⛔ Лимит запросов (429) с этого IP",
    "ip_blocked": "⛔ IP заблокирован YouTube",
    "no_route": "❌ VPN/прокси не работает (нет связи)",
    "unreachable": "❌ YouTube недоступен",
    "degraded": "⚠️ Частичные проблемы",
    "error": "❌ Ошибка проверки",
}

HINTS = {
    "proxy_dead": "Проверьте, что VPN-клиент включён и слушает этот порт; попробуйте socks5h:// вместо socks5://.",
    "dns_failed": "Включите в VPN «Remote DNS» / укажите DNS 1.1.1.1 вручную.",
    "bot_check": "Подключите cookies YouTube (поле «Cookies YouTube из браузера» или data/cookies.txt) либо смените страну VPN.",
    "ip_blocked": "Смените сервер/ноду VPN — этот IP в чёрном списке YouTube.",
    "rate_limited": "Подождите и снизьте частоту запросов (интервал сканирования), либо смените ноду.",
    "geo_blocked": "Смените страну VPN (обычно US/DE/NL) — контент регионально ограничен.",
    "nsig_throttled": "Обновите yt-dlp (`pip install -U yt-dlp`); при SOCKS5 попробуйте socks5h://.",
    "format_unavailable": "Попробуйте другой player client (например android/tv) — `web` часто урезан на VPN-IP.",
    "outdated_ytdlp": "Обновите yt-dlp: `pip install -U yt-dlp`.",
    "ssl_error": "VPN-клиент подменяет сертификат — отключите его HTTPS-инспекцию или добавьте сертификат в доверенные.",
    "private_or_paid": "Это не проблема VPN: видео доступно только авторизованным/спонсорам.",
    "timeout": "Увеличьте таймаут или смените ноду — соединение обрывается.",
}


def classify_error(text):
    """Map an arbitrary yt-dlp/network error string to a stable diagnostic code.

    Returns ``{"code", "message", "severity", "raw"}``. Never raises.
    """
    raw = str(text or "")
    low = raw.lower()
    for code, message, severity, needles in ERROR_PATTERNS:
        for n in needles:
            if n in low:
                return {"code": code, "message": message, "severity": severity, "raw": raw}
    return {"code": "unknown", "message": raw.strip()[:300] or "Неизвестная ошибка",
            "severity": "fatal", "raw": raw}


# --------------------------------------------------------------------------
# Proxy / profile parsing
# --------------------------------------------------------------------------
_DIRECT_WORDS = {"", "direct", "none", "no", "-", "no-proxy", "noproxy", "off",
                 "без впн", "без vpn", "напрямую", "системный", "system"}


def normalize_proxy(value):
    """Normalize a user-supplied proxy value to ``None`` (direct) or a URL."""
    if value is None:
        return None
    s = str(value).strip()
    if s.lower() in _DIRECT_WORDS:
        return None
    if "://" not in s:
        # be forgiving: 127.0.0.1:1080 -> socks5://127.0.0.1:1080
        s = "socks5://" + s
    scheme, _, rest = s.partition("://")
    scheme = scheme.lower()
    if scheme == "socks":
        scheme = "socks5"
    return scheme + "://" + rest


def proxy_label(profile):
    """Short human label for a profile (used in tables)."""
    p = profile.get("proxy")
    if not p:
        return "direct (без VPN)"
    # hide credentials in logs/reports
    if "@" in p.split("://", 1)[-1]:
        creds, _, host = p.rpartition("@")
        scheme, _, rest = creds.partition("://")
        return f"{scheme}://***@{host}"
    return p


def parse_profile_spec(text):
    """Parse a profile spec into ``(profiles, targets)``.

    Accepted forms:
      * JSON object: ``{"profiles": [{"name": ..., "proxy": ...}], "targets": [...]}``
      * JSON array of profiles
      * plain text, one profile per line::

            # comment
            Нидерланды SOCKS5 = socks5://127.0.0.1:1080
            Германия HTTP, http://user:pass@1.2.3.4:8080
            direct
            target: https://www.youtube.com/watch?v=XXXX
    """
    profiles, targets = [], []
    raw = (text or "").strip()
    if not raw:
        return profiles, targets

    if raw[0] in "[{":
        try:
            data = json.loads(raw)
        except Exception:
            data = None
        if isinstance(data, dict):
            for p in data.get("profiles") or []:
                profiles.append(_coerce_profile(p))
            for t in data.get("targets") or []:
                if str(t).strip():
                    targets.append(str(t).strip())
            if not profiles and data.get("proxy") is not None:
                profiles.append(_coerce_profile(data))
            if profiles:
                return [p for p in profiles if p], targets
        elif isinstance(data, list):
            for p in data:
                profiles.append(_coerce_profile(p))
            return [p for p in profiles if p], targets

    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        if line.lower().startswith(("target:", "targets:", "url:")):
            val = line.split(":", 1)[1].strip()
            if val:
                targets.append(val)
            continue
        name, sep, rest = line.partition("=")
        if not sep:
            name, sep, rest = line.partition(",")
        if sep:
            name = name.strip()
            proxy = normalize_proxy(rest)
        else:
            name, proxy = line.strip(), normalize_proxy(line)
        profiles.append({
            "name": name or (proxy_label({"proxy": proxy})),
            "proxy": proxy,
            "notes": "",
        })
    return profiles, targets


def _coerce_profile(obj):
    if isinstance(obj, str):
        return {"name": proxy_label({"proxy": normalize_proxy(obj)}),
                "proxy": normalize_proxy(obj), "notes": ""}
    if isinstance(obj, dict):
        proxy = normalize_proxy(obj.get("proxy") or obj.get("url") or obj.get("server"))
        name = str(obj.get("name") or obj.get("label") or "").strip()
        return {"name": name or proxy_label({"proxy": proxy}),
                "proxy": proxy,
                "notes": str(obj.get("notes") or obj.get("note") or "")}
    return None


def dedupe_profiles(profiles):
    """Drop profiles with duplicate (name, proxy) and give unique names."""
    out, seen = [], set()
    for i, p in enumerate(profiles or []):
        if not isinstance(p, dict):
            continue
        proxy = normalize_proxy(p.get("proxy"))
        name = str(p.get("name") or proxy_label({"proxy": proxy}) or f"profile-{i + 1}").strip()
        key = (name.casefold(), proxy or "")
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "proxy": proxy, "notes": p.get("notes") or ""})
    return out


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------
def env_report():
    """Describe the local toolchain — VPN problems are often toolchain problems."""
    yt_ver = "unknown"
    if _HAS_YTDLP:
        yt_ver = getattr(getattr(yt_dlp, "version", None), "__version__", "unknown")
    try:
        import socks  # noqa: F401
        pysocks = True
    except Exception:
        pysocks = False
    return {
        "yt_dlp": yt_ver if _HAS_YTDLP else "не установлен",
        "yt_dlp_ok": bool(_HAS_YTDLP),
        "ffmpeg": shutil.which("ffmpeg") or None,
        "ffprobe": shutil.which("ffprobe") or None,
        "pysocks": pysocks,
        "python": sys.version.split()[0],
        "platform": sys.platform,
    }


def env_hints(env):
    hints = []
    if not env.get("yt_dlp_ok"):
        hints.append("Не установлен yt-dlp: `pip install -U yt-dlp`.")
    if not env.get("ffmpeg"):
        hints.append("ffmpeg не найден — скачивание работает, но конвертация в mp3 (путь MuseNest) не пройдёт. "
                     "См. README → Установка ffmpeg.")
    if not env.get("pysocks"):
        hints.append("PySocks не установлен: сам yt-dlp умеет SOCKS5 без него, "
                     "но сторонние библиотеки (requests) — нет. `pip install PySocks` для полноты.")
    return hints


# --------------------------------------------------------------------------
# Low-level helpers
# --------------------------------------------------------------------------
def _run_with_timeout(fn, timeout):
    """Run ``fn()`` in a daemon thread; return ``(result, error, elapsed)``.

    VPN connections hang instead of failing, so every probe is hard-capped.
    """
    box = {}

    def _wrap():
        try:
            box["result"] = fn()
        except Exception as e:
            box["error"] = e

    started = time.time()
    th = threading.Thread(target=_wrap, daemon=True)
    th.start()
    th.join(timeout)
    elapsed = time.time() - started
    if th.is_alive():
        return None, TimeoutError(f"превышен лимит {timeout:.0f} c"), elapsed
    return box.get("result"), box.get("error"), elapsed


def _http(url, proxy=None, timeout=20, data=None, headers=None):
    """GET/POST through the *yt-dlp* networking stack.

    Using yt-dlp's own request director means SOCKS5 works without PySocks and
    the probe observes exactly the transport the downloader will use.
    """
    if not _HAS_YTDLP:
        raise RuntimeError("yt-dlp не установлен")
    from yt_dlp.networking import Request

    hdrs = {"User-Agent": _UA, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    opts = {"quiet": True, "no_warnings": True, "socket_timeout": timeout,
            "retries": 0, "logger": _NullLogger()}
    if proxy:
        opts["proxy"] = proxy
    with yt_dlp.YoutubeDL(opts) as ydl:
        resp = ydl.urlopen(Request(url, data=data, headers=hdrs))
        body = resp.read()
        status = getattr(resp, "status", 200)
        try:
            text = body.decode("utf-8", "replace")
        except Exception:
            text = str(body[:400])
        return {"status": status, "text": text}


class _NullLogger:
    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


class _CollectLogger:
    """yt-dlp logger that records messages and can abort on stop-request."""

    def __init__(self, sink=None, stop_check=None):
        self.sink = sink if sink is not None else []
        self._stop_check = stop_check

    def _log(self, level, msg):
        if self._stop_check and self._stop_check():
            raise RuntimeError("Stop requested")
        try:
            self.sink.append({"level": level, "message": str(msg)[:500]})
        except Exception:
            pass

    def debug(self, msg):
        self._log("debug", msg)

    def info(self, msg):
        self._log("info", msg)

    def warning(self, msg):
        self._log("warning", msg)

    def error(self, msg):
        self._log("error", msg)


def _base_opts(cfg=None, proxy=None, cookies="auto", timeout=30, logger=None, stop_check=None):
    """Build yt-dlp options mirroring core/youtube.py::_base_opts(cfg)."""
    opts = None
    if cookies == "auto" and cfg is not None:
        try:
            from . import youtube as yt_mod
            opts = yt_mod._base_opts(dict(cfg))
        except Exception:
            opts = None
    if opts is None:
        opts = {"quiet": True, "no_warnings": True, "user_agent": _UA,
                "extractor_retries": 1, "retries": 1, "socket_timeout": timeout,
                "http_headers": {"Accept-Language": "en-US,en;q=0.9,ru;q=0.8", "Accept": "*/*"}}
        if cookies == "auto" and cfg is not None:
            try:
                from . import youtube as yt_mod
                path = yt_mod._cookies_path()
                if os.path.exists(path):
                    opts["cookiefile"] = path
            except Exception:
                pass
    if cookies == "none":
        opts.pop("cookiesfrombrowser", None)
        opts.pop("cookiefile", None)
    # Probes must be fast and must not be skewed by production sleep-jitter.
    opts.update({
        "quiet": True, "no_warnings": True,
        "logger": logger or _NullLogger(),
        "socket_timeout": timeout,
        "retries": 1, "extractor_retries": 1,
        "retry_sleep": {"http": 1},
        "sleep_interval": 0, "max_sleep_interval": 0,
    })
    if proxy:
        opts["proxy"] = proxy
    else:
        opts.pop("proxy", None)
    return opts


# --------------------------------------------------------------------------
# Probes
# --------------------------------------------------------------------------
def probe_egress(proxy=None, timeout=15, on_log=None):
    """Which IP/country/ISP does the VPN show to the internet?"""
    started = time.time()
    last_err = None
    for source, url in IP_ENDPOINTS:
        try:
            res = _http(url, proxy=proxy, timeout=timeout,
                        headers={"Accept": "application/json"})
            data = json.loads(res["text"])
        except Exception as e:
            last_err = e
            continue
        info = {
            "ok": True,
            "source": source,
            "ip": data.get("ip") or data.get("query") or data.get("origin")
                  or (data.get("data") or {}).get("ip") or "",
            "country": data.get("country_name") or data.get("country") or "",
            "country_code": (data.get("country_code") or data.get("countryCode")
                             or (data.get("country") or {}).get("code")
                             or (data.get("country_code_iso") or "")),
            "city": data.get("city") or "",
            "org": data.get("org") or data.get("isp") or data.get("connection", {}).get("isp")
                   or data.get("asn", {}).get("name") or "",
            "asn": data.get("asn") if isinstance(data.get("asn"), str)
                   else (data.get("asn") or {}).get("asn") or "",
            "ms": int((time.time() - started) * 1000),
        }
        if not info["ip"]:
            last_err = RuntimeError("ответ без IP")
            continue
        if on_log:
            on_log(f"IP {info['ip']} ({info.get('country') or '?'}) через {source}")
        return info
    err = classify_error(last_err)
    return {"ok": False, "source": None, "ip": "", "country": "", "country_code": "",
            "city": "", "org": "", "asn": "", "ms": int((time.time() - started) * 1000),
            "error": err["message"], "code": err["code"]}


def probe_reachability(proxy=None, timeout=15):
    """Plain HTTPS reachability + latency to YouTube through this exit."""
    started = time.time()
    try:
        res = _http("https://www.youtube.com/", proxy=proxy, timeout=timeout,
                    headers={"Accept": "text/html"})
        ok = 200 <= int(res.get("status") or 0) < 400
        return {"ok": ok, "status": res.get("status"),
                "ms": int((time.time() - started) * 1000), "bytes": len(res.get("text") or "")}
    except Exception as e:
        err = classify_error(e)
        return {"ok": False, "status": None, "ms": int((time.time() - started) * 1000),
                "error": err["message"], "code": err["code"]}


def probe_extract(url=DEFAULT_TARGET, proxy=None, cfg=None, timeout=60,
                  cookies="auto", clients=None, on_log=None):
    """Metadata extraction (``extract_info``) — the first thing that breaks on a bad VPN."""
    started = time.time()
    logger = _CollectLogger()
    opts = _base_opts(cfg, proxy=proxy, cookies=cookies, timeout=min(timeout, 30), logger=logger)
    opts.update({"skip_download": True, "noplaylist": True})
    if clients:
        opts["extractor_args"] = {"youtube": {"player_client": list(clients)}}

    def _do():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    info, err, elapsed = _run_with_timeout(_do, timeout)
    if err is not None:
        diag = classify_error(err)
        msgs = [m["message"] for m in logger.sink if m["level"] in ("warning", "error")][:5]
        return {"ok": False, "url": url, "error": diag["message"], "code": diag["code"],
                "raw": str(err)[:400], "log": msgs, "ms": int(elapsed * 1000)}

    formats = info.get("formats") or []
    audios = [f for f in formats if f.get("acodec") not in (None, "none")]
    best_audio = None
    if audios:
        best_audio = max(audios, key=lambda f: (f.get("abr") or 0))
    videos = [f for f in formats if f.get("vcodec") not in (None, "none")]
    res = {
        "ok": True, "url": url, "ms": int(elapsed * 1000),
        "id": info.get("id"), "title": info.get("title"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader") or info.get("channel"),
        "format_count": len(formats),
        "audio_formats": len(audios),
        "video_formats": len(videos),
        "max_height": max([f.get("height") or 0 for f in videos] or [0]),
        "best_audio_ext": best_audio.get("ext") if best_audio else None,
        "best_audio_abr": best_audio.get("abr") if best_audio else None,
        "best_audio_protocol": best_audio.get("protocol") if best_audio else None,
        "availability": info.get("availability"),
        "log": [m["message"] for m in logger.sink if m["level"] in ("warning", "error")][:5],
    }
    if on_log:
        on_log(f"extract ok: «{res['title']}» — {res['format_count']} форматов, "
               f"аудио {res['best_audio_ext']}/{res['best_audio_abr']}")
    if res["format_count"] == 0:
        res["ok"] = False
        res["code"] = "format_unavailable"
        res["error"] = "YouTube вернул 0 форматов"
    return res


def probe_clients(url=DEFAULT_TARGET, proxy=None, cfg=None, timeout=45,
                  cookies="auto", clients=None, on_log=None):
    """Which YouTube player clients survive from this exit IP."""
    out = []
    for client in (clients or DEFAULT_CLIENTS):
        started = time.time()
        try:
            r = probe_extract(url, proxy=proxy, cfg=cfg, timeout=timeout,
                              cookies=cookies, clients=[client])
            out.append({"client": client, "ok": bool(r.get("ok")),
                        "code": r.get("code") or ("ok" if r.get("ok") else "error"),
                        "formats": r.get("format_count"),
                        "error": r.get("error"),
                        "ms": int((time.time() - started) * 1000)})
        except Exception as e:
            diag = classify_error(e)
            out.append({"client": client, "ok": False, "code": diag["code"],
                        "error": diag["message"], "ms": int((time.time() - started) * 1000)})
        if on_log:
            last = out[-1]
            on_log(f"client {client}: {'ok' if last['ok'] else last['code']} ({last['ms']} ms)")
    return out


def probe_search(query=DEFAULT_SEARCH, limit=5, proxy=None, cfg=None, timeout=60,
                 cookies="auto", on_log=None):
    """``ytsearchN:`` — exactly how MuseNest finds candidate videos."""
    started = time.time()
    logger = _CollectLogger()
    opts = _base_opts(cfg, proxy=proxy, cookies=cookies, timeout=min(timeout, 30), logger=logger)
    # NB: no "ignoreerrors" here — it would swallow the proxy/DNS/geo error and
    # we would misreport a dead VPN as "0 search results".
    opts.update({"skip_download": True, "noplaylist": True,
                 "default_search": "ytsearch", "extract_flat": True})

    def _do():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(f"ytsearch{limit}:{query}", download=False)

    info, err, elapsed = _run_with_timeout(_do, timeout)
    if err is not None:
        diag = classify_error(err)
        return {"ok": False, "query": query, "error": diag["message"], "code": diag["code"],
                "raw": str(err)[:400], "count": 0, "ms": int(elapsed * 1000)}
    entries = [e for e in (info.get("entries") or []) if e and e.get("id")]
    res = {"ok": bool(entries), "query": query, "count": len(entries),
           "first": entries[0].get("title") if entries else None,
           "ms": int(elapsed * 1000)}
    if not entries:
        # Still no entries: classify from what yt-dlp logged, so a transport
        # problem is not reported as an empty result set.
        blob = " ".join(m["message"] for m in logger.sink if m["level"] in ("warning", "error"))
        diag = classify_error(blob) if blob.strip() else {"code": "unknown", "message": ""}
        res["code"] = diag["code"] if diag["code"] != "unknown" else "format_unavailable"
        res["error"] = diag["message"] or "Поиск вернул 0 результатов"
        res["log"] = [m["message"] for m in logger.sink if m["level"] in ("warning", "error")][:5]
    if on_log:
        on_log(f"search «{query}»: {res['count']} результатов ({res['ms']} ms)")
    return res


def probe_download(url=DEFAULT_TARGET, proxy=None, cfg=None, timeout=120,
                   cookies="auto", throttle_kbps=60, on_log=None):
    """Real audio bytes + throughput (throttle detection). No postprocessor — ffmpeg not needed."""
    if not _HAS_YTDLP:
        return {"ok": False, "code": "no_ytdlp", "error": "yt-dlp не установлен"}
    tmp_dir = tempfile.mkdtemp(prefix="musenest-vpnprobe-")
    stats = {"start": None, "end": None, "bytes": 0, "speeds": []}

    def _hook(d):
        if d.get("status") == "downloading":
            if stats["start"] is None:
                stats["start"] = time.time()
            stats["bytes"] = d.get("downloaded_bytes") or 0
            if d.get("speed"):
                stats["speeds"].append(float(d["speed"]))
        elif d.get("status") == "finished":
            stats["end"] = time.time()
            stats["bytes"] = d.get("downloaded_bytes") or d.get("total_bytes") or stats["bytes"]

    logger = _CollectLogger()
    opts = _base_opts(cfg, proxy=proxy, cookies=cookies, timeout=min(timeout, 40), logger=logger)
    opts.update({
        "format": "bestaudio/best",
        "outtmpl": os.path.join(tmp_dir, "probe.%(ext)s"),
        "noplaylist": True,
        "progress_hooks": [_hook],
    })

    def _do():
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

    info, err, elapsed = _run_with_timeout(_do, timeout)
    files = []
    try:
        files = sorted(os.listdir(tmp_dir))
    except Exception:
        pass
    size = 0
    for f in files:
        try:
            size += os.path.getsize(os.path.join(tmp_dir, f))
        except Exception:
            pass
    try:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception:
        pass

    dl_seconds = 0.0
    if stats["start"] and stats["end"] and stats["end"] > stats["start"]:
        dl_seconds = stats["end"] - stats["start"]
    kbps = 0.0
    if dl_seconds > 0 and stats["bytes"]:
        kbps = (stats["bytes"] * 8.0 / 1000.0) / dl_seconds
    elif stats["speeds"]:
        kbps = (sum(stats["speeds"]) / len(stats["speeds"])) * 8.0 / 1000.0

    if err is not None:
        diag = classify_error(err)
        return {"ok": False, "url": url, "code": diag["code"], "error": diag["message"],
                "raw": str(err)[:400], "bytes": size, "kbps": round(kbps, 1),
                "seconds": round(dl_seconds, 2), "ms": int(elapsed * 1000),
                "log": [m["message"] for m in logger.sink if m["level"] in ("warning", "error")][:5]}

    ok = bool(files) and size > 0
    res = {"ok": ok, "url": url, "files": files, "bytes": size,
           "kbps": round(kbps, 1), "seconds": round(dl_seconds, 2),
           "ms": int(elapsed * 1000),
           "throttled": bool(ok and throttle_kbps and kbps < throttle_kbps),
           "throttle_kbps": throttle_kbps}
    if not ok:
        res["code"] = "no_file"
        res["error"] = "Скачивание завершилось без файла"
    if on_log:
        on_log(f"download: {size} байт за {res['seconds']} c = {res['kbps']} kbps"
               + (" (ЗАДУШЕНО)" if res.get("throttled") else ""))
    return res


def probe_musenest_mp3(url=DEFAULT_TARGET, proxy=None, cfg=None, timeout=150, on_log=None):
    """Exercise the *production* path: core.youtube.download_audio() → mp3 via ffmpeg."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return {"ok": False, "code": "no_ffmpeg",
                "error": "ffmpeg не найден — конвертация в mp3 невозможна"}
    try:
        from . import youtube as yt_mod
    except Exception as e:  # pragma: no cover
        return {"ok": False, "code": "import_error", "error": str(e)}

    tmp_dir = tempfile.mkdtemp(prefix="musenest-mp3probe-")
    target = os.path.join(tmp_dir, "probe")
    probe_cfg = dict(cfg or {})
    if proxy:
        probe_cfg["proxy"] = proxy
    else:
        probe_cfg["proxy"] = ""

    started = time.time()
    result, err, elapsed = _run_with_timeout(
        lambda: yt_mod.download_audio(url, target, quality="192", cfg=probe_cfg), timeout)
    files = []
    try:
        files = sorted(os.listdir(tmp_dir))
    except Exception:
        pass
    try:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception:
        pass
    if err is not None:
        diag = classify_error(err)
        return {"ok": False, "code": diag["code"], "error": diag["message"],
                "raw": str(err)[:400], "files": files, "ms": int(elapsed * 1000)}
    ok = any(f.lower().endswith(".mp3") for f in files)
    res = {"ok": ok, "files": files, "ms": int(elapsed * 1000)}
    if not ok:
        res["code"] = "no_mp3"
        res["error"] = "mp3 не создан (проверьте ffmpeg/кодек)"
    if on_log:
        on_log(f"путь MuseNest (mp3): {'ok' if ok else res.get('error')}")
    return res


# --------------------------------------------------------------------------
# Profile run + verdict
# --------------------------------------------------------------------------
def run_profile(profile, cfg=None, targets=None, options=None, on_log=None):
    """Run the whole battery for one VPN/proxy profile. Returns a flat result dict."""
    options = dict(options or {})
    targets = targets or [DEFAULT_TARGET]
    target = targets[0]
    proxy = normalize_proxy(profile.get("proxy"))
    timeout = float(options.get("timeout") or 25)
    cookies = options.get("cookies") or "auto"
    throttle_kbps = float(options.get("throttle_kbps") or 60)
    do_ip = options.get("ip", True)
    do_reach = options.get("reach", True)
    do_extract = options.get("extract", True)
    do_search = options.get("search", True)
    do_download = options.get("download", True)
    do_clients = options.get("clients") not in (None, [], [""])
    do_mp3 = options.get("mp3", False)
    clients = options.get("clients") or DEFAULT_CLIENTS

    result = {
        "schema": PROBE_SCHEMA,
        "name": profile.get("name") or proxy_label({"proxy": proxy}),
        "proxy": proxy,
        "proxy_label": proxy_label(profile),
        "notes": profile.get("notes") or "",
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_s": 0.0,
    }

    def _log(msg):
        if on_log:
            on_log(result["name"], msg)

    started = time.time()
    if do_ip:
        _log("проверка исходящего IP…")
        result["egress"] = probe_egress(proxy=proxy, timeout=timeout, on_log=lambda m: _log(m))
    if do_reach:
        _log("проверка доступности youtube.com…")
        result["reach"] = probe_reachability(proxy=proxy, timeout=timeout)
    if do_extract:
        _log("извлечение метаданных…")
        result["extract"] = probe_extract(target, proxy=proxy, cfg=cfg,
                                          timeout=max(timeout * 2, 45), cookies=cookies,
                                          on_log=lambda m: _log(m))
    if do_clients:
        _log("проверка player clients…")
        result["clients"] = probe_clients(target, proxy=proxy, cfg=cfg,
                                          timeout=max(timeout * 2, 45), cookies=cookies,
                                          clients=clients, on_log=lambda m: _log(m))
    if do_search:
        _log("проверка поиска (ytsearch)…")
        result["search"] = probe_search(options.get("search_query") or DEFAULT_SEARCH,
                                        limit=int(options.get("search_limit") or 5),
                                        proxy=proxy, cfg=cfg,
                                        timeout=max(timeout * 2, 45), cookies=cookies,
                                        on_log=lambda m: _log(m))
    if do_download:
        _log("реальное скачивание аудио…")
        result["download"] = probe_download(options.get("download_url") or target,
                                            proxy=proxy, cfg=cfg,
                                            timeout=float(options.get("download_timeout") or 150),
                                            cookies=cookies, throttle_kbps=throttle_kbps,
                                            on_log=lambda m: _log(m))
    if do_mp3:
        _log("проверка пути MuseNest (mp3)…")
        result["mp3"] = probe_musenest_mp3(options.get("download_url") or target,
                                           proxy=proxy, cfg=cfg,
                                           timeout=float(options.get("mp3_timeout") or 200),
                                           on_log=lambda m: _log(m))

    result["elapsed_s"] = round(time.time() - started, 1)
    result.update(verdict_for(result))
    return result


def verdict_for(result):
    """Derive a verdict + actionable hints from a completed profile result."""
    codes = []
    for key in ("egress", "reach", "extract", "search", "download", "mp3"):
        block = result.get(key)
        if isinstance(block, dict) and not block.get("ok") and block.get("code"):
            codes.append(block["code"])

    blocked = {"bot_check", "ip_blocked", "rate_limited", "geo_blocked"}
    fatal = {"proxy_dead", "dns_failed", "timeout", "ssl_error"}

    verdict = "ok"
    if any(c in fatal for c in codes):
        reach = result.get("reach") or {}
        egress = result.get("egress") or {}
        if (result.get("reach") and not reach.get("ok")) and (
                result.get("egress") and not egress.get("ok")):
            verdict = "no_route"
        elif result.get("reach") and not reach.get("ok"):
            verdict = "unreachable"
        else:
            verdict = "degraded"
    elif any(c in blocked for c in codes):
        for c in ("bot_check", "ip_blocked", "rate_limited", "geo_blocked"):
            if c in codes:
                verdict = c
                break
    elif result.get("download") and not result["download"].get("ok"):
        verdict = "extract_only"
    elif result.get("download", {}).get("throttled"):
        verdict = "throttled"
    elif codes:
        verdict = "degraded"

    hints = []
    seen = set()
    for c in codes:
        h = HINTS.get(c)
        if h and h not in seen:
            seen.add(h)
            hints.append(h)
    verdict_text = VERDICTS.get(verdict, verdict)
    if verdict == "no_route" and not result.get("proxy"):
        # No proxy at all: this is the OS route (system-wide VPN client), so the
        # wording must not blame a proxy that does not exist.
        verdict_text = "❌ Нет связи с YouTube через системный маршрут (VPN-клиент выключен?)"
        hints.insert(0, "Профиль без прокси: включите VPN-клиент или задайте прокси явно (socks5://…).")
    egress = result.get("egress") or {}
    if result.get("egress") and egress.get("ok"):
        hints.append(f"Ваш выход: {egress.get('ip')} — {egress.get('country') or '?'}"
                     + (f", {egress['org']}" if egress.get("org") else ""))
    if verdict == "ok" and result.get("clients"):
        working = [c["client"] for c in result["clients"] if c.get("ok")]
        broken = [c["client"] for c in result["clients"] if not c.get("ok")]
        if broken:
            hints.append(f"Рабочие player clients: {', '.join(working) or '—'}; "
                         f"не работают: {', '.join(broken)}")
    return {"verdict": verdict, "verdict_text": verdict_text,
            "codes": codes, "hints": hints}


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------
def _clip(text, width):
    """Truncate to ``width`` display columns, adding an ellipsis when cut."""
    s = str(text if text is not None else "")
    if len(s) <= width:
        return s
    return s[:max(1, width - 1)] + "…"


def format_text_report(results, env=None, targets=None):
    """Human-readable console report (Russian)."""
    lines = []
    env = env or env_report()
    lines.append("Окружение: yt-dlp %s | ffmpeg %s | PySocks %s | python %s" % (
        env.get("yt_dlp"), "да" if env.get("ffmpeg") else "НЕТ",
        "да" if env.get("pysocks") else "нет", env.get("python")))
    for h in env_hints(env):
        lines.append("  ! " + h)
    lines.append("")

    cols = [26, 16, 4, 12, 12, 14]
    hdr = ("%-26s %-16s %-4s %-12s %-12s %-14s %s" %
           ("Профиль", "Выходной IP", "Стр", "YouTube", "Поиск", "Скачивание", "Вердикт"))
    lines.append(hdr)
    lines.append("-" * max(104, len(hdr)))
    for r in results:
        eg = r.get("egress") or {}
        reach = r.get("reach") or {}
        srch = r.get("search") or {}
        dl = r.get("download") or {}
        dl_txt = "—"
        if dl:
            dl_txt = ("%s кбит/с%s" % (int(dl.get("kbps") or 0),
                                       " (резка)" if dl.get("throttled") else "")) if dl.get("ok") \
                else (dl.get("code") or "нет")
        lines.append("%-26s %-16s %-4s %-12s %-12s %-14s %s" % (
            _clip(r.get("name") or "", cols[0]),
            _clip(eg.get("ip") or "—", cols[1]),
            _clip(eg.get("country_code") or "—", cols[2]),
            _clip("ok" if reach.get("ok") else (reach.get("code") or "нет"), cols[3]),
            _clip("ok" if srch.get("ok") else (srch.get("code") or "—"), cols[4]),
            _clip(dl_txt, cols[5]),
            r.get("verdict_text") or r.get("verdict"),
        ))
    lines.append("")

    for r in results:
        lines.append("=== %s (%s) — %s" % (r.get("name"), r.get("proxy_label"),
                                            r.get("verdict_text") or r.get("verdict")))
        eg = r.get("egress") or {}
        if r.get("egress"):
            if eg.get("ok"):
                lines.append("  IP: %s | %s, %s | %s | ASN %s | %s ms" % (
                    eg.get("ip"), eg.get("country"), eg.get("city") or "—",
                    eg.get("org") or "—", eg.get("asn") or "—", eg.get("ms")))
            else:
                lines.append("  IP: не определён (%s)" % eg.get("error"))
        if r.get("reach"):
            rc = r["reach"]
            lines.append("  youtube.com: %s (%s ms)" % (
                "доступен" if rc.get("ok") else "НЕ доступен: %s" % rc.get("error"), rc.get("ms")))
        ex = r.get("extract") or {}
        if r.get("extract"):
            if ex.get("ok"):
                lines.append("  extract: «%s» | %s форматов (аудио %s) | макс. %sр | %s ms" % (
                    ex.get("title"), ex.get("format_count"), ex.get("audio_formats"),
                    ex.get("max_height"), ex.get("ms")))
            else:
                lines.append("  extract: ОШИБКА [%s] %s" % (ex.get("code"), ex.get("error")))
        if r.get("clients"):
            parts = ["%s:%s" % (c["client"], "ok" if c.get("ok") else (c.get("code") or "нет"))
                     for c in r["clients"]]
            lines.append("  player clients: " + ", ".join(parts))
        if r.get("search"):
            sr = r["search"]
            lines.append("  поиск: %s" % ("%s результатов" % sr.get("count") if sr.get("ok")
                                          else "ОШИБКА [%s] %s" % (sr.get("code"), sr.get("error"))))
        if r.get("download"):
            dl = r["download"]
            if dl.get("ok"):
                lines.append("  скачивание: %s байт за %s c → %s кбит/с%s" % (
                    dl.get("bytes"), dl.get("seconds"), dl.get("kbps"),
                    " ⚠ скорость режется YouTube" if dl.get("throttled") else ""))
            else:
                lines.append("  скачивание: ОШИБКА [%s] %s" % (dl.get("code"), dl.get("error")))
        if r.get("mp3"):
            mp = r["mp3"]
            lines.append("  путь MuseNest (mp3): %s" % (
                "ok (%s)" % ", ".join(mp.get("files") or []) if mp.get("ok")
                else "ОШИБКА [%s] %s" % (mp.get("code"), mp.get("error"))))
        for h in r.get("hints") or []:
            lines.append("  → " + h)
        lines.append("")
    return "\n".join(lines)


def format_markdown_report(results, env=None, targets=None, label=None):
    """Markdown comparison table — handy to paste into an issue/notes."""
    env = env or env_report()
    out = ["# Отчёт тестера yt-dlp / VPN",
           "",
           "- Дата: %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
           "- Метка: %s" % (label or "—"),
           "- yt-dlp: `%s`, ffmpeg: `%s`, python: `%s`" % (
               env.get("yt_dlp"), env.get("ffmpeg") or "нет", env.get("python")),
           "",
           "| Профиль | Прокси | IP | Страна | Провайдер | YouTube | Поиск | Скорость | Вердикт |",
           "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        eg = r.get("egress") or {}
        reach = r.get("reach") or {}
        srch = r.get("search") or {}
        dl = r.get("download") or {}
        speed = ("%s кбит/с%s" % (int(dl.get("kbps") or 0), " ⚠резка" if dl.get("throttled") else "")
                 if dl.get("ok") else (dl.get("code") or "—")) if dl else "—"
        out.append("| %s | `%s` | %s | %s | %s | %s | %s | %s | %s |" % (
            r.get("name"), r.get("proxy_label"), eg.get("ip") or "—",
            eg.get("country") or "—", eg.get("org") or "—",
            "ok" if reach.get("ok") else (reach.get("code") or "нет"),
            "ok" if srch.get("ok") else (srch.get("code") or "—"),
            speed, r.get("verdict_text") or r.get("verdict")))
    out.append("")
    for r in results:
        if r.get("hints"):
            out.append("**%s**" % r.get("name"))
            for h in r["hints"]:
                out.append("- %s" % h)
            out.append("")
    return "\n".join(out)


def run_all(profiles, cfg=None, targets=None, options=None, on_log=None, on_profile=None):
    """Run every profile sequentially (never raises)."""
    profiles = dedupe_profiles(profiles)
    results = []
    for i, p in enumerate(profiles):
        try:
            res = run_profile(p, cfg=cfg, targets=targets, options=options, on_log=on_log)
        except Exception as e:  # pragma: no cover - defensive
            res = {"name": p.get("name"), "proxy": p.get("proxy"),
                   "proxy_label": proxy_label(p), "verdict": "error",
                   "verdict_text": VERDICTS["error"], "codes": ["exception"],
                   "hints": [str(e)], "traceback": traceback.format_exc()[-1500:],
                   "elapsed_s": 0.0}
        results.append(res)
        if on_profile:
            try:
                on_profile(i + 1, len(profiles), res)
            except Exception:
                pass
    return results
